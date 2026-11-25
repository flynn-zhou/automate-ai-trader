# -*- coding: utf-8 -*-
"""K 线演化（docs/K线演化-设计讨论稿.md 的实现）。

一句话定位
----------
**每根（指定周期的）K 线收盘时，输出一份多维读数：K 线结构、观察线反应、量能、
区间演化空间位置。** 接生产、逐根算、周期由使用者指定。目的是辅助进场与离场。

不是「给一根 K 线做分类」，也不是判断哪条线是支撑。

观察线，不是支撑线
------------------
用户原话：「任何指标，比如黄金分割线，ema20/200 等都不是支撑线，只是观察线！
观察线就是不要直接动手上仓位，你得观察价格在这里的反应。」

    「支撑线」是断言 —— 无法验证，跌破就改口，永远自洽。
    「观察线」是观测位 —— 可以记录、可以数、可以统计。

支撑不是线的属性，是反应的统计结果。所以本包**不判断哪条线是支撑**，
只记录在每条观察线上发生了什么。

实现进度（设计稿 §12 严格按序，不许跳）
---------------------------------------
    第 1 步  观察线计算与落库 kbar_line        ← 已完成
    第 2 步  事实层 kbar_evolution（结构 + 量能 + 内路径）  ← 已完成
    第 3 步  反应层 kbar_reaction              ← 已完成
    第 4 步  事件层 kbar_event（+ 表 1 的 event 列）  ← 已完成
    第 5 步  事后层 kbar_forward（独立进程，不与第 4 步共享代码路径）  ← 已完成
    读数层   shape.py → 表 5 kbar_shape（判断也落库）  ← 已完成
    佐证层   verify.py → 表 6 kbar_verify（用后面的 K 线验当时的判断）  ← 已完成

    **第 5 步是事后层，禁止参与当下判定。** 它读之后的 K 线（那就是它的工作），
    所以必须物理隔开：单独一张表 3、单独一个进程、单独一个模块 forward.py。
    当下判定路径（lines/facts/reaction/event/shape）不许 import forward ——
    tests/test_kbar_forward.py 里有一条测试扫代码钉这条红线。
    不许跳过第 4 步直接建分类器。

    **佐证层（表 6）同一条红线。** 它用 bars[t+1..t+N] 去核对「当时那句话对不对」，
    所以 shape.py 同样不许 import verify —— 上表一读，判定就在偷看未来，
    1.7 万根历史结论全部作废。单向：表 5 是输入，表 6 是旁注，绝不回写。

    第 3 步的两条硬规则（都踩过，别改回去）：
      1. 反应只能取**上一根 K 线**那一行（`pick_prev`）。用同一 ts 那行是偷未来，
         实测 1,725 次假触碰（占 7.2%）。
      2. 上一根没有这条线，就是它**不存在** —— 不许拿更早的值往前带。
         区间演化转 PRE 时四线全空，靠这条才不会用已消失的线做反应。

读数层（shape.py）：判断本身也落库
-----------------------------------
第 5 步之后加了一层 `shape.py`：把表 1 已有的原料合成一句**判断**
（这根是不是插针 / 有没有收回哪条线 / 是不是恐慌盘 / 是不是针后稳住）。

    为什么需要它：只印 `n_touch=3`、`vol_rel=7.35` 不是读数，读者还得自己判断。
    它的边界：只描述**已经发生的事**，不产出仓位动作，也**不判断哪条线是支撑**。
    设计稿 §12 结尾禁止的是「第 6 步 判断标准」（给线定性 → 变成决策依据），
    不是禁止把观察结果写成一句人话。两者区别：一个产出动作，一个产出描述。

**2026-11-08 用户改口：「所有演化结论字段或者描述都要入 db 库」。**
原先判成「现算不落库」，理由是阈值还没定（D-21）；用户要的是可追溯，
所以改成落表 5 `kbar_shape`。为了不让「临时阈值」被冻成历史，每行都带
`params_hash`：改 shape.py 顶部常量后不重跑，`tools/check_kbar_pipeline.py`
立刻红，`tools/show_kbar.py` 也会在标题打「旧口径」。
表 0-4 存**客观值**，表 5 存**判断**，这是它们的分工。

模块
----
    lines.py     纯计算：EMA / 唐奇安 / 结构位 / 区间演化四线，含防偷未来
    facts.py     纯计算：K 线结构 / 量能 / 3s 内路径 / 区间位置（zone）
    reaction.py  纯计算：碰到哪几条线 / 从哪边来 / 穿透多深 / 收回多少
    event.py     纯计算：突破 / 反包（只用收盘换边，不引入阈值）
    shape.py    纯计算：插针 / 插针收回 / 恐慌盘 / 插针后稳住（**不许调 forward**）
    verify.py    纯计算：把结论翻译成可被推翻的预言 + 用后面 N 根裁决
                 （**不许被 shape.py 调用**；本层就是事后层，同表 3 红线）
    forward.py   纯计算：事后收益与「守住」（**禁止当下判定路径调用**）
    store.py     data/kbar.db 的 kbar_line / kbar_evolution / kbar_reaction /
                 kbar_event / kbar_forward / kbar_shape / kbar_verify / kbar_meta
    build.py     只读取数（生产库 mode=ro）→ 计算 → 落库；含多源合并
"""
from __future__ import annotations

