# -*- coding: utf-8 -*-
"""K 线演化新鲜度闸。

## 为什么需要这个

2026-11-10 → 11-16，`kbar.db` **静默停摆 6.5 天**：
`tools/ingest_kbar.py` 有一个变量遮蔽 bug，任何调用都 `UnboundLocalError` 必崩。
但没有任何告警 —— 页面照常渲染，只是画的是旧数据。

**沉默的失效比报错危险得多。** 告警的报错会有人来修；
一张看起来正常、实际停在 6 天前的图，会让所有「基于它得出的结论」一起错，
而且没人怀疑。

这个脚本干三件事：
  1. 算每个周期落后多少小时
  2. 写 `data/kbar_health.json`（给 Web 读，页面上能看见「这一层落后 N 小时」）
  3. 落后超过阈值就推飞书告警 + 写 `logs/ai-trader.log`，并以非 0 退出
     （非 0 → systemd unit 变 failed → `systemctl --failed` 能看见）

用法:
    python tools/kbar_freshness.py                 # 检查，超过 2h 报警
    python tools/kbar_freshness.py --max-lag 3     # 自定义阈值
    python tools/kbar_freshness.py --no-alert      # 只检查不推送
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

KBAR_DB = os.path.join(ROOT, "data", "kbar.db")
HEALTH_JSON = os.path.join(ROOT, "data", "kbar_health.json")

# 每个周期的「一根」有多长，用来算允许的宽松量
INTERVALS = ((300000, "5m"), (900000, "15m"))


def _max_ts(con, interval_ms: int) -> int:
    row = con.execute(
        "SELECT MAX(ts) FROM kbar_evolution WHERE interval_ms=?",
        (int(interval_ms),)).fetchone()
    return int(row[0]) if row and row[0] else 0


def check(kbar_db: str = KBAR_DB, max_lag_h: float = 2.0,
          now_ms: int | None = None) -> dict:
    """返回健康快照。不写文件、不报警 —— 纯函数，方便测。"""
    import sqlite3

    now = int(now_ms if now_ms is not None else
              dt.datetime.now().timestamp() * 1000)
    human = dt.datetime.fromtimestamp(now / 1000).strftime("%Y-%m-%d %H:%M:%S")

    if not os.path.exists(kbar_db):
        # 注意：这里必须返回**和正常路径同构**的快照。
        # 第一版这里少放了 max_lag_hours 和 layers，结果 alert() 一读就 KeyError，
        # 而报警路径崩掉恰恰是最不该崩的地方（正常时永远走不到它）。
        return {"ok": False, "reason": "kbar.db 不存在",
                "max_lag_hours": max_lag_h,
                "checked_at_ms": now, "checked_at_human": human,
                "layers": {label: {"max_ts": 0, "max_ts_human": None,
                                   "lag_hours": None, "stale": True}
                           for _, label in INTERVALS}}

    con = sqlite3.connect("file:%s?mode=ro" % kbar_db.replace("\\", "/"),
                          uri=True)
    try:
        layers = {}
        for interval_ms, label in INTERVALS:
            mx = _max_ts(con, interval_ms)
            lag_h = (now - mx) / 3600000.0 if mx else None
            layers[label] = {
                "max_ts": mx,
                "max_ts_human": (dt.datetime.fromtimestamp(mx / 1000)
                                 .strftime("%Y-%m-%d %H:%M") if mx else None),
                "lag_hours": None if lag_h is None else round(lag_h, 2),
                # 没有数据也算 stale —— 那是最坏的情况，不能算「没问题」
                "stale": True if lag_h is None else lag_h > max_lag_h,
            }
    finally:
        con.close()

    ok = not any(v["stale"] for v in layers.values())
    return {"ok": ok, "max_lag_hours": max_lag_h,
            "checked_at_ms": now,
            "checked_at_human": human,
            "layers": layers}


def write_health(snap: dict, path: str = HEALTH_JSON) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)          # 原子替换：Web 永远读到完整文件


def _describe(snap: dict) -> str:
    parts = []
    for label, v in snap["layers"].items():
        if v["lag_hours"] is None:
            parts.append("%s 没有任何数据" % label)
        else:
            parts.append("%s 落后 %.1f 小时（最新 %s）"
                         % (label, v["lag_hours"], v["max_ts_human"]))
    return "；".join(parts)


def alert(snap: dict, webhook: str = "") -> bool:
    """推飞书 + 写主日志。返回是否真的发出去了。"""
    title = "⚠ ai-trader K线演化停摆"
    body = ("%s\n阈值 %.1f 小时。这一层停了，所有基于它的分析都是旧数据。\n"
            "先查：cd /home/cryptobot/ai-trader && tail -40 logs/kbar_tick.log"
            % (_describe(snap), snap["max_lag_hours"]))

    # 进现有日志通道（运维 grep 的那个文件）
    log = os.path.join(ROOT, "logs", "ai-trader.log")
    try:
        os.makedirs(os.path.dirname(log), exist_ok=True)
        with open(log, "a", encoding="utf-8") as f:
            f.write("%s WARNING kbar_freshness | %s %s\n"
                    % (dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                       title, body.replace("\n", " | ")))
    except OSError:
        pass

    webhook = webhook or os.environ.get("AI_FEISHU_WEBHOOK", "")
    if not webhook:
        return False
    try:
        from ai_trader.guard import feishu_notifier
        fn = feishu_notifier(webhook)
        if fn is None:
            return False
        fn(title, body)
        return True
    except Exception as e:                      # 告警本身不能把流程搞挂
        sys.stderr.write("[kbar_freshness] 飞书推送失败：%s\n" % e)
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description="K线演化新鲜度闸")
    ap.add_argument("--kbar-db", default=KBAR_DB)
    ap.add_argument("--max-lag", type=float, default=2.0,
                    help="允许落后多少小时（默认 2）")
    ap.add_argument("--no-alert", action="store_true", help="只检查，不推送")
    ap.add_argument("--quiet", action="store_true", help="不打印正常状态")
    args = ap.parse_args()

    snap = check(args.kbar_db, args.max_lag)
    try:
        write_health(snap)
    except OSError as e:
        sys.stderr.write("[kbar_freshness] 写 %s 失败：%s\n" % (HEALTH_JSON, e))

    if snap["ok"]:
        if not args.quiet:
            print("[kbar_freshness] 正常：%s" % _describe(snap))
        return 0

    print("[kbar_freshness] ✗ 落后超阈值（%.1f 小时）：%s"
          % (snap["max_lag_hours"], _describe(snap)))
    if not args.no_alert:
        sent = alert(snap)
        print("  飞书告警：%s" % ("已推送" if sent else "未配置或推送失败"))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
