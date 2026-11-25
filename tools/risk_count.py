# -*- coding: utf-8 -*-
"""近 1 小时风险事件计数 + 保护单失败次数（只读）。"""
import sqlite3
import time

DB = "data/market_trade.db"
c = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
try:
    since = int(time.time() * 1000) - 3600 * 1000
    print("近 1 小时风险事件（按类型）：")
    for kind, n in c.execute(
            "select kind, count(*) from risk_events where ts>=? "
            "group by kind order by 2 desc", (since,)):
        print("  %-34s %d" % (kind, n))
    print("\n最近 5 条：")
    for ts, kind, detail in c.execute(
            "select ts, kind, substr(detail,1,90) from risk_events "
            "order by ts desc limit 5"):
        print("  %s  %-30s %s"
              % (time.strftime("%H:%M:%S", time.localtime(ts / 1000.0)),
                 kind, detail))
finally:
    c.close()
