# -*- coding: utf-8 -*-
"""把实盘峰值基线重置为「注资后的真实起始权益」。

为什么需要它
------------
2026-11-03 跑过约 14 分钟 live，账户 125.008778U，一笔没做，之后钱被提走。
切回 live 时 Guard 按 paper=0 取到 125.0088 当基准，而当时权益 0.007U ——
回撤算出 -99.99%，立刻触发 60% 硬停，还要人工 reset。
就算注资回 100U，也凭空带着 -20% 回撤，60% 的预算只剩 40 点。

那 125.0088 本身没错，错在它属于【另一笔已经撤走的资金】。

做法
----
不删任何历史行。给 guard_state 记一个 baseline_ts，
峰值只从此刻往后算，旧数据原样保留、可追溯。
同时清掉硬停（reset），因为硬停本来就是按旧峰值算出来的。

用法
----
    python tools/reset_live_baseline.py            # 干跑，只打印将要发生什么
    python tools/reset_live_baseline.py --apply    # 真正写库

安全约束
----------
  - 默认干跑，必须显式 --apply；
  - 只往后推起点，绝不改 equity 表的任何一行；
  - 写库前把旧值打印出来，便于事后对账；
  - 凭证明文不打印。
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.config import load  # noqa: E402
from ai_trader.trade_db import TradeDB  # noqa: E402


def main() -> int:
    apply = "--apply" in sys.argv
    cfg = load()
    mode = cfg.trading.mode
    strat = cfg.trading.strategy
    paper = 0 if mode == "live" else 1

    # 交易库路径和 main.py 必须是同一个推导规则，写死或写错都会读到别的库，
    # 而那样"没找到记录"看起来和"本来就没有"一模一样。
    trade_path = cfg.store.path.replace(".db", "") + "_trade.db"
    tdb = TradeDB(trade_path)
    tdb.start()

    gs = tdb.load_guard_state() or {}
    old_baseline = int(gs.get("baseline_ts") or 0)
    old_peak = tdb.peak_equity(strat, paper, old_baseline)
    all_peak = tdb.peak_equity(strat, paper, 0)

    now = int(time.time() * 1000)
    cur = tdb.equity_curve(1)
    cur_eq = cur[0]["equity"] if cur else 0.0

    print("=" * 66)
    print(" 实盘峰值基线重置   模式=%s 策略=%s 账本=%s"
          % (mode, strat, "实盘 paper=0" if paper == 0 else "纸面 paper=1"))
    print("=" * 66)
    print("  当前生效基线 ts : %s" % (old_baseline or "无（全历史）"))
    print("  当前生效峰值     : %.6f" % old_peak)
    print("  全历史峰值       : %.6f" % all_peak)
    print("  库内最新权益     : %.6f" % cur_eq)
    day_base = float(gs.get("day_start_equity") or 0.0)
    print("  当日基准权益     : %.6f" % day_base)
    print("  旧硬停           : %s" % ("是" if gs.get("halted_hard") else "否"))
    if gs.get("halt_reason"):
        print("  硬停原因         : %s" % gs["halt_reason"])
    print("")
    print("  执行后将写入 baseline_ts = %d（现在）" % now)
    print("  同时把当日基准权益重置为当前权益 %.6f" % cur_eq)
    print("    理由：本金断层不是当日亏损。若不重置，日亏损闸会按")
    print("    %.2f -> %.6f 算出 -100%% 并继续停到明天。" % (day_base, cur_eq))
    print("  含义：峰值只从此刻往后算，%s 之前的历史行原样保留但不计入"
          % time.strftime("%Y-%m-%d %H:%M", time.localtime(now / 1000)))
    print("")

    if not apply:
        print("  [干跑] 没有写任何东西。确认无误后加 --apply")
        print("=" * 66)
        return 0

    # 保住其他字段，只动 baseline_ts
    tdb.save_guard_state({
        "halted_hard": False,
        "halt_reason": "",
        "halt_kind": "",
        "halt_ts": 0,
        "day": gs.get("day") or "",
        # 当日基准必须跟着重置。
        # 不重置的后果实测过：峰值解决了，日亏损闸还在按
        # "基准 100U -> 现在 0.0071U = 今天亏了 100%" 继续硬停，
        # 一直停到明天 date roll 才解 —— 看起来像"改了没用"。
        # 本金断层（提款/注资）不是当日亏损，不该计入日亏损闸。
        "day_start_equity": cur_eq,
        "daily_halt_day": "",
        "close_fail_streak": int(gs.get("close_fail_streak") or 0),
        "baseline_ts": now,
    })
    time.sleep(1.0)   # 单写队列是异步的，等它落盘

    gs2 = tdb.load_guard_state() or {}
    new_peak = tdb.peak_equity(strat, paper, int(gs2.get("baseline_ts") or 0))
    print("  写入完成：")
    print("    baseline_ts = %d" % int(gs2.get("baseline_ts") or 0))
    print("    新峰值       = %.6f" % new_peak)
    print("    当日基准     = %.6f" % float(gs2.get("day_start_equity") or 0.0))
    print("    硬停         = %s" % ("是" if gs2.get("halted_hard") else "否"))
    print("")
    print("  提示：GUI/服务进程内存里还持有旧的 Guard 状态。")
    print("       请重启服务使新的基线生效。")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())