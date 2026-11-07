# -*- coding: utf-8 -*-
"""线上实际生效的滞回带，与"如果 env 漏了这一行"会变成什么。"""
import os, sys
sys.path.insert(0, '/home/cryptobot/ai-trader')
from ai_trader.config import load as load_cfg
from ai_trader.trading.trend import TrendStrategy
from ai_trader.trading.wait_stats import CALIBRATED_HYST_PCT

cfg = load_cfg()
tg = TrendStrategy(cfg.trading, symbol=cfg.market.symbol,
                   fapi_base=cfg.market.fapi_base, proxy=cfg.market.http_proxy,
                   exec_market=cfg.trading.exec_market)
print('AI_TREND_HYST 在环境里:', repr(os.environ.get('AI_TREND_HYST')))
print('config 解析出的 pct   : %.4f' % cfg.trading.trend_hysteresis_pct)
print('策略实际用的 h        : %.6f（= %.4f%%）' % (tg.hyst, tg.hyst * 100))
print('标定值 CALIBRATED     : %.4f%%' % CALIBRATED_HYST_PCT)
ok = abs(tg.hyst * 100 - CALIBRATED_HYST_PCT) < 1e-9
print('线上是否跑在标定档    :', '是' if ok else '否')
print()

# 模拟漏配：去掉环境变量后，默认值给出什么
os.environ.pop('AI_TREND_HYST', None)
cfg2 = load_cfg()
tg2 = TrendStrategy(cfg2.trading, symbol=cfg2.market.symbol,
                    fapi_base=cfg2.market.fapi_base, proxy=cfg2.market.http_proxy,
                    exec_market=cfg2.trading.exec_market)
print('【漏配场景】config 默认 pct: %.4f' % cfg2.trading.trend_hysteresis_pct)
print('【漏配场景】策略会用的 h  : %.6f（= %.4f%%）' % (tg2.hyst, tg2.hyst * 100))
print('【漏配场景】是否仍为标定档:', '是（本轮修复生效）' if abs(tg2.hyst*100 - CALIBRATED_HYST_PCT) < 1e-9 else '否（仍在跑被否决的配置）')
