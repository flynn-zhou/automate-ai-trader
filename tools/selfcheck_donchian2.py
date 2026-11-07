# -*- coding: utf-8 -*-
"""重放加速版：不逐根重建列表，只按下标切片算通道，和回测逐笔对齐。"""
import os, sys, sqlite3, time
sys.path.insert(0, "/home/cryptobot/ai-trader")
from ai_trader.config import load
from ai_trader.trading.donchian import DonchianStrategy

os.environ["AI_STRATEGY"] = "donchian"
cfg = load()
s = DonchianStrategy(cfg.trading, symbol="BTCUSDT", fapi_base="https://fapi.binance.com")
N, M = s.entry_n, s.exit_m

c = sqlite3.connect("/home/cryptobot/ai-trader/data/hist.db")
rows = c.execute("select open_time,o,h,l,c from k2 where symbol='BTCUSDT' and interval='1h'"
                 " order by open_time").fetchall()
c.close()
h = [r[2] for r in rows]
l = [r[3] for r in rows]
cl = [r[4] for r in rows]
n = len(rows)

# A: 直接按回测口径重放
pos = 0
ent = []
for i in range(max(N, M), n - 1):
    hi = max(h[i - N:i])
    lo = min(l[i - M:i])
    if pos == 0 and cl[i] > hi:
        pos = 1
        ent.append(i)
    elif pos == 1 and cl[i] < lo:
        pos = 0

# B: 用 DonchianStrategy 的 want_entry/want_exit（只给最近 160 根，模拟线上窗口）
pos2 = 0
ent2 = []
for i in range(max(N, M), n - 1):
    s.bars = [{"t": rows[j][0], "o": rows[j][1], "h": h[j],
               "l": l[j], "c": cl[j], "v": 1.0} for j in range(i - 155, i + 1)]
    if pos2 == 0:
        if s.want_entry(cl[i]):
            pos2 = 1
            ent2.append(i)
    else:
        if s.want_exit(cl[i]):
            pos2 = 0

print("回测口径（直接算通道） 开仓 %d 次" % len(ent))
print("策略口径（want_entry） 开仓 %d 次" % len(ent2))
print("完全一致：%s" % ("是" if ent == ent2 else "**否**"))
if ent != ent2:
    a, b = set(ent), set(ent2)
    print("  只在回测里：%s" % sorted(a - b)[:5])
    print("  只在策略里：%s" % sorted(b - a)[:5])
yrs = (rows[-1][0] - rows[0][0]) / (365.25 * 24 * 3600 * 1000)
print("跨度 %.2f 年 -> %.1f 笔/年（回测 17.9）" % (yrs, len(ent) / yrs))