from .build import (build_and_persist, build_and_persist_events,
                    build_and_persist_facts, build_and_persist_forward,
                    build_and_persist_reactions, build_and_persist_shape,
                    build_and_persist_verify, event_params_snapshot,
                    facts_params_snapshot, forward_params_snapshot,
                    load_1m_as_15m, load_csv_bars, load_market_bars_rich,
                    load_shape, load_verify, load_ticks_3s, merge_bars,
                    open_ro, reaction_params_snapshot, shape_params_snapshot,
                    ticks_cover)
from .facts import (bar_facts, levels_by_role, path_facts, structure,
                    vol_rel, vol_slot_rel, zone_of, TICKS_PER_BAR,
                    TICK_INTERVAL_MS, VOL_REL_N, VOL_SLOT_MIN_SAMPLES,
                    slots_per_day_for, ticks_per_bar_for)
from .lines import (DONCHIAN_HI_BARS, DONCHIAN_LO_BARS, EMA_PERIODS,
                    EMA_WARMUP_BARS, PRIOR_HL_BARS, RANGE_LINE_KINDS,
                    RANGE_PERIOD_MS, SWING_HALF, Bar, Line, build_bar_lines,
                    donchian_values, ema_values, line_key, period_label,
                    prior_hl_values, range_lines_at, range_p, swing_values)
from .reaction import (assert_no_lookahead, as_of, build_reactions,
                       close_side_of, is_touch, pen_depth_pct, pick_prev,
                       recover_pct, side_of, touch_counts_by_bar, TOUCH_TOL_PCT)
from .event import (BAR_EVENT_RULE, BREAK_DOWN, BREAK_UP, EVENT_VOCAB_RANGE,
                    EVENT_VOCAB_RANGE_ONLY, EVENT_VOCAB_USED, FAILED_BREAK,
                    NEUTRAL, REVERT_WINDOW_BARS, bar_event_columns, build_events,
                    classify_cross, event_counts_by_event,
                    find_breakout_then_revert, side_of_close)
# 第 5 步：事后层。**只给回填/探针用**，当下判定路径不许 import 这些。
# 注意 forward.side_of_close 与 event.side_of_close 同名 —— 两个模块都在，
# 包级只导出 event 那个；要用事后层那个请写 `from ai_trader.kbar import forward as F`。
from . import forward as forward
from .forward import (FORWARD_HORIZONS, HELD_BARS, HELD_TOL_PCT, RET_BASIS,
                      RET_FEE, ForwardLookaheadError, build_forward,
                      forward_returns, held_of, held_side_for, stats_by_held)
# 读数层：把表 1 的原料合成一句判断。纯计算，**落表 5**，不碰 forward。
from .shape import (PANIC_CLOSE_POS_MAX, PANIC_TAKER_BUY_MAX, PANIC_VOL_REL_MIN,
                    PIN_KIND_SWING, PIN_KIND_WICK, SCENARIO_TOL_PTS,
                    STABLE_LOOKBACK_BARS, STABLE_MAX_PIN_RANGE_MULT, STATE_NONE,
                    STATE_NEAR_PIN, STATE_ORDER, STATE_PANIC, STATE_PIN,
                    STATE_PIN_RECOVERED, STATE_STABLE, SWING_LOOKBACK_BARS,
                    WICK_PCT_MIN, WICK_PTS_MIN, Geometry, Judgement, after_pin,
                    after_pin_full, counts_by_label, find_pin_before,
                    find_pin_before_ex, geometry, is_panic, judge, panic_side,
                    params, params_hash, pin_amp, pin_side, recovered_after_pin,
                    recovered_lines, stable_of, swing_pin_depth, swing_pin_kind)
