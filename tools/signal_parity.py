# -*- coding: utf-8 -*-
"""信号级 parity：线上策略和回测必须产生【同一批】进出场点。

为什么这比 PnL 级 parity 更强：
  PnL 级 parity 只比对最终收益（我笔记里那个 0.88% 误差）。
  两个不同的策略也可能撞出相近的收益——那叫巧合，不叫一致。
  信号级比对的是"哪一根进、哪一根出"，一次不一致就暴露。

回测的定义（tools/long_only.py:25-49）：
  进场：price > sma(i,n) 且 C[i-1] <= sma(i-1,n)     price 是第 i 根的收盘
  出场：price < sma(i,n)

线上（ai_trader/trading/trend.py:121-156）：
  above      = cl[-1] > sma_now        （cl[-1] 是最后一根已收盘）
  prev_above = cl[-2] > sma_prev
  want_entry = above and not prev_above

两者必须逐根一致。
"""
import os
import sys
sys.path.insert(0, os.getcwd())

from ai_trader.config import AppCfg
from ai_trader.trading.trend import TrendStrategy

N = 200
IV = 14400000


def sma_of(cl, i, n):
    """回测口径：mean(C[i-n:i])，不含第 i 根。"""
    if i < n:
        return None
    return sum(cl[i - n:i]) / n


def backtest_signals(cl, n):
    """复刻 tools/long_only.py 的进出场判定，返回 (进场下标, 出场下标)。"""
    entries, exits = [], []
    pos = False
    for i in range(n + 1, len(cl)):
        s = sma_of(cl, i, n)
        sp = sma_of(cl, i - 1, n)
        if s is None or sp is None:
            continue
        price = cl[i]
        if not pos and price > s and cl[i - 1] <= sp:
            pos = True
            entries.append(i)
        elif pos and price < s:
            pos = False
            exits.append(i)
    return entries, exits


def live_signals(cl, n):
    """喂 4h 收盘序列给线上策略，记录它想在哪一根进、哪一根出。

    进出场都由 want_entry / state 决定。出场在线上是"4h 收盘跌破 SMA"，
    对应 state()["above"] == False。
    """
    cfg = AppCfg()
    cfg.trading.sma_lookback = n
    tg = TrendStrategy(cfg.trading, symbol="BTCUSDT")
    tg.seeded = True
    tg.need_reconcile = False

    entries, exits = [], []
    pos = False
    for i in range(len(cl)):
        if i < n + 1:
            continue
        # 只喂到第 i 根（含），模拟"第 i 根刚收盘"
        tg.bars = [{"t": k * IV, "o": cl[k], "h": cl[k], "l": cl[k],
                    "c": cl[k], "v": 1.0} for k in range(i + 1)]
        tg.last_eval_bar = (i) * IV
        st = tg.state(cl[i])
        if not st["ready"]:
            continue
        if not pos:
            sig = tg.want_entry(cl[i])
            if sig:
                pos = True
                entries.append(i)
        else:
            if not st["above"]:
                pos = False
                exits.append(i)
    return entries, exits


def synth(n=400):
    """造一段能触发多次交叉的序列：正弦叠加趋势。"""
    import math
    cl = []
    base = 30000.0
    for i in range(n):
        v = base * (1.0 + 0.25 * math.sin(i / 11.0) + i * 0.0008)
        cl.append(round(v, 2))
    return cl


cl = synth(400)
bt_e, bt_x = backtest_signals(cl, N)
lv_e, lv_x = live_signals(cl, N)

print("回测进场 %d 次，出场 %d 次" % (len(bt_e), len(bt_x)))
print("线上进场 %d 次，出场 %d 次" % (len(lv_e), len(lv_x)))
print()
mismatch = 0
if bt_e != lv_e:
    print("!!! 进场序列不一致")
    print("  回测:", bt_e[:12])
    print("  线上:", lv_e[:12])
    mismatch += 1
if bt_x != lv_x:
    print("!!! 出场序列不一致")
    print("  回测:", bt_x[:12])
    print("  线上:", lv_x[:12])
    mismatch += 1
if mismatch == 0:
    print("一致：线上和回测在同样的 K 线上进出场")
print()
print("进场下标:", bt_e[:10])
print("出场下标:", bt_x[:10])
raise SystemExit(1 if mismatch else 0)
