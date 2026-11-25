# -*- coding: utf-8 -*-
"""K 线演化 · 第 6 步：佐证层（表 6 `kbar_verify`）。

    ┌──────────────────────────────────────────────────────────────┐
    │ 「判断对不对，只能用后面的 K 线来判。」                          │
    │                                                              │
    │ 表 3（forward）回答的是「之后涨没涨」—— 那是客观收益，与判断     │
    │ 无关。本模块回答的是另一件事：**当时那句话，被后面打脸了没有**。  │
    │                                                              │
    │ 红线（与表 3 完全一致，有测试扫源码钉着）：                       │
    │   · **shape.py 永远不许 import 本模块**                        │
    │     上表一读，判定就在偷看未来，1.7 万根历史结论全部作废         │
    │   · 单独一张表（表 6）、单独一个进程                              │
    └──────────────────────────────────────────────────────────────┘

一条铁律：**没有可被推翻的预言，就没有结论**。

    所以每个结论词都必须翻译成一句带具体价位的话，例如

        「下影 76,127.4 是被打回来的位置，后面 1 根不会有效跌破它」

    然后拿 `bars[t+1..t+N]` 去核对。翻译不出来的词（比如「无」）就记
    `VERDICT_NO_PRED`，**不许**凑一个成立率 —— 那等于把「我没说法」
    伪装成「我说对了」。

为什么必须有基准率：N=1 时「随便一根，本根低点后面不被破」本来就有
53.1%（BTCUSDT@15m 实测）。报「命中 73.5%」而不报基准，就是把
「比瞎猜好 20 个点」说成「七成准」。

口径（都写在文件顶部，一处可改）：

    VERIFY_HORIZONS   = (1, 3)   N=1 为主判据，N=3 看连续性
    VERIFY_TOL_PTS    = 0.0      **不复用 SCENARIO_TOL_PTS 的 20**
    尾部不足 N 根      = VERDICT_UNKNOWN（不是「成立」，也不是「推翻」）
"""

from __future__ import annotations

import hashlib
import json
from typing import Dict, List, NamedTuple, Optional, Sequence, Tuple

from . import shape as S

# ---------------------------------------------------------------- 口径常量
# 后面看几根。1 为主判据：信息量最大（提升最多），尾部只损失 1 根。
# 3 同时落库，用来回答「这个结论只是碰巧下一根没打脸，还是真的站住了」。
VERIFY_HORIZONS: Tuple[int, ...] = (1, 3)

# 佐证容差。0 = 碰到就算破。
# ⚠ 用户给的 20（SCENARIO_TOL_PTS）**只属于 after_pin 那一个场景**，
#   原话：「我设置的这个容差只在这一个场景生效！别扩散！」
#   这里是另一个场景，所以从 0 起算。实测 0/5/20 三档结论都不翻面。
VERIFY_TOL_PTS = 0.0

# 预言规则的版本号。改了「什么话算成立」就 +1 ——
# params_hash 只认阈值，认不出规则改动，这个字段才是那件事的开关。
PRED_RULES_VERSION = "v1"

# ---------------------------------------------------------------- 裁决四态
VERDICT_HELD = "佐证成立"      # 后面没打脸
VERDICT_BROKE = "被推翻"       # 后面打脸了
VERDICT_UNKNOWN = "无法判定"   # 后面凑不满 N 根（表尾），**不含判断**
VERDICT_NO_PRED = "无预言"     # 这个结论词给不出可推翻的话

VERDICT_ORDER: Tuple[str, ...] = (VERDICT_HELD, VERDICT_BROKE,
                                  VERDICT_UNKNOWN, VERDICT_NO_PRED)

# 预言种类。落库后能按种类聚合，也能让人一眼看出判的是哪个价位。
PRED_PIN_LOW_HELD = "针低守住"
PRED_PIN_LOW_BROKE = "针低会破"
PRED_PIN_HIGH_HELD = "针高守住"
PRED_PIN_HIGH_BROKE = "针高会破"
PRED_REF_LOW_HELD = "参照针低守住"
PRED_REF_HIGH_HELD = "参照针高守住"
PRED_QUIET = "后无大波动"
PRED_NONE = "无预言"


