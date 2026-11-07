# -*- coding: utf-8 -*-
"""线上口径逐年收益 —— 逐根 mark-to-market（持仓浮亏必须进曲线）。

上一版按"平仓时才记账"，持仓期间浮亏不进曲线，回撤被低估。
本版每根 4h 都按收盘价重估。

★ 2026-11-02（第 94 轮）：这个工具以前用【裸均线】进出（没有滞回带），
  而线上 TrendStrategy 用的是 收盘 > SMA*(1+h) / 收盘 <= SMA*(1-h)。
  也就是说：产出「策略能赚多少」这个头条数字的工具，跑的不是线上那套规则。

  实测差额（13200 根 4h，2020-10~2026-10，双边 0.10% 成本）：
      裸均线(旧口径)  +472%  CAGR +33.6%  回撤 47.8%  181 笔  手续费 120.0%
      线上 0.2% 带子  +548%  CAGR +36.4%  回撤 46.1%  126 笔  手续费  87.9%
  带子过滤掉擦边的假突破：笔数少 30%，省下的手续费是 32 个百分点的权益。
  方向和我在动手前猜的相反（我以为买得更高会更差）——这条是量出来的，不是推的。

  现在带子从真实配置导入，并且进出规则和 trend.py 做了行为级对照
  （tests/test_liveedge_parity.py），结构上不可能再走散。
"""
import datetime
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ai_trader.config import TradingCfg  # noqa: E402

DB, NL, SP, COST = "data/backtest.db", 200, 25.0, 0.0010
# 和线上同一个来源：ai_trader/config.py 的 trend_hysteresis_pct（默认 0.2）。
# 不许在这里再写一个字面量 —— 那正是它上一次和线上走散的原因。
HYST = TradingCfg().trend_hysteresis_pct / 100.0


def simulate(C, L, hyst, nl=NL, sp=SP, cost=COST):
    """跑一遍策略，返回逐根权益曲线和成交明细。

    进场：C[i] > sma*(1+h)，且上一根还没到这条线（新穿）
    出场：C[i] <= sma*(1-h)
    止损：L[i] <= entry*(1-sp/100)，按止损价成交（乐观假设，见 HANDOFF）

    ★ 这两个谓词必须和 trend.TrendStrategy 的 enter_above / exit_above 一致：
        enter_above = cl[-1] > sma*(1+h)     （严格大于）
        exit_above  = cl[-1] > sma*(1-h)     （出场 = not exit_above）
    有 tests/test_liveedge_parity.py 对着比。
    """
    n = len(C)
    cash, qty, pos, entry, stop = 1.0, 0.0, False, 0.0, 0.0
    mtm, trades, stopped, bars, fees = [], 0, 0, 0, 0.0
    worst_mae = 0.0
    signals = []          # (i, 'enter'|'exit', price)，供对照测试用
    for i in range(n):
        if i >= nl + 1:
            s = sum(C[i - nl:i]) / nl
            sp_prev = sum(C[i - 1 - nl:i - 1]) / nl
            if not pos:
                if C[i] > s * (1 + hyst) and C[i - 1] <= sp_prev * (1 + hyst):
                    pos = True; entry = C[i]; stop = entry * (1 - sp / 100.0)
                    f = cash * cost; cash -= f; fees += f
                    qty = (cash * (1 - cost)) / entry
                    cash -= qty * entry; trades += 1
                    signals.append((i, 'enter', C[i]))
            else:
                bars += 1
                mae = (entry - L[i]) / entry * 100
                worst_mae = max(worst_mae, mae)
                if L[i] <= stop:
                    px = stop
                    cash += qty * px; f = qty * px * cost; cash -= f; fees += f
                    qty = 0.0; pos = False; stopped += 1
                    signals.append((i, 'stop', px))
                elif C[i] <= s * (1 - hyst):
                    px = C[i]
                    cash += qty * px; f = qty * px * cost; cash -= f; fees += f
                    qty = 0.0; pos = False
                    signals.append((i, 'exit', px))
        mtm.append(cash + qty * C[i])
    return dict(mtm=mtm, trades=trades, stopped=stopped, bars=bars,
                fees=fees, mae=worst_mae, final=mtm[-1], signals=signals)


def mdd(cur):
    pk = cur[0]; w = 0.0
    for v in cur:
        pk = max(pk, v); w = max(w, (pk - v) / pk)
    return w * 100


def main():
    c = sqlite3.connect(DB)
    rows = c.execute("SELECT open_time,h,l,c FROM klines WHERE interval='4h'"
                     " ORDER BY open_time").fetchall()
    c.close()
    rows = [r for r in rows if r[3] and r[3] > 0]
    T = [r[0] for r in rows]
    H = [r[1] for r in rows]
    L = [r[2] for r in rows]
    C = [r[3] for r in rows]
    N = len(rows)
    yr = [datetime.datetime.utcfromtimestamp(t / 1000).year for t in T]
    yrs = (T[-1] - T[0]) / (365.25 * 86400000)

    r = simulate(C, L, HYST)
    mtm = r['mtm']
    bhc = [C[i] / C[0] for i in range(N)]

    bounds = {}
    for i, y in enumerate(yr):
        e = bounds.setdefault(y, [i, i])
        e[1] = i
    years = sorted(bounds)

    print("数据 %d 根 4h  %.1f 年｜滞回带 %.2f%%（来自线上配置）"
          % (N, yrs, HYST * 100))
    print()
    print("=" * 74)
    print("%-6s %11s %11s %10s" % ("年份", "策略", "买入持有", "差额"))
    print("=" * 74)
    prev_s = prev_b = 1.0
    for y in years:
        i0, i1 = bounds[y]
        s_r = (mtm[i1] / prev_s - 1) * 100
        b_r = (bhc[i1] / prev_b - 1) * 100
        print("%-6d %+10.1f%% %+10.1f%% %+9.1f%%" % (y, s_r, b_r, s_r - b_r))
        prev_s, prev_b = mtm[i1], bhc[i1]

    s_f, b_f = mtm[-1], bhc[-1]
    s_dd, b_dd = mdd(mtm), mdd(bhc)
    print("=" * 74)
    print("%-6s %+10.0f%% %+10.0f%%   总收益" % ("全期", (s_f - 1) * 100, (b_f - 1) * 100))
    print("%-6s %+10.1f%% %+10.1f%%   CAGR" % ("全期", (s_f ** (1 / yrs) - 1) * 100,
                                                 (b_f ** (1 / yrs) - 1) * 100))
    print("%-6s %10.1f%% %10.1f%%   最大回撤(逐根重估)" % ("全期", s_dd, b_dd))
    print("%-6s %10.2f %10.2f   Calmar" % ("全期",
          ((s_f ** (1 / yrs) - 1) * 100) / s_dd, ((b_f ** (1 / yrs) - 1) * 100) / b_dd))
    print()
    print("笔数 %d｜止损触发 %d 次｜在场 %.0f%%｜累计手续费 %.1f%% 权益"
          % (r['trades'], r['stopped'], r['bars'] / N * 100, r['fees'] * 100))
    print("持仓期最大浮亏 (MAE) %.1f%%｜止损线 %.0f%%" % (r['mae'], SP))
    print("100U -> 策略 %.0fU（最坏 %.0fU）｜买入持有 %.0fU（最坏 %.0fU）"
          % (100 * s_f, 100 * s_f * (1 - s_dd / 100), 100 * b_f,
             100 * b_f * (1 - b_dd / 100)))


if __name__ == '__main__':
    main()
