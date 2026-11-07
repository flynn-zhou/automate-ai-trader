# -*- coding: utf-8 -*-
"""risk_pct 到底有没有用？扫一遍看实际仓位变不变。"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ai_trader.config import TradingCfg
from ai_trader.trading import risk as risk_mod

EQUITY = 71.77
ENTRY = 77740.0

def run(risk_pct, stop_pct, max_lev, hyst=0.0):
    cfg = TradingCfg()
    cfg.risk_pct = risk_pct
    cfg.max_leverage = max_lev
    cfg.exec_market = "spot"
    cfg.qty_step = 0.00001
    cfg.min_notional = 5.0
    feats = {"atr3s_pct": 0.02}
    plan = risk_mod.plan(feats, EQUITY, ENTRY, "buy",
                         stop_pct=stop_pct, tp_pct=stop_pct * 3,
                         cfg=cfg, sigma3=0.02)
    return plan

print('=== 趋势策略：止损 25%%，杠杆上限 1x，扫 risk_pct ===')
print('%-10s %10s %10s %10s' % ('risk_pct', 'qty', '名义U', '杠杆'))
for r in (5, 10, 20, 25, 30, 35, 50, 80, 100):
    p = run(r, 25.0, 1.0)
    print('%-10d %10.5f %10.2f %9.3fx' % (r, p.qty, p.notional, p.leverage))

print()
print('=== 同样扫 risk_pct，但止损放宽到 50%% ===')
print('%-10s %10s %10s %10s' % ('risk_pct', 'qty', '名义U', '杠杆'))
for r in (5, 20, 35, 50, 60):
    p = run(r, 50.0, 1.0)
    print('%-10d %10.5f %10.2f %9.3fx' % (r, p.qty, p.notional, p.leverage))

print()
print('=== 剥头皮式：止损 0.5%%，杠杆上限 150x，扫 risk_pct ===')
print('%-10s %10s %10s %10s' % ('risk_pct', 'qty', '名义U', '杠杆'))
for r in (5, 20, 35, 50, 100):
    p = run(r, 0.5, 150.0)
    print('%-10d %10.6f %10.2f %9.3fx' % (r, p.qty, p.notional, p.leverage))
