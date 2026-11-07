# -*- coding: utf-8 -*-
"""上线前自检：唐奇安在服务器上用真实历史跑一遍，和回测数字对得上吗。"""
import os, sys, sqlite3, time
sys.path.insert(0, "/home/cryptobot/ai-trader")

from ai_trader.config import load, INTERVALS_MS, INTERVAL_NAMES
from ai_trader.trading.donchian import DonchianStrategy

os.environ["AI_STRATEGY"] = "donchian"
cfg = load()
print("strategy           =", cfg.trading.strategy)
print("donchian_entry/exit=", cfg.trading.donchian_entry, cfg.trading.donchian_exit)
print("intervals          =", [(iv, INTERVAL_NAMES[iv]) for iv in INTERVALS_MS])

s = DonchianStrategy(cfg.trading, symbol="BTCUSDT", fapi_base="https://fapi.binance.com",
                     proxy="", exec_market="perp")
print("iv(ms)             =", s.iv, "->", s.interval_name())

# 用 hist.db 的真实 1h K 灌进策略，逐根重放，和回测比笔数
c = sqlite3.connect("/home/cryptobot/ai-trader/data/hist.db")
rows = c.execute("select open_time,o,h,l,c from k2 where symbol='BTCUSDT' and interval='1h'"
                 " order by open_time").fetchall()
c.close()
print("hist 1h bars       =", len(rows))

s.bars = [{"t": r[0], "o": r[1], "h": r[2], "l": r[3], "c": r[4], "v": 1.0} for r in rows]
s.seeded = True
s.need_reconcile = False
s.last_eval_bar = s.bars[-1]["t"]

st = s.state(rows[-1][4])
print("ready              =", st["ready"], "bars =", st["bars"])
print("upper/lower        =", round(st["upper"], 1), round(st["lower"], 1))
print("above_upper        =", st["above_upper"], " below_lower =", st["below_lower"])
print("dist_to_entry_pct  = %.2f%%" % st["dist_to_entry_pct"])
print("next_bar_eta_h     = %.2f" % s.next_bar_eta_h())
roll = s.roll_forward_to_break(rows[-1][4])
print("roll_forward       =", roll)

# 逐根重放：数出策略在这 6.2 年里的开平仓事件次数，必须接近回测的 128 笔
n_entry = n_exit = 0
pos = 0
for i in range(len(rows)):
    s.bars = [{"t": r[0], "o": r[1], "h": r[2], "l": r[3], "c": r[4], "v": 1.0}
              for r in rows[:i + 1]]
    if len(s.bars) < 152:
        continue
    px = rows[i][4]
    if pos == 0:
        if s.want_entry(px):
            n_entry += 1
            pos = 1
    else:
        if s.want_exit(px):
            n_exit += 1
            pos = 0
print("重放开仓 %d 次 / 平仓 %d 次（回测 128 笔）" % (n_entry, n_exit))
