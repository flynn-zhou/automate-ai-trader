# -*- coding: utf-8 -*-
"""VOL-1 / KIND-1：量能口径改为「附近放量 >= N x 最近均量」，三引线停用。

用户 2026-11-26 口径：
  「量能门槛要跟最近相比大于 3 就行，不是固定，只要最近 K 线出现了大于最近
    均量的 3 倍或者 4 倍或者 5 都算，具体多少回测一下，不一定是突破的这一根，
    我们看的是附近的放量行为，附近放量行为出现了，那就是满足了」

这里钉三件事：
  1. 基准是**均值**不是中位数（口径从"中位量"改成"均量"）。
  2. 门槛看的是**突破根前后**窗口内的**最大**量，不是突破根自己。
  3. 三引线**不在生产启用集合里**（758 笔真实样本均 R -0.515）。
"""
from __future__ import annotations

import pytest

from ai_trader.trading import pending_plan as P


class _H(object):
    """1h bar，只要 ts/o/h/l/c/v。"""

    __slots__ = ("ts", "o", "h", "l", "c", "v")

    def __init__(self, ts, o, h, l, c, v):
        self.ts, self.o, self.h, self.l, self.c, self.v = ts, o, h, l, c, v


M1H = 3_600_000
T0 = 1_795_000_000_000 - (1_795_000_000_000 % M1H)


def _build(vols, tags, line_px, **kw):
    """造一段 1h 序列：价格从上方一路下跌，**穿过** line_px。

    第 0 根收盘 = 60000 且初始价 60200，之后每根跌 100 ——
    于是「前收 >= 线 > 今收」在穿过那根成立（窗口闸的 `crossed`）。
    """
    hours = []
    px = 60_200.0
    for i, v in enumerate(vols):
        o = px
        c = px - 100.0
        hours.append(_H(T0 + i * M1H, o, o + 10.0, c - 10.0, c, v))
        px = c

    def tag_of(ts):
        return tags.get(int(ts), ("", ""))

    def line_of(ts, key):
        return line_px

    return P.build_open_windows(hours, tag_of, line_of, **kw)


def _tags_for(idx, n, tag=P.TAG_BREAKOUT, direction="DOWN"):
    """第 idx 根是「突破/反转」，其后紧跟 2 根同方向的 `forming`。

    ★ 后面那几根 `forming` 是**必需**的：窗口 = 突破 + 紧跟的 forming 段，
      没有 forming 就 `end is None`，窗口根本不会生成。
    """
    t = {}
    if 0 <= idx < n:
        t[T0 + idx * M1H] = (tag, direction)
    for k in (1, 2):
        if idx + k < n:
            t[T0 + (idx + k) * M1H] = (P.TAG_FORMING, direction)
    return t


# ------------------------------------------------------------------ 口径

def test_reference_is_mean_not_median():
    """基准必须是**均值**：一串 1,1,1,1,100 里，均值 20.8、中位数 1。

    构造使「突破根量 3.0」：对均值 20.8 只有 0.14 倍（不过门），
    对中位数 1 是 3.0 倍（过门）。若实现还用中位数，这个用例会失败。
    """
    vols = [1.0, 1.0, 1.0, 1.0, 100.0]
    # 第 5 根（idx=5）是突破根，量 3.0；参考窗口 = 前 5 根
    vols = vols + [3.0, 1.0, 1.0]
    tags = _tags_for(5, len(vols))
    wins = _build(vols, tags, line_px=59_650.0, vol_mult=3.0, vol_win=24,
                  vol_near=0, start_tags=(P.TAG_BREAKOUT,), extra_bars=3)
    assert wins == [], "用均值时 3.0/20.8 不过门；过了说明还在用中位数"


def test_mean_lets_a_real_expansion_through():
    """同样一串量，突破根放 100（= 均值 20.8 的 4.8 倍）必须过门。"""
    vols = [1.0, 1.0, 1.0, 1.0, 1.0, 100.0, 1.0, 1.0]
    tags = _tags_for(5, len(vols))
    wins = _build(vols, tags, line_px=59_650.0, vol_mult=3.0, vol_win=24,
                  vol_near=0, start_tags=(P.TAG_BREAKOUT,), extra_bars=3)
    assert len(wins) == 1


# ------------------------------------------------------------------ 附近

def test_expansion_before_the_breakout_bar_counts():
    """★ 用户明确要的：放量**不一定在突破那一根**。

    突破根量很小（1.0），但**前一根**放量 100 —— 附近放量成立，
    窗口必须长出来（"先扫后破"）。
    """
    vols = [1.0, 1.0, 1.0, 1.0, 100.0, 1.0, 1.0, 1.0]
    tags = _tags_for(5, len(vols))
    wins = _build(vols, tags, line_px=59_650.0, vol_mult=3.0, vol_win=24,
                  vol_near=1, start_tags=(P.TAG_BREAKOUT,), extra_bars=3)
    assert len(wins) == 1, "前一根放量应算「附近放量」"


