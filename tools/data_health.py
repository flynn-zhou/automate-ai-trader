# -*- coding: utf-8 -*-
"""回测数据体检：缺口、重复、对齐。

为什么必须先查这个：SMA200 是 200 根 4h K 线的均值。
如果数据有缺口，200 根覆盖的实际时间就不是 33 天而是更长——
SMA 的含义变了，信号跟着变，但回测照样能跑出漂亮数字。
"""
import sqlite3, sys
from datetime import datetime, timezone

IV = 14400000
c = sqlite3.connect("data/backtest.db")

def fmt(ms):
    return datetime.fromtimestamp(ms/1000, timezone.utc).strftime("%Y-%m-%d %H:%M")

ts = [r[0] for r in c.execute(
    "SELECT open_time FROM klines WHERE interval='4h' ORDER BY open_time")]
print("4h K 线数量: %d" % len(ts))
if not ts:
    sys.exit("没有 4h 数据")

print("首根: %s UTC" % fmt(ts[0]))
print("末根: %s UTC" % fmt(ts[-1]))
span_days = (ts[-1]-ts[0])/86400000
print("时间跨度: %.1f 天 (%.2f 年)" % (span_days, span_days/365))

mis = [t for t in ts if t % IV != 0]
print("未对齐到 4h 边界: %d 根" % len(mis))
if mis[:3]:
    print("   例:", [fmt(t) for t in mis[:3]])

print("重复时间戳: %d" % (len(ts) - len(set(ts))))

gaps = [(a, b-a) for a, b in zip(ts, ts[1:]) if b-a != IV]
print("不等于 4h 的间隔: %d 处" % len(gaps))
for a, d in gaps[:10]:
    print("   %s UTC 之后缺 %.1f 小时" % (fmt(a), d/3600000 - 4))

expect = int(round((ts[-1]-ts[0])/IV)) + 1
missing = expect-len(ts)
print("按跨度应有 %d 根，实际 %d 根，缺 %d (%.2f%%)"
      % (expect, len(ts), missing, missing/expect*100))

now_ms = int(datetime.now(timezone.utc).timestamp()*1000)
print("最后一根是否已收盘: %s" % ("是" if ts[-1] + IV <= now_ms else "否（未收盘，会被回测当成已实现）"))
