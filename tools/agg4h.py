# -*- coding: utf-8 -*-
"""把 1h K 线聚合成 4h，供趋势策略回测使用。

为什么单独做：download_klines.py 只支持到 1h，
而趋势策略用的是 4h SMA200。聚合规则必须和实盘 feed 一致：
  open   = 第一根 1h 的 open
  high   = 四根的最大
  low    = 四根的最小
  close  = 最后一根的 close
  volume = 四根之和
  open_time = 第一根 1h 的 open_time（对齐到 4h 边界）
"""
import sqlite3, sys, os

DB = sys.argv[1] if len(sys.argv) > 1 else "data/backtest.db"
IV4H = 14400000

c = sqlite3.connect(DB)
c.execute("""CREATE TABLE IF NOT EXISTS klines(
    interval TEXT NOT NULL, open_time INTEGER NOT NULL,
    o REAL, h REAL, l REAL, c REAL, v REAL, q REAL, n INTEGER,
    b REAL, s REAL, PRIMARY KEY(interval, open_time))""")

rows = list(c.execute(
    "SELECT open_time,o,h,l,c,v,q,n,b,s FROM klines WHERE interval='1h' "
    "ORDER BY open_time"))
if not rows:
    print("没有 1h 数据")
    sys.exit(1)

# 按 4h 边界分组
buckets = {}
for r in rows:
    t = int(r[0])
    key = (t // IV4H) * IV4H
    buckets.setdefault(key, []).append(r)

out = []
for k in sorted(buckets):
    g = buckets[k]
    # 只取完整桶（4 根 1h），最后不完整的丢弃——避免用半根 K 线算收盘价
    if len(g) != 4:
        continue
    o = g[0][1]; h = max(x[2] for x in g); l = min(x[3] for x in g)
    cl = g[-1][4]; v = sum(x[5] for x in g); q = sum(x[6] for x in g)
    n = sum(x[7] for x in g); b = sum(x[8] for x in g); s = sum(x[9] for x in g)
    out.append((k, o, h, l, cl, v, q, n, b, s))

print("1h 根数 %d -> 完整 4h 根数 %d" % (len(rows), len(out)))
if out:
    import datetime
    f = lambda ms: datetime.datetime.utcfromtimestamp(ms/1000).strftime("%Y-%m-%d %H:%M")
    print("时间范围 %s ~ %s" % (f(out[0][0]), f(out[-1][0])))

c.executemany(
    "INSERT OR REPLACE INTO klines VALUES('4h',?,?,?,?,?,?,?,?,?,?)", out)
c.commit()
print("已写入 4h 桶 %d 根" % len(out))
c.close()
