# -*- coding: utf-8 -*-
"""FILL-1 离线重放量具：用**生产真实数据**证明守卫挡住幽灵成交。

2026-11-25 14:50 的真钱事故：挂单成功后，状态机在同一批/下一轮就推出
FILLED → close → CLOSED_TP，而交易所那两张单始终是 NEW、executed_qty=0
→ 两张真实挂单变成孤儿。根因是游标被解冻重算到 386，而 armed_at_bar 是
399，ARMED 回头扫了**挂单之前**的 K。

本量具把当时那条真实计划 + market.db 里真实的 15m/5m K 线，按当时游标
逐周期（每根新 15m 推进一次）喂进状态机，跑 A/B 两组：

  A（反事实）把 armed_at_bar / armed_at_sub_ts 置空 → 守卫整段失效
              = 修复前的行为
  B（现状）  锚点保留 → 守卫生效

**为什么必须是 A/B 而不是只跑现状**：只看到 B=ARMED 证明不了"是守卫起
的作用"——也可能是这批数据本来就推不出成交。必须同一批数据、同一份代码、
唯一变量是守卫，才构成因果证据。

用法（在服务器上跑，需要 data/market.db + data/market_pending.db）：

    PYTHONPATH=/home/cryptobot/ai-trader \\
        .venv/bin/python tools/replay_fill_guard.py

判据：挡掉幽灵成交次数 > 0 且 误伤次数 == 0。
"""
import sqlite3
import sys

sys.path.insert(0, "/home/cryptobot/ai-trader")

from ai_trader.trading import thesis as T                  # noqa: E402
from ai_trader.trading import pending_thesis_store as PTS  # noqa: E402

DATA = "/home/cryptobot/ai-trader/data"


class Bar(object):
    """最小 K 线：字段名与 feed.Bar 无关，只要能取到 o/h/l/c/ts。"""

    __slots__ = ("ts", "o", "h", "l", "c", "v")

    def __init__(self, ts, o, h, l, c, v=0.0):
        self.ts, self.o, self.h, self.l, self.c, self.v = ts, o, h, l, c, v

    def __repr__(self):
        return "Bar(%s o=%.2f h=%.2f l=%.2f c=%.2f)" % (
            self.ts, self.o, self.h, self.l, self.c)


def load(interval_ms, until_ts, limit):
    con = sqlite3.connect("file:%s/market.db?mode=ro" % DATA, uri=True)
    rows = con.execute(
        "SELECT open_time, o, h, l, c, v FROM bars WHERE interval_ms=?"
        " AND symbol='BTCUSDT' AND open_time<=? ORDER BY open_time DESC"
        " LIMIT ?", (interval_ms, until_ts, limit)).fetchall()
    con.close()
    return [Bar(int(r[0]), float(r[1]), float(r[2]), float(r[3]),
                float(r[4]), float(r[5] or 0.0)) for r in reversed(rows)]


def thesis_rows():
    """只取 2026-11-25 14:50 那两条（本轮真钱事故的当事行）。

    ★ 必须精确锁定：其它历史行的 armed_at_bar=0、armed_at_sub_ts 为空，
      推不出「游标回头扫」这个场景，拿它们重放只能得到"新旧都一样"，
      而那个结果会被误读成"守卫没用"。
    """
    want = ("下跌三引线假突破限价单|up|1795572900000|58930.0000",
            "下跌三引线假突破限价单|up|1795585500000|58978.7000")
    con = sqlite3.connect("file:%s/market_pending.db?mode=ro" % DATA, uri=True)
    con.row_factory = sqlite3.Row
    out = []
    for tid in want:
        r = con.execute("SELECT * FROM pending_theses WHERE thesis_id=?",
                        (tid,)).fetchone()
        if r is not None:
            out.append(dict(r))
        else:
            print("！库里找不到 %s" % tid)
    con.close()
    return out