class Prediction(NamedTuple):
    """一个结论词被翻译成的话，以及考卷。"""
    kind: str                 # 预言种类（落库列 pred_kind）
    text: str                 # 人读那句话（落库列 pred_text）
    ref_price: Optional[float]   # 这句话针对的价位
    side: Optional[str]       # 沿哪个方向判：down=看低点，up=看高点
    must_hold: bool           # True=「不该破」，False=「该破」
    amp: Optional[float] = None  # PRED_QUIET 用：多大的波动算「大」


class VerifyResult(NamedTuple):
    verdict: str
    actual_price: Optional[float]
    diff_pts: Optional[float]
    n_bars: int
    detail: str

    def as_dict(self) -> dict:
        return {"verdict": self.verdict, "actual_price": self.actual_price,
                "diff_pts": self.diff_pts, "n_bars": self.n_bars,
                "detail": self.detail}


def verify_params() -> Dict:
    """本层的口径快照。**不含** shape 的阈值（那是表 5 的 params_hash 管的）。"""
    return {"horizons": list(VERIFY_HORIZONS),
            "tol_pts": VERIFY_TOL_PTS,
            "pred_rules_version": PRED_RULES_VERSION,
            "verdict_states": list(VERDICT_ORDER)}


def verify_hash() -> str:
    """本层口径指纹。改了 N / 容差 / 预言规则，这个数就变，表 6 立刻可辨新旧。"""
    blob = json.dumps(verify_params(), sort_keys=True,
                      separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------- 翻译预言
def _bar(j: dict) -> S.Geometry:
    return S.geometry(j["o"], j["h"], j["l"], j["c"])


def _fmt_ts(ts) -> str:
    return S._fmt_ts(ts) if ts is not None else "?"


def predict(bars: Sequence[dict], i: int, judgement,
            ref_pin_side: Optional[str] = None) -> Prediction:
    """把第 `i` 根的结论翻译成一句可被推翻的话。

    `judgement` 是 `shape.Judgement`（或任何有 label/states/pin_ts/
    ref_pin_kind 属性的东西）。`ref_pin_side` 优先用落库的 `ref_pin_side` 列；
    没给就退回自己推 —— 但**推的方向可能与判定时的方向不一致**
    （实测 8 根：那根有长上影却按「向下极值」判的稳住），
    所以生产路径必须传落库值。
    """
    from . import shape as _S
    lab = judgement.label
    states = judgement.states
    g = _bar(bars[i])

    # ---- 恐慌盘：方向由 panic_side 定，与「是不是针」无关（v2 口径）----
    if S.STATE_PANIC in states:
        psd = S.panic_side(g) or (S.pin_side(g) if S.pin_side(g) else None)
        has_wick = S.pin_side(g) is not None
        if psd == S.SIDE_DOWN:
            if has_wick:
                return Prediction(
                    PRED_PIN_LOW_HELD,
                    "恐慌盘里下影 %.1f 点已被买回，后面不会跌破本根低点 %.1f"
                    % (g.dn, g.l), g.l, S.SIDE_DOWN, True)
            return Prediction(
                PRED_PIN_LOW_BROKE,
                "恐慌盘收在最低（收位 %.0f%%）、卖压未释放，后面会继续跌破本根低点 %.1f"
                % (g.close_pos * 100, g.l), g.l, S.SIDE_DOWN, False)
        if psd == S.SIDE_UP:
            if has_wick:
                return Prediction(
                    PRED_PIN_HIGH_HELD,
                    "恐慌盘里上影 %.1f 点已被卖回，后面不会突破本根高点 %.1f"
                    % (g.up, g.h), g.h, S.SIDE_UP, True)
            return Prediction(
                PRED_PIN_HIGH_BROKE,
                "恐慌盘收在最高（收位 %.0f%%）、买盘未释放，后面会继续突破本根高点 %.1f"
                % (g.close_pos * 100, g.h), g.h, S.SIDE_UP, False)

    # ---- 本根自己是针：预言「这个极值是被打回来的位置」 ----
    # ⚠ 「插针收回」全部同时是「插针」（收回以插针为前提），所以它走到这里时
    #   被验的是**针那半句**，「哪条线收回来了」那半句本层**验不了** ——
    #   本层只核对极值，不核对线（线要另立预言，实测「后面不反向穿线」只有
    #   21.8%，比基准 48.6% 还低 26.8 点，因为价格本来就天天穿线）。
    #   所以下面那句话末尾会明写「（本层只验极值这一半）」——
    #   不写就是让人以为收回那半句也验过了。
    _rec_note = ("（本层只验极值这一半，"
                 "「哪条线收回」不在此列的核对范围）"
                 if S.STATE_PIN_RECOVERED in states else "")
    ps = S.pin_side(g)
    if ps is not None and (S.STATE_PIN in states or lab == S.STATE_PIN):
        if ps == S.SIDE_DOWN:
            return Prediction(
                PRED_PIN_LOW_HELD,
                "下影 %.1f 点（%.0f%% 全幅）是「被打回来的位置」，"
                "后面不会跌破本根低点 %.1f%s"
                % (g.dn, g.dn_pct * 100, g.l, _rec_note),
                g.l, S.SIDE_DOWN, True)
        return Prediction(
            PRED_PIN_HIGH_HELD,
            "上影 %.1f 点（%.0f%% 全幅）是「被打回来的位置」，"
            "后面不会突破本根高点 %.1f%s"
            % (g.up, g.up_pct * 100, g.h, _rec_note),
            g.h, S.SIDE_UP, True)

    # ---- 插针收回：预言「收回来了，那条线不会立刻再被穿越」 ----
    # 这里**不给**预言：实测 1,197 根里只有 21.8% 后面 1 根不再反向穿线，
    # 而「随便一根后面不反向」本身就有 48.6% —— 比基准还低 26.8 个点。
    # 也就是说这个结论词的「收回」说的是同根内的事，**不构成对未来的说法**。
    # 硬凑一个预言会把「没有预言」伪装成「预言错了」，所以记 无预言。
    if lab == S.STATE_PIN_RECOVERED and S.STATE_PIN not in states:
        return Prediction(
            PRED_NONE,
            "「插针收回」说的是同根内穿透后收回，不构成对后面几根的说法",
            None, None, True)

    # ---- 稳住 / 远离：预言「参照针的极值守住」 ----
    if lab in (S.STATE_STABLE, S.STATE_NEAR_PIN) or \
            (S.STATE_STABLE in states) or (S.STATE_NEAR_PIN in states):
        j = None
        if judgement.pin_ts is not None:
            for k in range(i - 1, -1, -1):
                if bars[k].get("ts") == judgement.pin_ts:
                    j = k
                    break
        if j is None:
            return Prediction(PRED_NONE, "找不到参照针，无预言", None, None, True)
        gp = _bar(bars[j])
        sd = ref_pin_side
        if sd is None:
            sd = S.pin_side(gp)
            if sd is None:
                sd = S.swing_pin_kind(bars, j, i - 1)
        kind_cn = "长影线" if judgement.ref_pin_kind == S.PIN_KIND_WICK else "局部极值"
        if sd == S.SIDE_DOWN:
            return Prediction(
                PRED_REF_LOW_HELD,
                "价格在 %s 那根%s针的上方，后面不会跌破它的低点 %.1f"
                % (_fmt_ts(judgement.pin_ts), kind_cn, gp.l),
                gp.l, S.SIDE_DOWN, True)
        return Prediction(
            PRED_REF_HIGH_HELD,
            "价格在 %s 那根%s针的下方，后面不会突破它的高点 %.1f"
            % (_fmt_ts(judgement.pin_ts), kind_cn, gp.h),
            gp.h, S.SIDE_UP, True)

    # ---- 无 / 无·真空 / 无·贴线：它说「这没什么」，可被推翻的话 = 「后面不出大波动」 ----
    # 用「本根全幅」当尺，与 shape 里 pin_amp 的下限同一个思路（防十字星除爆）。
    # 三个词共用同一条预言：拆词拆的是**环境描述**，不是「要验的那句话」。
    # 但原文里写上词名 —— 否则两个词在表 6 的 pred_text 里分不开，没法分别统计。
    amp = max(g.rng, S.WICK_PTS_MIN)
    _word = lab if lab in _S.LABEL_NO_ALL else _S.STATE_NONE
    return Prediction(
        PRED_QUIET,
        "%s → 后面不会走出 >= 1 倍本根全幅（%.0f 点）的单边"
        % (_word, amp), None, None, True, amp)


# ---------------------------------------------------------------- 裁决
def verify(bars: Sequence[dict], i: int, prediction: Prediction,
           horizon: int, tol_pts: float = VERIFY_TOL_PTS) -> VerifyResult:
    """拿 `bars[i+1 .. i+horizon]` 去核对这句预言。

    **只用 `i` 之后的根**。越界的不是「成立」而是「无法判定」：
    表尾 3 根永远验不了，把它算成成立就是把「没验」说成「验过了」。
    """
    if horizon <= 0:
        raise ValueError("horizon 必须 > 0，收到 %r" % (horizon,))
    fut = list(bars[i + 1:i + 1 + horizon])
    n_bars = len(fut)
    if prediction.kind == PRED_NONE:
        return VerifyResult(VERDICT_NO_PRED, None, None, n_bars,
                            "这个结论词没有可被推翻的说法")
    if n_bars < horizon:
        return VerifyResult(VERDICT_UNKNOWN, None, None, n_bars,
                            "后面只有 %d 根，凑不满 %d 根" % (n_bars, horizon))

    g0 = _bar(bars[i])

    # 「后面不出大波动」：量最大偏离，超过 1 倍全幅就是被推翻
    if prediction.kind == PRED_QUIET:
        amp = prediction.amp or max(g0.rng, S.WICK_PTS_MIN)
        ext = max(max(abs(x["h"] - bars[i]["c"]), abs(x["l"] - bars[i]["c"]))
                  for x in fut)
        ok = ext < amp
        return VerifyResult(
            VERDICT_HELD if ok else VERDICT_BROKE, ext, ext - amp, n_bars,
            "后面最大偏离本根收盘 %.1f 点 = %.2f 倍全幅（阈值 1.00 倍 = %.0f 点）"
            % (ext, ext / amp, amp))

    ref = prediction.ref_price
    if ref is None or prediction.side is None:
        return VerifyResult(VERDICT_NO_PRED, None, None, n_bars,
                            "预言缺价位或方向，未判")
    if prediction.side == S.SIDE_DOWN:
        actual = min(x["l"] for x in fut)
        diff = actual - ref          # 正=没破，负=破了
        # 容差 0 时「正好打到」就算破 —— 与本项目 reaction 层 TOUCH_TOL_PCT=0
        # 「正好等于线价算触碰」同一个口径。用 < 会把「打到没超过」算成守住，
        # 那是对被验的那个判断网开一面，佐证层的意义就没了。
        breached = actual <= ref - tol_pts
        word = "跌破" if breached else "未跌破"
        unit = "低点"
    else:
        actual = max(x["h"] for x in fut)
        diff = ref - actual          # 正=没破，负=破了
        breached = actual >= ref + tol_pts
        word = "突破" if breached else "未突破"
        unit = "高点"

    if prediction.must_hold:
        ok = not breached
    else:
        ok = breached

    is_own = prediction.kind in (PRED_PIN_LOW_HELD, PRED_PIN_LOW_BROKE,
                                 PRED_PIN_HIGH_HELD, PRED_PIN_HIGH_BROKE)
    detail = ("后面 %d 根%s到 %.1f，%s%s %.1f（差 %+.1f 点，容差 %.0f）"
              % (n_bars, word, actual,
                 "本根" if is_own else "参照针", unit, ref, diff, tol_pts))
    if not prediction.must_hold:
        detail += "；预言是「会破」，%s算%s" % ("破了" if breached else "没破",
                                            "成立" if ok else "推翻")
    return VerifyResult(VERDICT_HELD if ok else VERDICT_BROKE, actual, diff,
                        n_bars, detail)


# ---------------------------------------------------------------- 存活根数
# 「事后要看多远」这件事，固定 N 是错的表达。
#
# 实测（17,654 根，N 一直测到 12）：
#     N=1 就定型的只有 45.5%，N=3 到 75.0%，N=6 到 90.2%，p99 = 11 根。
#     也就是说只报 N=1/3 的时候，一大半判断还只是**临时答案**。
#
# 而且裁决随 N 是**单调**的：窗口越长越可能破极值，破了就永远不会变回。
# 所以每一根都有一个「第一次被推翻」的根数。这个数比分档裁决信息量大得多，
# 也顺带修掉一个结构性错误：
#     守住型（must_hold=True） 在短 N 下**偏松** —— 还没来得及破就算成立
#     会破型（must_hold=False）在短 N 下**偏严** —— 还没来得及破就算推翻
# 两类预言放在同一个 N 上比命中率，本身就不成立。存活根数把这个问题消掉。
#
# 12 是测出来的：p99 = 11，测到 12 时还在以 1.0%/档 的速度下降（长尾）。
# 它是个**截断**，不是自然终点 —— 换周期/换品种要重测。
SURVIVAL_MAX_N = 12

SURV_BROKE = "已破"        # 第 n 根时破了 → 定型
SURV_LASTED = "活到最后"    # 满 max_n 根都没破 → 定型（删失）
SURV_PENDING = "未成熟"     # 后面的根不够 max_n 根 → **还不能下结论**

SURV_ORDER: Tuple[str, ...] = (SURV_BROKE, SURV_LASTED, SURV_PENDING)


class Survival(NamedTuple):
    """一根判断「活了多久」的结果。

    `status` 三态是必须的：`SURV_PENDING` 和 `SURV_LASTED` 都叫「没破」，
    但一个是「还没验完」，一个是「验完了真没破」。混起来就是拿表尾冒充结论。
    """
    status: str
    break_n: Optional[int]     # 第几根第一次破；活到最后/未成熟为 None
    n_seen: int                # 实际看到几根（未成熟时 < max_n）
    max_n: int
    ref_price: Optional[float]
    actual_at_break: Optional[float]   # 破的时候达到的极值
    detail: str

    def as_dict(self) -> dict:
        return {"status": self.status, "break_n": self.break_n,
                "n_seen": self.n_seen, "max_n": self.max_n,
                "ref_price": self.ref_price,
                "actual_at_break": self.actual_at_break,
                "detail": self.detail}


def survival_params() -> Dict:
    """存活层的口径。**故意与 verify_params() 分开。**

    并进 verify_params() 会把 verify_hash 改掉，表 6 全库立刻变「过期」
    （§7.7 会红）。两层各自一个指纹，互不牵连。
    """
    return {"max_n": SURVIVAL_MAX_N,
            "tol_pts": VERIFY_TOL_PTS,
            "pred_rules_version": PRED_RULES_VERSION,
            "status_states": list(SURV_ORDER)}


def survival_hash() -> str:
    import hashlib
    blob = json.dumps(survival_params(), sort_keys=True, ensure_ascii=True)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]


