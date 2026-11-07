# -*- coding: utf-8 -*-
"""同一条机制，只改滞回带：看线上那 0.2% 到底吃掉多少边际。

为什么必须比：
  tools/liveedge.py 是产出头条收益数字的工具，它的入口是
      if C[i] > s and C[i-1] <= sp        # 没有滞回带
  而线上 TrendStrategy 是
      进：cl[-1] > sma*(1+h)
      出：cl[-1] <= sma*(1-h)
  两套规则不同。对只做多的趋势跟随，滞回带意味着买得更晚更高、
  卖得更晚更低 —— 方向上是【不利】的，所以头条数字可能偏高。

这里把两条规则放在同一份数据、同一套成本/止损上跑，量化差额。
"""
import datetime, sqlite3

DB, NL, SP, COST = 'data/backtest.db', 200, 25.0, 0.0010
c = sqlite3.connect(DB)
rows = c.execute("SELECT open_time,h,l,c FROM klines WHERE interval='4h'"
                 " ORDER BY open_time").fetchall()
c.close()
rows = [r for r in rows if r[3] and r[3] > 0]
T = [r[0] for r in rows]; H = [r[1] for r in rows]
L = [r[2] for r in rows]; C = [r[3] for r in rows]
N = len(rows)
yr = [datetime.datetime.utcfromtimestamp(t / 1000).year for t in T]
yrs = (T[-1] - T[0]) / (365.25 * 86400000)


def run(h):
    """h = 滞回带（小数）。h=0 就是 liveedge.py 的现行规则。"""
    cash, qty, pos, entry, stop = 1.0, 0.0, False, 0.0, 0.0
    mtm, trades, stopped, bars, fees = [], 0, 0, 0, 0.0
    worst_mae = 0.0
    for i in range(N):
        if i >= NL + 1:
            s = sum(C[i - NL:i]) / NL
            sp = sum(C[i - 1 - NL:i - 1]) / NL
            if not pos:
                if C[i] > s * (1 + h) and C[i - 1] <= sp * (1 + h):
                    pos = True; entry = C[i]; stop = entry * (1 - SP / 100.0)
                    f = cash * COST; cash -= f; fees += f
                    qty = (cash * (1 - COST)) / entry
                    cash -= qty * entry; trades += 1
            else:
                bars += 1
                mae = (entry - L[i]) / entry * 100
                worst_mae = max(worst_mae, mae)
                if L[i] <= stop:
                    px = stop
                    cash += qty * px; f = qty * px * COST; cash -= f; fees += f
                    qty = 0.0; pos = False; stopped += 1
                elif C[i] <= s * (1 - h):
                    px = C[i]
                    cash += qty * px; f = qty * px * COST; cash -= f; fees += f
                    qty = 0.0; pos = False
        mtm.append(cash + qty * C[i])
    return dict(mtm=mtm, trades=trades, stopped=stopped, bars=bars,
                fees=fees, mae=worst_mae, final=mtm[-1])


def mdd(cur):
    pk = cur[0]; w = 0.0
    for v in cur:
        pk = max(pk, v); w = max(w, (pk - v) / pk)
    return w * 100


bhc = [C[i] / C[0] for i in range(N)]
print('数据 %d 根 4h  %.2f 年  (%d -> %d)' % (N, yrs, yr[0], yr[-1]))
print()
print('%-10s %8s %8s %9s %7s %8s %9s' % ('滞回带', '总收益', 'CAGR', '最大回撤',
                                          '笔数', '止损次数', '累计手续费'))
print('-' * 74)
res = {}
for h in (0.0, 0.001, 0.002, 0.005, 0.010, 0.020):
    r = run(h)
    res[h] = r
    dd = mdd(r['mtm'])
    cagr = (r['final'] ** (1 / yrs) - 1) * 100
    print('%-10s %+7.0f%% %+7.1f%% %8.1f%% %7d %8d %8.1f%%'
          % (('%g%%' % (h * 100)) if h else '0 (线上回测)',
             (r['final'] - 1) * 100, cagr, dd, r['trades'], r['stopped'],
             r['fees'] * 100))
print('-' * 74)
b_dd = mdd(bhc)
print('%-10s %+7.0f%% %+7.1f%% %8.1f%%' % ('买入持有', (bhc[-1] - 1) * 100,
                                           (bhc[-1] ** (1 / yrs) - 1) * 100, b_dd))
print()
base = res[0.0]
live = res[0.002]
print('线上规则(0.2%%) vs 回测工具(0%%):')
print('  总收益 %+.0f%% -> %+.0f%%   差 %+.1f 个百分点'
      % ((base['final'] - 1) * 100, (live['final'] - 1) * 100,
         (live['final'] - base['final']) * 100))
print('  笔数   %d -> %d' % (base['trades'], live['trades']))
print('  回撤   %.1f%% -> %.1f%%' % (mdd(base['mtm']), mdd(live['mtm'])))
print('  100U   %.0fU -> %.0fU' % (100 * base['final'], 100 * live['final']))
