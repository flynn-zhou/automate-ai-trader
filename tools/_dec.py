# -*- coding: utf-8 -*-
import sqlite3, time
now = int(time.time() * 1000)
c = sqlite3.connect('/home/cryptobot/ai-trader/data/market_trade.db')
print('=== 最近 8 条决策 ===')
for r in c.execute('SELECT ts,signal,action,substr(reason,1,60) FROM decisions ORDER BY ts DESC LIMIT 8'):
    print('  %s  %-14s %-5s %s' % (time.strftime('%m-%d %H:%M', time.localtime(r[0]/1000)), r[1], r[2], r[3]))
print()
print('=== 策略时代起点 ===')
for r in c.execute("SELECT MIN(ts) FROM decisions WHERE signal LIKE 'trend%'"):
    print('  trend 首条决策:', time.strftime('%Y-%m-%d %H:%M', time.localtime(r[0]/1000)) if r[0] else '无')
print()
print('=== 决策里出现过的 signal ===')
for r in c.execute('SELECT signal, COUNT(*) FROM decisions GROUP BY signal ORDER BY 2 DESC'):
    print('  %-16s %d' % (r[0], r[1]))