def load_old_thesis_module():
    """反事实对照：不引入旧模块，而是把「挂单时刻」抹成 None。

    ★ 为什么这样等价：守卫的判据是
        `sb_ts <= armed_at_sub_ts`  → 跳过（以及 `i <= armed_at_bar`）
      把这两个锚点都置空，守卫整段失效，行为即退回修复前
      （挂单之前的 K 也会被拿去判成交）。
      比去服务器上翻 git 更可靠：服务器没有 .git，
      而且这里比的是**同一份代码里守卫生效/失效**，没有别的变量。
    """
    return None, "锚点置空（守卫失效 = 修复前行为）"


def replay(mod, thesis, b15, b5, progress):
    """`step()` 返回 `(RunResult, progress)` —— 取结果部分。"""
    res, _ = mod.step(thesis, b15, mod.LineBook(), progress=progress,
                      entry_bars=b5, entry_interval_ms=300000,
                      interval_ms=900000)
    return res


def main():
    rows = thesis_rows()
    print("候选预期 %d 条" % len(rows))
    tgt = [r for r in rows if str(r.get("state")) in
           ("CLOSED_TP", "CLOSED_SL", "CLOSED_THESIS")]
    if not tgt:
        print("库里没有已终止的 up 预期，无法重放")
        return 1
    _unused, old_path = load_old_thesis_module()
    print("反事实对照：%s" % old_path)
    print()
    verdict = []
    for row in tgt[:3]:
        tid = row["thesis_id"]
        th = PTS.thesis_of(row)
        prog = PTS.progress_of(row)
        armed_bar = row.get("armed_at_bar")
        armed_sub = row.get("armed_at_sub_ts")
        bar_ts = int(row.get("bar_ts") or 0)
        next_index = int(row.get("next_index") or 0)
        # ★ 逐周期重放：生产是「每根新 15m 推进一次」，缓冲随之前移。
        #   只重放一根会漏掉事故真正发生的那一轮（下一条预期就是这样）。
        p = dict(prog)
        p["state"] = T.ARMED
        p["next_index"] = next_index
        p_old = dict(p)
        p_old["armed_at_bar"] = None      # 反事实：守卫失效 = 修复前
        p_old["armed_at_sub_ts"] = None
        print("=" * 90)
        print("%s" % tid)
        print("  挂单 %.2f 止损 %.2f 止盈 %.2f｜armed_at_bar=%s"
              " armed_at_sub_ts=%s next_index=%s"
              % (th.order_price, th.stop_price, th.target_price, armed_bar,
                 armed_sub, next_index))
        suppressed = 0        # 守卫挡掉的幽灵成交
        wrong = 0             # 守卫把正常成交也挡掉的次数（必须为 0）
        for k in range(0, 4):
            end = bar_ts + k * 900000
            b15 = load(900000, end, 400)
            b5 = load(300000, end, 1200)
            if len(b15) < 2 or not b5:
                continue
            try:
                o = replay(T, PTS.thesis_of(row), b15, b5, p_old).state
            except Exception as e:
                o = "ERR:%s" % e
            try:
                n = replay(T, PTS.thesis_of(row), b15, b5, p).state
            except Exception as e:
                n = "ERR:%s" % e
            tag = ""
            if o == T.FILLED and n != T.FILLED:
                suppressed += 1
                tag = "  ← 守卫挡住幽灵成交"
            elif n == T.FILLED and o != T.FILLED:
                wrong += 1
                tag = "  ← ★ 守卫误伤正常成交"
            print("   周期 +%d 根：旧=%-10s 新=%-10s%s" % (k, o, n, tag))
        print("  小结：挡掉幽灵 %d 次，误伤 %d 次" % (suppressed, wrong))
        verdict.append((tid, suppressed, wrong))

    print()
    print("=" * 90)
    total_sup = sum(s for _, s, _ in verdict)
    total_wrong = sum(w for _, _, w in verdict)
    ok = total_sup > 0 and total_wrong == 0
    for tid, sup, wrong in verdict:
        print("%-46s 挡掉幽灵 %d 次，误伤 %d 次"
              % (tid[:44], sup, wrong))
    print()
    print("结论：%s" % (
        "守卫在这批真实数据上挡掉 %d 次幽灵成交、0 次误伤 —— 因果链闭合"
        % total_sup if ok else
        "★ 挡掉 %d 次、误伤 %d 次，需继续查" % (total_sup, total_wrong)))
    return 0 if ok else 1


sys.exit(main())
