# -*- coding: utf-8 -*-
"""维度层：把库里已有的 39 个维度摊开成一根 K 线一份「环境快照」。

## 这一层是什么、不是什么

**是**：让人读得懂的说明层。判据（`shape.judge`）给出结论词，这一层给出
「那根 K 线当时所处的环境」：价怎么排、量怎么放、距区间哪儿、有没有线可碰、
碰的时候穿了多深、事件方向如何。

**不是**：判据。这一层的任何值都**不参与**决定结论词，也**不改变**任何阈值。
`shape.judge` 仍然只读 8 个维度（OHLC + vol_rel + taker_buy_ratio + line_price
+ n_touch）。`n_touch` 是「无」拆成「无·真空 / 无·贴线」的唯一依据，
它确实决定 label，所以必须算判据输入 —— 早先台账漏标它，是记账错。

为什么现在要做：全库 39 个维度，判据只吃 8 个，时/空/情绪三类 17 个
**一列没读**。这些维度不是没价值，是没出口 —— 它们躺在表 1/1.5/2 里，
只有打开 SQL 才看得见。用户原话：

> 「其实这么多维度就是说明或者辅助判断：量、价、时、空 + 价格在观察线的反应
>    + 情绪（恐慌/高潮情况）+ 收割（订单块）+ 其他」

所以这一层的职责就是「说明」。

## 分档为什么不算「调阈值」

分档只用三种天然切点，一个都不是拟合出来的：

- 0~1 的有界比例 → 三等分（1/3、2/3）
- 倍数型（vol_rel、vol_slot_rel）→ 相对 1.0（等于均量 / 高于 / 低于）
- 计数型（n_touch、n_lines、nth_touch）→ 0 / 1~2 / 3+

这些切点**不参与判定**，只把数字翻成人话。真正的判据阈值在 `shape.py`
顶部，有 `params_hash` 钉着；这里没有 hash 以外的自由度。

## 单一量规

明细表（表 0 / 1.5 / 2）一根 K 线有多行，摊成一根一行必须有量规。
本层只用一条：**取「穿透最深的那条线」**。理由：穿透深度是「反应」里
唯一有方向、有量级的量，最深的那次就是这根 K 线里最有信息的那次交互。
`side` / `recover_pct` / `close_side` / `line_kind` / `nth_touch` 全部取同一条线
—— 混着取会造出「A 线的方向 + B 线的收回」这种不存在的组合。
"""
from __future__ import annotations

from typing import Dict, List, NamedTuple, Optional, Tuple

# ---------------------------------------------------------------- 分档切点
# 只用于「翻成人话」，不参与判定。见模块文档。
THIRDS = (1.0 / 3.0, 2.0 / 3.0)
COUNT_CUTS = (0, 2)          # 0 / 1~2 / 3+


def _third_word(x: Optional[float]) -> str:
    """0~1 的有界比例 → 三档。None 返回空串（不是 0，别把「没算」说成「小」）。"""
    if x is None:
        return ""
    if x < THIRDS[0]:
        return "小"
    if x < THIRDS[1]:
        return "中"
    return "大"


def _mult_word(x: Optional[float]) -> str:
    """倍数型 → 相均量。1.0 是天然分界（不多不少）。"""
    if x is None:
        return ""
    if x < 1.0:
        return "低于均量"
    if x < 2.0:
        return "约均量"
    return "显著放量"


def _count_word(x: Optional[int]) -> str:
    """计数型 → 0 / 1~2 / 3+。"""
    if x is None:
        return ""
    if x <= COUNT_CUTS[0]:
        return "无"
    if x <= COUNT_CUTS[1]:
        return "少"
    return "密"


class Dim(NamedTuple):
    """一个维度：名字 + 分类 + 取值器 + 分档器 + 一句话解释。"""
    name: str
    cat: str
    get: object            # ctx -> value
    bucket: object         # value -> str（None 表示不分档）
    note: str


