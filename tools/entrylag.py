# -*- coding: utf-8 -*-
"""4h 收盘后价格漂多远 —— 回测「按收盘价成交」有多乐观。

为什么量（第 96 轮）：
  回测里进场价就是 4h 收盘价，0 延迟。实盘要等官方 K 线落地、算信号、
  过闸、下市价单、等成交，是秒到分钟级。回测假设 0 延迟。
  这个 0 延迟假设值多少钱？

数据分工（两边互补，都不能单独下结论）：
  服务器：1h K 线 60000 根，覆盖 6.8 年 —— 能测「延迟 1 小时」的上界，样本大
  本地  ：1m K 线但只有 2026-07~2026-11 —— 能测真实的分钟级延迟，样本小

  1 小时漂移不是真实延迟，但它是【上界】：真实延迟远小于 1 小时。
  如果连 1 小时漂移都很小，那秒级延迟更不用担心。
"""
import bisect
import sqlite3
import statistics
import sys

DB = sys.argv[1] if len(sys.argv) > 1 else '/home/cryptobot/ai-trader/data/backtest.db'
LAG_IV = sys.argv[2] if len(sys.argv) > 2 else '1h'
H4 = 4 * 3600 * 1000

c = sqlite3.connect(DB)
h4 = dict(c.execute("SELECT open_time, c FROM klines WHERE interval='4h'"
                    " ORDER BY open_time"))
sub = sorted(c.execute("SELECT open_time, c FROM klines WHERE interval=?"
                       " ORDER BY open_time", (LAG_IV,)))
c.close()
print('库 %s｜4h %d 根｜%s %d 根' % (DB, len(h4), LAG_IV, len(sub)))
if not sub:
    print('  没有 %s 数据，跳过' % LAG_IV)
    raise SystemExit

starts = [r[0] for r in sub]
closes = [r[1] for r in sub]


TOL = 5 * 60 * 1000      # 容忍 5 分钟


def price_at(ts):
    """ts 之后第一根，但必须【紧邻】——否则返回 None。

    ★ 这里踩过一次大坑：本地 1m 只覆盖 2026-07~11，而 4h 从 2020 开始。
      不设容忍度的话，2020 年的收盘点会被 bisect 对到 2026 年的第一根 1m，
      于是「1 分钟后价格」算出来是 +52% —— 一个荒谬但不会报错的数字。
      两段数据覆盖区间不同，必须靠容忍度挡住，不能靠调用方记得。
    """
    i = bisect.bisect_left(starts, ts)
    if i >= len(starts):
        return None
    if starts[i] - ts > TOL:        # 离得太远，说明这段没数据
        return None
    return closes[i]


diffs = []
for t, px in sorted(h4.items()):
    if not px or px <= 0:
        continue
    p2 = price_at(t + H4)
    if p2 is None or p2 <= 0:
        continue
    diffs.append((p2 - px) / px * 100.0)

n = len(diffs)
if n < 5:
    print('  样本太少（%d），不下结论' % n)
    raise SystemExit
v = sorted(diffs)
mean = statistics.mean(v)
med = statistics.median(v)
q = lambda p: v[int(p * (n - 1))]
up = sum(1 for x in v if x > 0) / n * 100
print('  收盘后 %s 的价格漂移（n=%d）' % (LAG_IV, n))
print('    均值 %+.3f%%  中位 %+.3f%%  P10 %+.3f%%  P90 %+.3f%%  最差 %+.3f%%'
      % (mean, med, q(0.1), q(0.9), max(v)))
print('    上涨占比 %.1f%%  （进场是买入，上涨=买贵了）' % up)
if n >= 30:
    # 对权益的影响：每次进场少赚/多赚这个比例，147 笔累积
    print()
    print('    若每次进场都经历这个漂移，147 笔累计影响（对数近似）：')
    print('      中位情形 %.2f%%   均值情形 %.2f%%'
          % ((1 + med / 100) ** 147 * 100 - 100,
             (1 + mean / 100) ** 147 * 100 - 100))
    print('      （不报「最差情形」：把单笔 12%% 当成每笔都发生、还复利 147 次，')
    print('        那不是压力测试，那是拿一个没有意义的数吓人。）')