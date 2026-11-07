import sqlite3
c = sqlite3.connect('/home/cryptobot/ai-trader/data/backtest.db')
tabs = [r[0] for r in c.execute("select name from sqlite_master where type='table'").fetchall()]
print('tables:', tabs)
for t in tabs:
    cols = [r[1] for r in c.execute('pragma table_info(%s)' % t).fetchall()]
    n = c.execute('select count(*) from %s' % t).fetchone()[0]
    print(' ', t, n, '->', cols)