def first_break(bars: Sequence[dict], i: int, prediction: Prediction,
                max_n: int = SURVIVAL_MAX_N,
                tol_pts: float = VERIFY_TOL_PTS) -> Survival:
    """往前走到「第一次被推翻」的那一刻，记下是第几根。

    单调性保证它一定存在（或不存在）：窗口越长越可能破极值，破了不会变回，
    所以「破了没」只会在某一根从 False 翻成 True 一次。

    三种结果必须分开，不许合并：
        已破      → 第 break_n 根被打脸（或证实，看 must_hold）
        活到最后  → 满 max_n 根都没破，**看完了**
        未成熟    → 后面的根不够 max_n，**还没看完** ← 表尾，不许当结论
    """
    if max_n <= 0:
        raise ValueError("max_n 必须 > 0，收到 %r" % (max_n,))
    if prediction.kind == PRED_NONE:
        return Survival(SURV_PENDING, None, 0, max_n, None, None,
                        "这个结论词没有可被推翻的说法")

    avail = max(0, len(bars) - i - 1)
    n_seen = min(avail, max_n)

    g0 = _bar(bars[i])
    ref = prediction.ref_price
    if prediction.kind == PRED_QUIET:
        amp = prediction.amp or max(g0.rng, S.WICK_PTS_MIN)
        best = None
        for k in range(1, n_seen + 1):
            x = bars[i + k]
            ext = max(abs(x["h"] - bars[i]["c"]), abs(x["l"] - bars[i]["c"]))
            if best is None or ext > best:
                best = ext
            if ext >= amp:
                return Survival(SURV_BROKE, k, n_seen, max_n, None, ext,
                                "第 %d 根最大偏离 %.1f 点 >= 全幅 %.1f 点"
                                % (k, ext, amp))
        if avail < max_n:
            return Survival(SURV_PENDING, None, n_seen, max_n, None, best,
                            "后面只有 %d 根，凑不满 %d 根" % (avail, max_n))
        return Survival(SURV_LASTED, None, n_seen, max_n, None, best,
                        "%d 根内最大偏离 %.1f 点，没到全幅 %.1f 点"
                        % (max_n, best if best is not None else 0.0, amp))

    if ref is None or prediction.side is None:
        return Survival(SURV_PENDING, None, n_seen, max_n, None, None,
                        "预言缺价位或方向，未判")

    unit = "低点" if prediction.side == S.SIDE_DOWN else "高点"
    for k in range(1, n_seen + 1):
        x = bars[i + k]
        if prediction.side == S.SIDE_DOWN:
            actual = x["l"]
            breached = actual <= ref - tol_pts
        else:
            actual = x["h"]
            breached = actual >= ref + tol_pts
        if breached:
            word = "跌破" if prediction.side == S.SIDE_DOWN else "突破"
            return Survival(SURV_BROKE, k, n_seen, max_n, ref, actual,
                            "第 %d 根%s%s %.1f（参照 %.1f，容差 %.0f）"
                            % (k, word, unit, actual, ref, tol_pts))
    if avail < max_n:
        return Survival(SURV_PENDING, None, n_seen, max_n, ref, None,
                        "后面只有 %d 根，凑不满 %d 根" % (avail, max_n))
    return Survival(SURV_LASTED, None, n_seen, max_n, ref, None,
                    "%d 根内一直没碰 %s %.1f" % (max_n, unit, ref))


