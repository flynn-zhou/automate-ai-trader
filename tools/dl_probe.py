import sqlite3, os, time
p = "data/backtest.db"
if os.path.exists(p):
    c = sqlite3.connect(p)
    try:
        n = c.execute("SELECT COUNT(*) FROM klines").fetchone()[0]
        r = c.execute("SELECT MIN(open_time), MAX(open_time) FROM klines").fetchone()
        print("rows:", n, "from", time.strftime("%Y-%m-%d", time.localtime(r[0]/1000)) if r[0] else "-",
              "to", time.strftime("%Y-%m-%d", time.localtime(r[1]/1000)) if r[1] else "-")
    except Exception as e:
        print("err:", e)
else:
    print("no db yet", os.path.abspath(p))