from .store import KBarStore
# 佐证层：用后面的 K 线验当时的判断。**只给回填/探针用**，
# 判定路径（shape.py）不许 import 本模块 —— 同表 3 红线，有测试扫源码钉着。
from . import verify as verify
from .verify import (VERDICT_BROKE, VERDICT_HELD, VERDICT_NO_PRED,
                     VERDICT_ORDER, VERDICT_UNKNOWN, VERIFY_HORIZONS,
                     VERIFY_TOL_PTS, Prediction, VerifyResult, hit_rate, lift_z,
                     predict, summarize, verify_hash, verify_params)

__all__ = [
    "Bar", "Line", "KBarStore",
    "build_bar_lines", "ema_values", "donchian_values", "prior_hl_values",
    "swing_values", "range_lines_at", "range_p", "line_key", "period_label",
    "EMA_PERIODS", "EMA_WARMUP_BARS", "DONCHIAN_HI_BARS", "DONCHIAN_LO_BARS",
    "PRIOR_HL_BARS", "SWING_HALF", "RANGE_PERIOD_MS", "RANGE_LINE_KINDS",
    # 第 2 步：事实层
    "bar_facts", "structure", "vol_rel", "vol_slot_rel", "path_facts",
    "zone_of", "levels_by_role",
    "TICK_INTERVAL_MS", "TICKS_PER_BAR", "VOL_REL_N", "VOL_SLOT_MIN_SAMPLES",
    "slots_per_day_for", "ticks_per_bar_for",
    "build_and_persist", "build_and_persist_facts", "facts_params_snapshot",
    "load_1m_as_15m", "load_csv_bars", "load_market_bars_rich",
    "load_ticks_3s", "merge_bars", "open_ro", "ticks_cover",
    # 第 3 步：反应层
    "build_reactions", "as_of", "pick_prev", "is_touch", "side_of", "close_side_of",
    "pen_depth_pct", "recover_pct", "touch_counts_by_bar", "assert_no_lookahead",
    "TOUCH_TOL_PCT", "build_and_persist_reactions", "reaction_params_snapshot",
    # 第 4 步：事件层
    "build_events", "classify_cross", "side_of_close", "bar_event_columns",
    "event_counts_by_event", "find_breakout_then_revert",
    "BREAK_UP", "BREAK_DOWN", "FAILED_BREAK", "NEUTRAL",
    "EVENT_VOCAB_RANGE", "EVENT_VOCAB_USED", "EVENT_VOCAB_RANGE_ONLY",
    "REVERT_WINDOW_BARS", "BAR_EVENT_RULE",
    "build_and_persist_events", "event_params_snapshot",
    # 第 5 步：事后层
    "forward", "build_forward", "forward_returns", "held_of", "held_side_for",
    "stats_by_held", "ForwardLookaheadError",
    "FORWARD_HORIZONS", "HELD_BARS", "HELD_TOL_PCT", "RET_BASIS", "RET_FEE",
    "build_and_persist_forward", "forward_params_snapshot",
    # 读数层（判断落表 5 kbar_shape）
    "judge", "geometry", "pin_side", "panic_side", "recovered_lines",
    "recovered_after_pin", "is_panic", "find_pin_before", "find_pin_before_ex",
    "after_pin", "after_pin_full", "stable_of", "counts_by_label",
    "swing_pin_kind", "swing_pin_depth", "pin_amp",
    "params", "params_hash",
    "build_and_persist_shape", "shape_params_snapshot", "load_shape",
    "Judgement", "Geometry",
    "STATE_PANIC", "STATE_PIN_RECOVERED", "STATE_PIN", "STATE_STABLE",
    "STATE_NEAR_PIN", "STATE_NONE", "STATE_ORDER",
    "PIN_KIND_WICK", "PIN_KIND_SWING",
    "WICK_PCT_MIN", "WICK_PTS_MIN", "SCENARIO_TOL_PTS",
    "PANIC_VOL_REL_MIN", "PANIC_TAKER_BUY_MAX", "PANIC_CLOSE_POS_MAX",
    "STABLE_LOOKBACK_BARS", "STABLE_MAX_PIN_RANGE_MULT", "SWING_LOOKBACK_BARS",
    # 佐证层（表 6 kbar_verify）
    "verify", "predict", "verify_hash", "verify_params", "summarize",
    "hit_rate", "lift_z", "Prediction", "VerifyResult",
    "VERIFY_HORIZONS", "VERIFY_TOL_PTS", "VERDICT_ORDER",
    "VERDICT_HELD", "VERDICT_BROKE", "VERDICT_UNKNOWN", "VERDICT_NO_PRED",
    "build_and_persist_verify", "load_verify",
]
