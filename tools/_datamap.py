# -*- coding: utf-8 -*-
import sqlite3, os, time
D = "/home/cryptobot/ai-trader/data"
for f in ("hist.db", "market.db", "market_trade.db", "backtest.db", "spot.db"):
    p = os.path.join(D, f)
    if not os.path.exists(p):
        continue
    c = sqlite3.connect(p)
    tabs = [r[0] for r in c.execute("select name from sqlite_master where type='table'")]
    print("== %s  %.1f MB  表: %s" % (f, os.path.getsize(p) / 1048576.0, ", ".join(tabs)))
    for t in tabs:
        try:
            n = c.execute("select count(*) from %s" % t).fetchone()[0]
            print("     %-16s %d 行" % (t, n))
        except Exception as e:
            print("     %-16s ?" % t)
    # 覆盖范围
    for tab, col in (("k2", "open_time"), ("bars", "open_time"), ("decisions", "ts"), ("trades", "ts")):
        if tab in tabs:
            try:
                lo, hi = c.execute("select min(%s), max(%s) from %s" % (col, col, tab)).fetchone()
                if lo:
                    print("     %s 范围: %s ~ %s" % (tab,
                          time.strftime("%Y-%m-%d %H:%M", time.gmtime(lo / 1000)),
                          time.strftime("%Y-%m-%d %H:%M", time.gmtime(hi / 1000))))
            except Exception:
                pass
    if "k2" in tabs:
        try:
            rows = c.execute("select symbol, interval, count(*) from k2 group by symbol, interval order by symbol, interval").fetchall()
            print("     k2 明细: " + "  ".join("%s/%s:%d" % (a, b, n) for a, b, n in rows))
        except Exception:
            pass
    c.close()
    print()