class EnvNote(NamedTuple):
    """旁证 / 矛盾提示。**只提示，不改结论。**"""
    kind: str              # "旁证" / "矛盾"
    dims: str              # 牵涉哪些维度，逗号分隔
    text: str              # 人读的一句话

    def as_dict(self) -> Dict:
        return {"kind": self.kind, "dims": self.dims, "text": self.text}


# ---------------------------------------------------------------- 取值上下文
def _v(ctx: Dict, key: str):
    return ctx.get(key)


def _bar(ctx: Dict, key: str):
    return (ctx.get("bar") or {}).get(key)


# ---------------------------------------------------------------- 39 个维度
# 顺序与《维度总账》§三 一致，方便两份文档对读。
#
# ⚠ 2026-11 删除两维：`src_ts` / `line_ready_ms`。它们原本写成
# `_pick(c).get("src_ts")`，而 `_pick` 返回的是 `ctx["reactions"]`（表 2
# `kbar_reaction`）的行 —— 那张表根本没有这两列，所以两个维度
# **结构上永远为 None**（实测两周期非空计数都是 0）。留着它们会让总账
# 声称「41 个维度」而其中 2 个从不曾有过值。线的年龄/可用时刻是表 0
# 的簿记列，不该冒充描述维度；防前视由 `lines.py` 的 `line_ready_ms`
# 门控保证，不需要在说明层再摆一份。
DIMS: Tuple[Dim, ...] = (
    # ---- 价 9 ----
    Dim("o", "价", lambda c: _bar(c, "o"), None, "开盘"),
    Dim("h", "价", lambda c: _bar(c, "h"), None, "最高"),
    Dim("l", "价", lambda c: _bar(c, "l"), None, "最低"),
    Dim("c", "价", lambda c: _bar(c, "c"), None, "收盘"),
    Dim("dir", "价", lambda c: _bar(c, "dir"), None, "涨跌方向"),
    Dim("body_pct", "价", lambda c: _bar(c, "body_pct"), _third_word,
        "实体占全幅"),
    Dim("up_wick_pct", "价", lambda c: _bar(c, "up_wick_pct"), _third_word,
        "上影占全幅"),
    Dim("dn_wick_pct", "价", lambda c: _bar(c, "dn_wick_pct"), _third_word,
        "下影占全幅"),
    Dim("close_pos", "价", lambda c: _bar(c, "close_pos"), _third_word,
        "收位（0=最低 1=最高）"),
    # ---- 量 5 ----
    Dim("vol", "量", lambda c: _bar(c, "vol"), None, "成交量"),
    Dim("vol_rel", "量", lambda c: _bar(c, "vol_rel"), _mult_word,
        "相对近 N 根均量"),
    Dim("vol_slot_rel", "量", lambda c: _bar(c, "vol_slot_rel"), _mult_word,
        "相对同时段均量"),
    Dim("taker_buy_ratio", "量", lambda c: _bar(c, "taker_buy_ratio"),
        _third_word, "主动买量占比"),
    Dim("conc", "量", lambda c: _bar(c, "conc"), _third_word,
        "3s 桶成交集中度"),
    # ---- 时 4 ----
    Dim("idx", "时", lambda c: _bar(c, "idx"), None, "第几根"),
    Dim("seg_idx", "时", lambda c: _bar(c, "seg_idx"), None, "段内第几根"),
    Dim("n_touch", "时", lambda c: _bar(c, "n_touch"), _count_word,
        "碰到几条观察线"),
    Dim("since_event_idx", "时", lambda c: _bar(c, "since_event_idx"),
        _count_word, "距上次事件几根"),
    # ---- 空 10 ----
    Dim("range_ts_h", "空", lambda c: _bar(c, "range_ts_h"), None,
        "所在区间长度"),
    Dim("fam", "空", lambda c: _bar(c, "fam"), None, "区间族"),
    Dim("seg", "空", lambda c: _bar(c, "seg"), None, "区间段"),
    Dim("direction", "空", lambda c: _bar(c, "direction"), None, "区间方向"),
    Dim("zone", "空", lambda c: _bar(c, "zone"), None,
        "价格相对区间的位置"),
    Dim("t_low_min", "空", lambda c: _bar(c, "t_low_min"), None,
        "距区间底几分钟"),
    Dim("t_high_min", "空", lambda c: _bar(c, "t_high_min"), None,
        "距区间顶几分钟"),
    Dim("n_lines", "空", lambda c: _bar(c, "n_lines"), _count_word,
        "本根有几条观察线"),
    Dim("line_period_ms", "空", lambda c: _pick(c).get("line_period_ms"), None,
        "最显著那条线的周期"),
    Dim("dist_pct", "空", lambda c: _nearest_dist(c), _third_word,
        "最近的线有多远"),
    # ---- 观察线的反应 8 ----
    Dim("line_price", "反应", lambda c: _pick(c).get("line_price"), None,
        "最显著那条线的线价"),
    Dim("on_line", "反应", lambda c: _bar(c, "on_line"), None, "收在线上"),
    Dim("line_key", "反应", lambda c: _pick(c).get("line_key"), None,
        "最显著那条线是哪条"),
    Dim("line_kind", "反应", lambda c: _pick(c).get("line_kind"), None,
        "线类型"),
    Dim("side", "反应", lambda c: _pick(c).get("side"), None,
        "从上方还是下方来碰"),
    Dim("pen_depth_pct", "反应", lambda c: _pick(c).get("pen_depth_pct"),
        _third_word, "穿透多深"),
    Dim("recover_pct", "反应", lambda c: _pick(c).get("recover_pct"),
        _third_word, "收回多少"),
    Dim("close_side", "反应", lambda c: _pick(c).get("close_side"), None,
        "收在线哪一侧"),
    # ---- 情绪 3 ----
    Dim("event", "情绪", lambda c: _bar(c, "event"), None, "线事件词"),
    Dim("prev_event", "情绪", lambda c: _bar(c, "prev_event"), None,
        "上一次事件词"),
    Dim("state", "情绪", lambda c: _bar(c, "state"), None, "事件层状态"),
)

