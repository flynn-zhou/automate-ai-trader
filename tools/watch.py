# -*- coding: utf-8 -*-
"""一屏看全系统在干什么（只读，绝不改任何东西）。

用途：等开仓的时候不用反复问「现在到哪一步了」。回答四个问题：

  1. 交易所上到底有没有我们的活单？成交了吗？
  2. 现价离挂单价多远？（还要跌/涨多少才成交）
  3. 如果已经开仓：仓位多大、止损在哪、离爆仓多远？
  4. 系统自己有没有在报错？（最近的风险事件 / 决策）

用法（服务器上）：

    PYTHONIOENCODING=utf-8 .venv/bin/python tools/watch.py
    PYTHONIOENCODING=utf-8 .venv/bin/python tools/watch.py --loop 60

`--loop` 每 60 秒重画一次（Ctrl+C 退出）。

★ 为什么单独写一个而不是加个 web 页面：调交易所要签名密钥，
  这些东西只该在服务器进程里待着。这是个纯读取的本地工具，
  不新增任何对外暴露面。
"""
import argparse
import asyncio
import os
import sqlite3
import sys
import time
from types import SimpleNamespace

ROOT = os.environ.get("AI_TRADER_ROOT", "/home/cryptobot/ai-trader")
sys.path.insert(0, ROOT)

ENV_FILE = "/etc/ai-trader/env"


def _env(name):
    v = os.environ.get(name)
    if v:
        return v
    try:
        for line in open(ENV_FILE, encoding="utf-8"):
            line = line.strip()
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def _ro(path, sql, args=()):
    try:
        c = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
        try:
            return list(c.execute(sql, args))
        finally:
            c.close()
    except Exception as e:                                   # noqa: BLE001
        return [("读取失败：%s" % e,)]


def _ago(ms):
    try:
        d = (int(time.time() * 1000) - int(ms)) / 1000.0
    except (TypeError, ValueError):
        return "?"
    if d < 90:
        return "%.0f 秒前" % d
    if d < 5400:
        return "%.1f 分钟前" % (d / 60.0)
    return "%.1f 小时前" % (d / 3600.0)


async def snapshot():
    from ai_trader.exchange.live import LiveExecutor

    key, sec = _env("AI_API_KEY"), _env("AI_API_SECRET")
    cfg = SimpleNamespace(trading=SimpleNamespace(symbol="BTCUSDT",
                                                  leverage=22.0))
    ex = LiveExecutor(None, cfg, key, sec)
    try:
        oo = await ex.open_orders("BTCUSDT")
        px = await ex.last_trade_price()
        pos = await ex.position()
        eq = await ex.equity()
    finally:
        try:
            await ex.close()
        except Exception:                                    # noqa: BLE001
            pass
    return oo, px, pos, eq


def draw(oo, px, pos, eq):
    PEND = "%s/data/market_pending.db" % ROOT
    TRADE = "%s/data/market_trade.db" % ROOT

    print("=" * 78)
    print("  %s    现价 %.2f" % (time.strftime("%H:%M:%S"), px))
    print("=" * 78)

    # ---- 1. 交易所上的活单 ----
    print()
    print("① 交易所挂单：%d 张" % len(oo or ()))
    if not oo:
        print("    （没有活单 —— 要么已成交，要么还没挂出去）")
    for o in oo or ():
        dist = ""
        try:
            p = float(o.price or 0)
            if p:
                dist = "｜距现价 %+.3f%%（%.1f 点）" % (
                    (p - px) / px * 100.0, p - px)
        except (TypeError, ValueError, ZeroDivisionError):
            pass
        print("    %s %s %s @ %.2f  已成交 %s%s"
              % (o.side, o.type, o.qty, float(o.price or 0),
                 o.executed_qty, dist))
        print("       client_id=%s  status=%s" % (o.client_id, o.status))

    # ---- 2. 仓位 ----
    qty = float((pos or {}).get("qty") or 0.0)
    print()
    print("② 持仓：%s %s" % (qty or "空仓", (pos or {}).get("side") or ""))
    if qty:
        entry = float(pos.get("entry") or 0.0)
        liq = float(pos.get("liq_price") or 0.0)
        upnl = float(pos.get("unrealized") or 0.0)
        print("    开仓价 %.2f｜浮动盈亏 %+.2fU" % (entry, upnl))
        if liq:
            print("    爆仓价 %.2f（距现价 %+.2f%%）"
                  % (liq, (liq - px) / px * 100.0 if px else 0.0))

    # ---- 3. 保证金 ----
    print()
    print("③ 资金：权益 %.2fU｜可用 %.2fU｜挂单占用 %.2fU"
          % (float(eq.get("equity") or 0.0),
             float(eq.get("available_balance") or 0.0),
             float(eq.get("open_order_margin") or 0.0)))

    # ---- 4. 预期状态机 ----
    print()
    print("④ 预期（非终态）：")
    rows = _ro(PEND, "SELECT thesis_id, state, next_index, armed_at_bar,"
                     " armed_at_sub_ts FROM pending_theses"
                     " WHERE state IN ('EXPECTED','ARMED','FILLED')"
                     " ORDER BY updated_ts DESC LIMIT 4")
    if not rows or not isinstance(rows[0][0], str):
        print("    （没有在管的预期）")
    for r in rows:
        if not isinstance(r[0], str):
            print("    %s" % (r[0],))
            continue
        print("    %-9s %s" % (r[1], r[0][:56]))
        print("              next=%s armed_bar=%s armed_sub=%s"
              % (r[2], r[3], r[4]))

    # ---- 5. 最近决策 ----
    print()
    print("⑤ 最近 3 条决策：")
    for r in _ro(TRADE, "SELECT ts, reason FROM decisions"
                        " ORDER BY id DESC LIMIT 3"):
        if len(r) == 2:
            print("    %-12s %s" % (_ago(r[0]), str(r[1])[:80]))

    # ---- 6. 风险事件（近 2 小时）----
    print()
    ev = _ro(TRADE, "SELECT ts, kind FROM risk_events WHERE ts >= ?"
                    " ORDER BY id DESC LIMIT 6",
             (int(time.time() * 1000) - 7200000,))
    print("⑥ 近 2 小时风险事件：%s" % (len(ev) if ev and len(ev[0]) == 2
                                       else "无"))
    for r in ev:
        if len(r) == 2:
            print("    %-12s %s" % (_ago(r[0]), r[1]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", type=int, default=0,
                    help="每 N 秒重画一次（0 = 只画一次）")
    a = ap.parse_args()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    while True:
        try:
            oo, px, pos, eq = loop.run_until_complete(snapshot())
            draw(oo, px, pos, eq)
        except Exception as e:                               # noqa: BLE001
            print("！快照失败：%s" % e)
        if not a.loop:
            break
        try:
            time.sleep(a.loop)
        except KeyboardInterrupt:
            break


if __name__ == "__main__":
    main()
