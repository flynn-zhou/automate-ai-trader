# -*- coding: utf-8 -*-
"""值守进程（独立于交易主进程）：每 N 分钟巡检一次，异常推送，每天一份值班报告。

为什么独立：
- 主进程挂了，如果巡检也跑在主进程里，就没人报警了。
- 这跟 guard 的分工不同：guard 在主进程里保护资金，sentinel 在外面看着主进程。

用法：
    python -m ai_trader.sentinel_daemon                 # 常驻（systemd 里跑这个）
    python -m ai_trader.sentinel_daemon --once          # 跑一次就退出（cron / 调试）
    python -m ai_trader.sentinel_daemon --report        # 只出值班报告
"""
from __future__ import annotations

import argparse
import logging
import os
import time

from .guard import feishu_notifier
from .journal import Journal
from .sentinel import Sentinel


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--interval", type=int, default=300)
    ap.add_argument("--trade-db", default="")
    ap.add_argument("--market-db", default="")
    a = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-5s %(name)s | %(message)s")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    trade_db = a.trade_db or os.path.join(root, "data", "market_trade.db")
    market_db = a.market_db or os.path.join(root, "data", "market.db")

    journal = None
    if os.path.exists(trade_db):
        journal = Journal(trade_db)
        journal.start()
    hook = feishu_notifier(os.environ.get("AI_FEISHU_WEBHOOK", "")
                           or os.environ.get("FEISHU_WEBHOOK_URL", ""))
    s = Sentinel(trade_db, market_db, journal=journal, notify=hook,
                 mode=os.environ.get("AI_TRADE_MODE", "paper"))

    try:
        if a.report:
            s.daily_report()
            return
        if a.once:
            r = s.run_once()
            print(("OK" if r["ok"] else "ISSUES: " + "；".join(r["issues"])))
            return
        logging.info("值守已启动，间隔 %ds，交易日库=%s", a.interval, trade_db)
        while True:
            try:
                s.run_once()
                # 每天 09:00 出一份值班报告（本地时间）
                day = time.strftime("%Y-%m-%d")
                if time.strftime("%H:%M") == "09:00" and s.st.last_report_day != day:
                    s.daily_report()
                    s.st.last_report_day = day
            except Exception:
                logging.error("值守巡检异常", exc_info=True)
            time.sleep(a.interval)
    finally:
        if journal is not None:
            journal.stop()


if __name__ == "__main__":
    main()