# 与总账核对用：维度名 → 分类
DIM_CAT = dict((d.name, d.cat) for d in DIMS)
DIM_NAMES = tuple(d.name for d in DIMS)


# ---------------------------------------------------------------- 单一量规
def _pick(ctx: Dict) -> Dict:
    """「最显著的那条线」= 穿透最深的那行反应。

    没有反应行时返回空 dict（**不是**造一行默认值）—— 空着是可查的，
    填个 0 会让人以为「穿了 0 深」。
    """
    rows = ctx.get("reactions") or []
    best = None
    for r in rows:
        p = r.get("pen_depth_pct")
        if p is None:
            continue
        if best is None or float(p) > float(best.get("pen_depth_pct")):
            best = r
    return best or {}


def _nearest_dist(ctx: Dict) -> Optional[float]:
    """最近的线有多远（占线价的百分比，取绝对值最小）。

    用绝对值：判断「离得远不远」不该被上下方向干扰。
    """
    best = None
    for r in (ctx.get("reactions") or []):
        d = r.get("dist_pct")
        if d is None:
            continue
        v = abs(float(d))
        if best is None or v < best:
            best = v
    return best


# ---------------------------------------------------------------- 快照
def snapshot(ctx: Dict) -> Dict:
    """一根 K 线 → 39 个维度的值 + 分档。

    返回 {"values": {...}, "buckets": {...}}。
    值缺失一律给 None（不补 0），分档缺失给空串。
    """
    values, buckets = {}, {}
    for d in DIMS:
        try:
            v = d.get(ctx)
        except Exception:
            v = None
        values[d.name] = v
        buckets[d.name] = d.bucket(v) if (d.bucket and v is not None) else ""
    return {"values": values, "buckets": buckets}


# ---------------------------------------------------------------- 旁证 / 矛盾
# 规则只用**判据没读的**维度 —— 用判据自己读的列来做「自证」是废话。
# 每条规则的期望方向都来自机制，不含任何数值阈值（除了 0 条/有线可碰这种
# 本来就无参数的判断）。
_UP_CLAIM = ("插针后稳住", "针后远离")     # 这两词的机制是「参照针的极值守住」
_SIDE_TO_LABEL = None