# ---------------------------------------------------------------- 聚合
def summarize(rows: Sequence[dict]) -> Dict:
    """把表 6 的行聚成「命中 / 基准 / 提升 / z」，按 label × horizon 分。

    `基准` 是同口径的**无条件**比率（同一批数据里，「这个预言对随便一根成立吗」），
    必须由调用方通过 `baselines` 传进来 —— 本模块不猜基准，
    因为基准取哪个参照系（本根极值 / 之前 N 根极值 / 距离分层）是口径问题，
    取错了就会得出「稳住无用」这种假结论（本项目犯过）。
    """
    out: Dict[Tuple[str, int], Dict[str, int]] = {}
    for r in rows:
        key = (r["label"], int(r["horizon"]))
        slot = out.setdefault(key, {v: 0 for v in VERDICT_ORDER})
        slot[r["verdict"]] = slot.get(r["verdict"], 0) + 1
    return out


def hit_rate(slot: Dict[str, int]) -> Optional[float]:
    """只在「成立 + 推翻」上算比率。无法判定与无预言**不进分母** ——
    把没验的算进去，等于用缺数据稀释准确率。"""
    denom = slot.get(VERDICT_HELD, 0) + slot.get(VERDICT_BROKE, 0)
    if not denom:
        return None
    return 100.0 * slot.get(VERDICT_HELD, 0) / denom


def lift_z(hits: int, total: int, base_pct: float) -> Tuple[float, float]:
    """提升量（百分点）与 z 值。`base_pct` 是 0-100 的基准率。

    报了提升量还必须报 z：+5 个点在 n=200 上可能是噪声，
    在 n=3,000 上就是真的。项目里「held 52.8% vs 47.2%」那次的教训就是
    只看差了多少点、没看样本。
    """
    if total <= 0:
        return 0.0, 0.0
    p0 = base_pct / 100.0
    p = hits / float(total)
    se = (p0 * (1.0 - p0) / total) ** 0.5
    z = (p - p0) / se if se else 0.0
    return 100.0 * p - base_pct, z
