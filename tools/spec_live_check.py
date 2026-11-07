# -*- coding: utf-8 -*-
"""拿真实币安现货规格，核对线上代码用的那几个常数。

为什么这件事测试做不到（第 112 轮）：
  FakeBinance 里 SPOT_SYMBOL 的 stepSize/minNotional 是我【手抄】的：
      stepSize 0.00001、minNotional 5.0、baseAssetPrecision 8
  所有现货测试都跑在这个副本上。如果真实的币安 BTCUSDT 改了规格，
  测试依然全绿，而线上会用错的步长下单——要么被拒，要么仓位算成 0。

  这和历史教训同型：测试只能证明「代码符合我以为的世界」，
  不能证明「我以为的世界是对的」。所以要向真交易所要一次。

只读 /api/v3/exchangeInfo，不需要密钥，不下单。
"""
import json, os, sys, urllib.request

PROXY = os.environ.get('AI_HTTP_PROXY') or ''
opener = (urllib.request.build_opener(urllib.request.ProxyHandler({'http': PROXY, 'https': PROXY}))
          if PROXY else urllib.request.build_opener())

req = urllib.request.Request('https://api.binance.com/api/v3/exchangeInfo?symbol=BTCUSDT',
                             headers={'User-Agent': 'ai-trader/1.0'})
with opener.open(req, timeout=20) as r:
    d = json.loads(r.read())
s = d['symbols'][0]
f = {x['filterType']: x for x in s['filters']}
lot = f.get('LOT_SIZE') or {}

real = {
  'step': float(lot.get('stepSize') or 0),
  'min_qty': float(lot.get('minQty') or 0),
  'min_notional': float((f.get('NOTIONAL') or {}).get('minNotional') or 0),
  'precision': int(s.get('baseAssetPrecision') or 0),
  'base': s.get('baseAsset'),
  'quote': s.get('quoteAsset'),
}
print('真实币安现货 BTCUSDT 规格：')
for k, v in real.items():
    print('  %-14s %s' % (k, v))

# 线上代码里的假定值（spot.py:160-165 的默认值）与 FakeBinance 副本
assumed = {'step': 0.00001, 'min_qty': 0.00001, 'min_notional': 5.0,
           'precision': 8, 'base': 'BTC', 'quote': 'USDT'}
print()
bad = 0
for k, want in assumed.items():
    got = real[k]
    if k in ('base', 'quote'):
        ok = got == want
    else:
        ok = abs(float(got) - float(want)) < 1e-12
    print('  %-14s 假定 %-12s 真实 %-12s %s' % (k, want, got, 'OK' if ok else '不一致！'))
    if not ok:
        bad += 1
print()
print('结论：' + ('全部一致' if not bad else '%d 项不一致，必须改assumed与FakeBinance' % bad))

# 顺带算一个对 100U 账户最要紧的数：最小可交易权益
px = None
try:
    rq = urllib.request.Request('https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT',
                                headers={'User-Agent': 'ai-trader/1.0'})
    with opener.open(rq, timeout=20) as r2:
        px = float(json.loads(r2.read())['price'])
except Exception as e:
    print('取价失败：%s' % e)
if px:
    one_step = real['step'] * px
    print('当前价 %.2f：一个 step = %.6f BTC = %.4f U' % (px, real['step'], one_step))
    print('  最小名义 %.1fU 需要 %.1f 个 step = %.6f BTC'
          % (real['min_notional'], real['min_notional'] / one_step, real['min_notional'] / px))
    import math
    # one_step 已经是 USDT 价值（step * px），这里不能再乘一次 px。
    # 我自己第一版就乘了两次，算出了 42.8 万 U —— 比没有这个数字更糟：
    # 它会让"100U 到底能不能建仓"这个问题得到一个荒谬的答案。
    steps = math.ceil(real['min_notional'] / one_step)
    min_equity = steps * one_step * 1.003
    print('  一个 step 是 %.4f U，凑够 %.1fU 需要 %d 个 step' %
          (one_step, real['min_notional'], steps))
    print('  最小可交易权益（含 0.3%% 手续费缓冲）≈ %.2f U' % min_equity)
sys.exit(1 if bad else 0)