def corroborate(label: str, dims: Dict) -> List[EnvNote]:
    """给出这个结论词的旁证与矛盾提示。

    ⚠ 这些提示**不改变结论**，也不参与任何判定。它们的作用是让人一眼看到
    「这个结论和它的环境是否自洽」，以及「有没有判据没看、但值得一看的东西」。

    参数说明：这里**只收 label 和快照**。早先还有第三个参数 `states`，
    但函数体一次都没引用它（纯死参数），所有调用方也都传 `()` —— 留着它
    会让人以为「旁证看过 states」，实际上没看。要补状态相关的规则，
    当场把 `states` 加回来并真的用上，不要先挂个空壳。
    """
    out: List[EnvNote] = []
    v = dims.get("values") or {}
    nt = v.get("n_touch")
    ev = v.get("event")
    cs = v.get("close_side")

    # ---- 矛盾 1：说「有东西守住」，但一根线都没碰到 ----
    # 这条是判据真没看的：`judge` 判稳住/远离时不读 n_touch。
    if label in _UP_CLAIM and nt is not None and int(nt) == 0:
        out.append(EnvNote(
            "矛盾", "n_touch",
            "结论说「%s」，但本根 n_touch=0 —— 一根观察线都没碰到，"
            "没有线可供「守住」，这个结论只靠几何，没有线的印证" % label))

    # ---- 矛盾 2：结论看多，事件却是向下突破 ----
    if ev and "向下突破" in str(ev):
        if label in ("插针", "插针收回", "插针后稳住", "针后远离"):
            out.append(EnvNote(
                "矛盾", "event",
                "结论是「%s」（针被打回来、偏多），但同根事件是「%s」—— "
                "事件方向与结论相反" % (label, ev)))

    # ---- 旁证 3：「无」却有事件 ----
    # 归因实测：带事件的「无」提升 +4.7~+9.8，无事件的 −3.7。所以这里提示。
    if label in ("无·真空", "无·贴线") and ev:
        out.append(EnvNote(
            "旁证", "event",
            "结论是「%s」（一个形态都没成立），但本根有事件「%s」—— "
            "形态词汇表里没有它，不代表市场没发生事" % (label, ev)))

    # ---- 旁证 4：贴线但穿透很浅 = 强摩擦 ----
    pd = v.get("pen_depth_pct")
    if label == "无·贴线" and pd is not None and float(pd) < THIRDS[0]:
        out.append(EnvNote(
            "旁证", "n_touch,pen_depth_pct",
            "贴线但穿透只有 %.2f%%（浅），价格被压在线上 —— 摩擦强" %
            (float(pd) * 100.0)))

    # ---- 矛盾 5：收位方向与结论方向相反 ----
    # 只在「恐慌盘」上做：它的机制是「情绪极端」，收位是它的核心读数。
    if label == "恐慌盘" and cs:
        cp = v.get("close_pos")
        if cp is not None:
            if str(cs) == "below" and float(cp) > THIRDS[1]:
                out.append(EnvNote(
                    "矛盾", "close_side,close_pos",
                    "恐慌盘收在线下方，但收位 %.0f%% 偏高（收在高处）—— "
                    "两个读数不一致" % (float(cp) * 100.0)))

    return out


# ---------------------------------------------------------------- 口径
DIMS_VERSION = "v1"


def dims_params() -> Dict:
    """维度层的口径。**刻意与 `shape.params()` 分开**：

    这一层不参与判定，所以它的版本变化不该影响表 5 的 `params_hash`，
    否则「加了个描述字段」会让全库判定表看起来过期。
    """
    return {
        "dims_version": DIMS_VERSION,
        "n_dims": len(DIMS),
        "thirds": list(THIRDS),
        "count_cuts": list(COUNT_CUTS),
        "pick_rule": "max_pen_depth_pct",
    }


def dims_hash() -> str:
    import hashlib
    import json
    raw = json.dumps(dims_params(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
