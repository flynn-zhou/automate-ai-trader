# -*- coding: utf-8 -*-
import sqlite3
c = sqlite3.connect('/home/cryptobot/ai-trader/data/backtest.db')
for iv, n, mn, mx in c.execute("SELECT interval, COUNT(*), MIN(open_time), MAX(open_time) FROM klines GROUP BY interval"):
    print('%-6s %8d' % (iv, n))