def test_expansion_after_the_breakout_bar_counts():
    """放量在**后一根**也算（突破之后才扫）。"""
    vols = [1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 100.0]
    tags = _tags_for(5, len(vols))
    wins = _build(vols, tags, line_px=59_650.0, vol_mult=3.0, vol_win=24,
                  vol_near=1, start_tags=(P.TAG_BREAKOUT,), extra_bars=3)
    assert len(wins) == 1, "后一根放量应算「附近放量」"


def test_vol_near_zero_ignores_neighbours():
    """`vol_near=0` 退化成「只看突破根自己」：前一根放量不再算数。"""
    vols = [1.0, 1.0, 1.0, 1.0, 100.0, 1.0, 1.0, 1.0]
    tags = _tags_for(5, len(vols))
    wins = _build(vols, tags, line_px=59_650.0, vol_mult=3.0, vol_win=24,
                  vol_near=0, start_tags=(P.TAG_BREAKOUT,), extra_bars=3)
    assert wins == []


def test_near_lookahead_does_not_indexerror():
    """突破根在序列末尾时，`vol_near` 往前看不能越界（曾经会 IndexError）。"""
    vols = [1.0, 1.0, 1.0, 1.0, 1.0, 100.0, 1.0, 1.0]
    tags = _tags_for(5, len(vols))
    wins = _build(vols, tags, line_px=59_650.0, vol_mult=3.0, vol_win=24,
                  vol_near=5, start_tags=(P.TAG_BREAKOUT,), extra_bars=0)
    assert len(wins) == 1


# ------------------------------------------------------------------ 反转起点

def test_reversal_tag_can_start_a_window():
    """用户 2026-11-26 追加：反转也能当窗口起点（22:45 那根 +852 点）。"""
    vols = [1.0, 1.0, 1.0, 1.0, 1.0, 100.0, 1.0, 1.0]
    tags = _tags_for(5, len(vols), tag=P.TAG_REVERSAL)
    wins = _build(vols, tags, line_px=59_650.0, vol_mult=3.0, vol_win=24,
                  vol_near=1, start_tags=(P.TAG_BREAKOUT, P.TAG_REVERSAL),
                  extra_bars=3)
    assert len(wins) == 1


def test_reversal_ignored_when_not_in_start_tags():
    """不在 `start_tags` 里时，反转不能开窗口（保持可回退）。"""
    vols = [1.0, 1.0, 1.0, 1.0, 1.0, 100.0, 1.0, 1.0]
    tags = _tags_for(5, len(vols), tag=P.TAG_REVERSAL)
    wins = _build(vols, tags, line_px=59_650.0, vol_mult=3.0, vol_win=24,
                  vol_near=1, start_tags=(P.TAG_BREAKOUT,), extra_bars=3)
    assert wins == []


# ------------------------------------------------------------------ 常量

def test_production_constants_are_the_calibrated_values():
    """生产常数必须等于回测夹出来的那一组 —— 防止有人手改成未验证的值。"""
    assert P.WINDOW_VOL_MULT == 3.0, "门槛 = 3 倍最近均量（用户口径）"
    assert P.WINDOW_VOL_NEAR == 2, "附近 = 前后各 2 根 1h（回测均 R 最高）"
    assert P.WINDOW_VOL_WIN == 24, "参考窗口 = 前 24 小时"
    assert P.TAG_BREAKOUT in P.WINDOW_START_TAGS
    assert P.TAG_REVERSAL in P.WINDOW_START_TAGS


def test_three_wicks_disabled_in_production_kinds():
    """★ 三引线必须**不在**生产启用集合里。

    758 笔真实样本、胜率 14%、均 R -0.515 —— 窗口内也是负的（11 笔 -0.586R）。
    要试回来必须改这一个常量，不能靠"探测器默认还在"蒙混过关。
    """
    assert P.KIND_WICKS not in P.ENABLED_KINDS
    assert P.KIND_INVERSE_V in P.ENABLED_KINDS
    assert P.KIND_EMA_REJECT in P.ENABLED_KINDS
    # 探测器本身必须还在（只是不启用），否则是删资产不是停用
    assert P.KIND_WICKS in P.KINDS
    assert P.KIND_WICKS in P.DETECTORS


def test_detect_all_default_still_covers_all_kinds():
    """`detect_all` 的默认 `kinds` 仍是全部三条 —— 测试/回测要能单独验形态。"""
    import inspect
    sig = inspect.signature(P.detect_all)
    assert sig.parameters["kinds"].default == P.KINDS
