# -*- coding: utf-8 -*-
"""同一个 SMA200 趋势规则，喂现货 4h 和合约 4h，结果差多少？

为什么要问：
  我们交易现货（exec_market=spot），但实时行情流是合约（feed.py 写死 fapi）。
  第 98 轮把【历史种子】改成了现货，却只改了一半——
  于是策略的 SMA200 = 现货历史 + 合约实时，是个混合序列。
  决定怎么修之前，先量清楚「市场选错」本身有多大代价。
"""
import json
import urllib.request

def fetch(base, path, start_ms, end_ms):
    got = []
    cur = start_ms
    while cur < end_ms:
        url = '%s%s?symbol=BTCUSDT&interval=4h&startTime=%d&endTime=%d&limit=1000' % (
            base, path, cur, end_ms)
        rows = json.loads(urllib.request.urlopen(
            urllib.request.Request(url, headers={'User-Agent': 'v'}), timeout=25).read())
        if not rows:
            break
        for x in rows:
            got.append((int(x[0]), float(x[1]), float(x[2]), float(x[3]),
                        float(x[4]), float(x[5])))
        nxt = int(rows[-1][0]) + 14400000
        if nxt <= cur:
            break
        cur = nxt
        if len(rows) < 1000:
            break
    return got

def run(bars, hyst=0.002, lookback=200, fee=0.0010, slip=0.0001):
    """与线上同一条规则：收盘价 > SMA*(1+h) 进，< SMA*(1-h) 出，全仓。"""
    closes = [b[4] for b in bars]
    times = [b[0] for b in bars]
    cash, qty = 100.0, 0.0
    equity_curve = []
    trades = 0
    entry_px = 0.0
    for i in range(lookback, len(closes)):
        sma = sum(closes[i-lookback:i]) / lookback
        px = closes[i]
        if qty == 0.0:
            if px > sma * (1 + hyst):
                buy_px = px * (1 + slip)
                qty = cash / (buy_px * (1 + fee))
                cash -= qty * buy_px * (1 + fee)
                entry_px = buy_px
                trades += 1
        else:
            if px < sma * (1 - hyst):
                sell_px = px * (1 - slip)
                cash += qty * sell_px * (1 - fee)
                qty = 0.0
        equity_curve.append(cash + qty * px)
    eq = equity_curve[-1] if equity_curve else 100.0
    # 最大回撤
    peak, mdd = 0.0, 0.0
    for e in equity_curve:
        peak = max(peak, e)
        if peak > 0:
            mdd = max(mdd, (peak - e) / peak * 100)
    days = (times[-1] - times[lookback]) / 86400000.0
    years = days / 365.25
    cagr = ((eq / 100.0) ** (1 / years) - 1) * 100 if years > 0 and eq > 0 else -100
    return dict(final=eq, cagr=cagr, mdd=mdd, trades=trades, years=years)

import time
end_ms = int(time.time() * 1000)
end_ms = (end_ms // 14400000) * 14400000      # 对齐到 4h 边界，避开未收盘那根
start_ms = end_ms - 3 * 365 * 86400000

print('拉取 3 年 4h 数据…')
spot = fetch('https://api.binance.com', '/api/v3/klines', start_ms, end_ms)
fut = fetch('https://fapi.binance.com', '/fapi/v1/klines', start_ms, end_ms)
print('现货 %d 根, 合约 %d 根' % (len(spot), len(fut)))
print('现货范围 %d 天, 合约范围 %d 天'
      % ((spot[-1][0]-spot[0][0])/86400000.0, (fut[-1][0]-fut[0][0])/86400000.0))
print()
# 基差统计
fs = {t: c for (t, o, h, l, c, v) in fut}
diffs = []
for (t, o, h, l, c, v) in spot:
    if t in fs:
        diffs.append((fs[t] - c) / c * 100)
if diffs:
    diffs.sort()
    print('合约相对现货的基差（%%）：均值 %+.4f, 中位 %+.4f, P5 %+.4f, P95 %+.4f'
          % (sum(diffs)/len(diffs), diffs[len(diffs)//2],
             diffs[int(len(diffs)*0.05)], diffs[int(len(diffs)*0.95)]))
    neg = sum(1 for d in diffs if d < 0)
    print('  合约低于现货的比例 %.1f%%（共 %d 个样本）' % (neg/len(diffs)*100, len(diffs)))
print()

# 用共同时间段比较，保证公平
common = sorted(set(t for (t, *_r) in spot) & set(fs.keys()))
sp = [b for b in spot if b[0] in set(common)]
fu = [b for b in fut if b[0] in set(common)]
print('共同区间 %d 根' % len(common))
print()
print('%-10s %10s %10s %10s %10s' % ('市场', '末值', 'CAGR', '最大回撤', '交易数'))
for name, bars in (('现货', sp), ('合约', fu)):
    r = run(bars)
    print('%-10s %10.2f %9.2f%% %9.2f%% %10d'
          % (name, r['final'], r['cagr'], r['mdd'], r['trades']))
