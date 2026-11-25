# -*- coding: utf-8 -*-
"""K 线形态读数（shape readout）：把表 1 已有的原料合成一句「这根是什么」。

为什么有这个模块
----------------
在此之前，这个能力只输出客观值（`n_touch=3`、`vol_rel=7.35`、`dn_wick_pct=0.77`）。
用户的原话：「你只是包含客观的值，没有判断」。
绝对值不是读数 —— 一根下影 166 点，读者还得自己判断这算不算插针。
本模块把那句判断显式写下来，可数、可复核、可争论。

**这不是设计稿禁止的「第 6 步 判断标准」。**
第 6 步禁止的是给线定性 ——「EMA200 是支撑」。那是把观察线变成决策依据。
本模块只描述**已经发生的事**：这根有没有插针、有没有收回来、是不是恐慌。
它不回答「所以该不该买」，也不回答「哪条线是支撑」。两者的区别是：
判断标准产出**仓位动作**，本模块产出**一句描述**。空仓时本模块照样输出。

判据的单位（写死，不许含糊）
---------------------------
- `WICK_PCT_MIN`     无量纲，影线 / 全幅
- `WICK_PTS_MIN`     **绝对价差，BTC 上 1 点 = 1 USDT**
- `SCENARIO_TOL_PTS` **绝对价差，点**
- 其余全是比值（vol_rel / taker_buy_ratio / 倍针幅 / 收位）

`SCENARIO_TOL_PTS = 20` 只用在 `after_pin` 这一个判据里（判「有没有破针的极值」）。
**不许扩散。** 用户原话：「我设置的这个容差只在这一个场景生效！别扩散！」
特别是**不要**去改 `reaction.TOUCH_TOL_PCT`（那是表 2 的入表条件，
一改表 2 行数、`nth_touch`、表 3 的 `held` 全部跟着变）。

v2：为什么「针」不再限定成单根长影线
------------------------------------
v1 只认单根长影线（影线占幅 >= 65%），2026-11-08 用户给了 6 根反例
（北京 11-04 10:00 / 10:15 / 10:30 / 10:45 / 11:15 / 11:30），全部判成「无」。
逐根核对后，v1 有两个结构性漏洞，都不是阈值问题：

**漏洞一：收在极值的大阴线在数学上没有影线，所以永远不是「针」。**

    收位 0%  =>  c == l  =>  下影 = min(o,c) - l = 0

2026-11-04 10:45（北京）就是这根：全幅 526.9 点、实体 98%、收在最低点、
量 3.09 倍、主动买 0.311。这是教科书式的恐慌砸盘，但 v1 因为「不是针」，
连「恐慌盘」也一起否掉 —— 恐慌盘被判据的前置条件连坐了。
全库这种「收在极值 + 巨量 + 主动性占优」的根有 181 根，v1 一根都没报。

所以 v2 把「恐慌盘」和「插针」解耦：
    插针   = 价格被推出去又拉回（形态）
    恐慌盘 = 量能 + 主动性 + 收盘落在被打的那一侧（力度）
恐慌盘**不再要求本根是针**。方向和力度各有各的判据，谁也不是谁的前置。

**漏洞二：一段下跌的最低点不是某一根的长影线，所以「针」找不出来。**

2026-11-04 11:00（北京）打出全波最低 75,690.2，但那是逐根阴线阴下来的，
**没有任何一根影线到 65%**（11:00 下影占幅只有 36%）。
11:15 从 75,868 收回到 76,319.5、收位 93%、主动买 0.659 —— 实实在在的稳住，
但 v1 的 `find_pin_before` 只认长影线，找不到那根针。
全库范围里「跨根收回」的形态有 1,046 根，其中 262 根 v1 判成「无」。

所以 v2 把「针」扩成两种形态（并集）：

    wick   单根长影线（v1 的口径，原样保留）
    swing  已确认的局部极值 —— 往回看 `SWING_LOOKBACK_BARS` 根，
           若某一根的低点是这段窗口里的最低、且低于它前一根，就是下针

**`swing` 的定义只用「当前及之前」的 K 线，不看未来。** 这一条是硬约束：
判第 `i` 根时，只能拿 `bars[0..i]`。所以「局部极值」不是「后面几根没破」，
而是「到今天为止它是这段窗口的最低」。改坏了就是偷未来，回测会用它赚钱。

**两种针的「针幅」为什么算法不同：**
    wick  针幅 = 那根自己的全幅（影线是它自己的）
    swing 针幅 = 从窗口最高点冲进极值的落差（极值没有自己的全幅可言）

swing 为什么不能用自己的全幅：极值那根可能是个十字星（全幅极小），
拿它当尺会让所有距离都变成「远离」。也不能用「窗口内最宽那根的全幅」——
实测那样「稳住」占 32%，比用落差（33%）差别不大但语义绕。
「冲进极值的落差」正是「这波被打出去多远」，与长影线的「影线有多长」对称。

**确认窗口取 8 根（不是 3）：** 实测 3 根窗口里几乎总有一个低点（全库 4,829
根都满足），那不是枢轴、是噪声，「稳住」会涨到 32%。放到 8 根剩 3,063 根，
「局部极值」才是结构性事实。实测各档（稳住 / swing 针）：

    3 根 → 5,699 / 4,829      8 根 → 4,450 / 3,063
    5 根 → 5,214 / 3,953     12 根 → 3,900 / 2,473
    6 根 → 4,933 / 3,617     16 根 → 3,499 / 2,050

**「稳住」变多是针变多的必然结果，不是判据出错。** 稳住数 ≈ 0.75 × 针数，
v1 也一样（2,078 根长影线针 → 1,565 根稳住）。针从 2,078 涨到 5,141
（长影线 + 局部极值），稳住自然跟着涨。这一点在 D-21 里挂着等用户定。

阈值怎么来的
------------
不是拍的，是扫出来的。17,654 根上的命中率：

    影占>=60% + 影>=40点  → 下针 1,472 / 上针 1,475   合计 16.69%
    影占>=65% + 影>=40点  → 下针 1,062 / 上针 1,016   合计 11.77%  <- 取这一档
    影占>=70% + 影>=40点  → 下针   749 / 上针   713   合计  8.28%

取 65 这一档的理由：它把 2026-11-07 23:45 和 2026-11-08 04:45 两根都收进来，
同时把 05:00（下影 10.2 点 = 10%）和 05:15（下影 10.2 点 = 25%）都排除 ——
后者是「稳住」，不是「针」。命中率 11.77% 与「插针是少数事件」的常识一致。

`PANIC_CLOSE_POS_MAX = 0.25` 的来源：恐慌盘要「价格确实被打到一侧」。
取 0.25 时全库 181 根（下压 43 / 上冲 48 是更早的粗筛，含重叠加总后 181）；
取 0.05 只剩 86 根，会把 2026-11-04 10:45（收位 0%）之外的同类漏掉。
取 0.40 会把 11:00（收位 36%、量 6.34 倍）也收进来，但那根主动买 0.456
刚刚越过 0.45 的上限，力度判据自己就没过 —— 两处都差一点，所以先不放大。

**为什么 `close_pos >= 0.65` 这个条件已经删掉**
------------------------------------------------
v1 一开始写了三个条件，第三个是 `close_pos >= 0.65`。它是个**死条件**：

    c >= min(o, c)  =>  c - l >= min(o, c) - l  =>  收位 >= 下影占幅

所以只要「下影占幅 >= 0.65」，「收位 >= 0.65」必然成立。实测反例 **0 根**，
开关这个条件命中数完全不变。**注意 v2 的 `panic_side` 用的是 `close_pos`，
那不是这条死条件复活** —— 它约束的是「恐慌盘」而不是「插针」，
不跟影线占幅绑定，所以既不恒真也不冗余。

**方向顺序是防御性的，但永不触发**：`dn_pct + up_pct + body_pct == 1`，
所以上下影不可能同时 >= 0.65。`pin_side` 先判下针只是为了让同一根永远得到
同一个答案，不是因为真的会撞上。

「稳住」的距离上限用**针自己的幅度**做尺，不用固定点数：
一根 216 点的针之后走出 140 点（0.65 倍针幅）还算在针的范围内；
一根 40 点的针之后走出 140 点就是另一段行情。实测 3,547 个「未破针」样本：
中位 0.56 倍、p75 0.90、p90 1.40。取 1.0 倍。

不写什么
--------
- 不写「主力在诱多」这类自由文本（设计稿 §8.4）。只是一组枚举 + 证据串。
- 不给方向建议，不产出信号。
- 不碰第 5 步（`forward.py`）：事后信息不许混进当下读数，回测会偷未来。

落库
----
判定**要落库**（用户 2026-11-08 明确要求：「所有演化结论字段或者描述都要入 db 库」）。
落在 `data/kbar.db` 的表 5 `kbar_shape`，见 `store.py`。
每行带 `params_hash` —— 阈值是临时的（D-21），落了库就等于把临时值冻成历史，
所以谁改了阈值而没重跑，`tools/check_kbar_pipeline.py` 立刻报红。
本模块仍然只做纯计算、不碰 IO，落库在 `build.py`。
"""
from __future__ import annotations

from typing import Dict, List, NamedTuple, Optional, Sequence, Tuple

# ---------------- 阈值（唯一出处，调参只改这里）----------------
WICK_PCT_MIN = 0.65        # 影线 / 全幅，无量纲
WICK_PTS_MIN = 40.0        # 影线绝对点数（绝对价差）
PANIC_VOL_REL_MIN = 3.0    # 本根量 / 前 20 根中位量
PANIC_TAKER_BUY_MAX = 0.45  # 主动买占比上限（恐慌砸盘时主动卖占优）
STABLE_LOOKBACK_BARS = 3   # 回看几根找针
SCENARIO_TOL_PTS = 20.0    # 【只在本场景生效】「不破针极值」允许的点数余量
STABLE_MAX_PIN_RANGE_MULT = 1.0  # 「稳住」允许离针多远 = N × 针自己的幅度

# ---- v2 新增（同样是临时值，见 D-21）----
SWING_LOOKBACK_BARS = 8    # 「局部极值针」的确认窗口：往回看几根才算真枢轴
PANIC_CLOSE_POS_MAX = 0.25  # 恐慌盘要求收盘落在被打出去的那一侧（下压 <=，上冲 >= 1-）

# ---------------- 针的两种形态 ----------------
PIN_KIND_WICK = "wick"     # 单根长影线
PIN_KIND_SWING = "swing"   # 已确认的局部极值（只用「今天为止」的数据）

# ---------------- 状态词（枚举，不许自由文本）----------------
SIDE_DOWN = "down"
SIDE_UP = "up"

STATE_PANIC = "恐慌盘"
STATE_PIN_RECOVERED = "插针收回"
STATE_PIN = "插针"
STATE_STABLE = "插针后稳住"
STATE_NEAR_PIN = "针后远离"
STATE_NONE = "无"

# ---- 「无」拆词（D-23.4，用户 2026-11-10 定：拆成两个词）----
#
# 为什么要拆：「无」占全库 60.2%，整体提升只有 +0.8 点（z=1.7），看着零信息。
# 但实测它内部是两个**相反**的群体被一个词盖住了（N=1，同口径基准 68.2%）：
#     n_touch=0（3,865 根）→ 62.4%，提升 **−5.8**
#     n_touch=1（2,763 根）→ 68.4%，提升 +0.2
#     n_touch=2（2,104 根）→ 72.1%，提升 +4.0
#     n_touch>=3（1,875 根）→ 79.4%，提升 +11.3
# 机制：一根观察线都没碰到 = 价格在**真空区**，没有东西拦它，更容易单边走；
# 碰到了线 = 被按在一个区域里。这正是「线的密度 = 摩擦」。
#
# 切分点选 0 | ≥1，**不选** ≤2 | ≥3：
#     后者跨度更大（12.8 vs 10.3），但那是把 −5.8 和 +4.0 **平均掉**换来的，
#     等于又造一个「相反群体塞进同一个词」—— 正是这次要修的病。
#     0 | ≥1 保留了「真空」这个负信息段的纯度。
#     若将来要更细，0 | 1~2 | ≥3 实测跨度 17.0（3,865 / 4,867 / 1,900），
#     那是升级项，不是现在。
LABEL_NO_EMPTY = "无·真空"    # n_touch == 0：一根观察线都没碰到
LABEL_NO_NEAR = "无·贴线"     # n_touch >= 1：碰到了线，价格被按在某个区域
NO_SPLIT_BY_TOUCH = 1         # 切分点：n_touch < 1 → 真空

# 「没有形态」这一族的所有词。凡是要判断「这根是不是没形态」的地方都用它，
# 不要把 STATE_NONE 硬编码成唯一答案。
LABEL_NO_ALL = (STATE_NONE, LABEL_NO_EMPTY, LABEL_NO_NEAR)

# 判定规则版本。**与 `params_hash` 分工不同**：
#   `params_hash` 只回答「这行是不是用现在这组**阈值**算的」，刻意不含逻辑，
#   所以它抓不到「词表变了 / 判定规则变了」。这里补上那个开关。
#   v1 = 「无」一个词；v2 = 拆成 无·真空 / 无·贴线（D-23.4，2026-11-10）。
# 行级检测不靠它，靠数据本身：v1 的行写「无」，v2 的行写「无·真空/贴线」，
# 所以「表 5 里裸『无』必须为 0」就是混合词表的检测器（见 check_kbar_pipeline §7.8）。
JUDGE_RULES_VERSION = "v2"

# ⚠ `n_touch` 的唯一来源是**表 1 落库值**，判定层不许自己数。
#   实测 17,654 根里有 2,518 根（14.3%）按 `reaction.is_touch` 自己数出来的
#   与表 1 不一致（自己数偏少）—— 自己算会造出两套 n_touch。
#   拿不到就退回 STATE_NONE 并在证据里写明，**不许猜**。

# 判定优先级：越靠前越先报。理由与 kline_intent 一致 ——
# 恐慌盘是「反方向的插针」，最容易被误读成突破，必须先排掉。
STATE_ORDER = (STATE_PANIC, STATE_PIN_RECOVERED, STATE_PIN,
               STATE_STABLE, STATE_NEAR_PIN)


class Geometry(NamedTuple):
    """一根 K 线的几何。全部由本根自己的 OHLC 算出，不看邻居。"""
    o: float
    h: float
    l: float
    c: float
    rng: float        # 全幅 h - l，已做 1e-9 下限保护
    body: float       # |c - o|
    up: float         # 上影 h - max(o, c)
    dn: float         # 下影 min(o, c) - l
    up_pct: float     # 上影 / 全幅
    dn_pct: float     # 下影 / 全幅
    close_pos: float  # (c - l) / 全幅


class Judgement(NamedTuple):
    """一根 K 线的读数。`states` 保留全部成立的状态，`label` 是优先级最高的那个。

    两个「针」的槽位别混：

        side / pin_kind        **本根自己**是针时的方向和形态；不是针则 None
        pin_ts / ref_pin_kind  「稳住」「远离」时**参照的那根针**是哪根、什么形态

    混成一个字段就会说不清「这根的形态是 swing」到底是它自己是极值，
    还是它稳住的是个极值 —— 落库后没人能分辨，也没法复核。
    """
    states: Tuple[str, ...]
    label: str
    side: Optional[str]       # 本根自己是针时的方向；不是针则 None
    evidence: Tuple[str, ...]
    pin_ts: Optional[int]     # 参照的针（稳住/远离时它稳的是哪一根）
    pin_kind: Optional[str] = None      # 本根自己是针时的形态 'wick'/'swing'
    ref_pin_kind: Optional[str] = None  # 参照那根针的形态
    ref_pin_side: Optional[str] = None  # 参照那根针的方向（down=低点针）

    def has(self, state: str) -> bool:
        return state in self.states

    def as_dict(self) -> dict:
        return {"states": list(self.states), "label": self.label,
                "side": self.side, "evidence": list(self.evidence),
                "pin_ts": self.pin_ts, "pin_kind": self.pin_kind,
                "ref_pin_kind": self.ref_pin_kind,
                "ref_pin_side": self.ref_pin_side}


def params() -> Dict[str, float]:
    """当前判据的全部阈值，**唯一出处**。落库时用来算 `params_hash`。"""
    return {
        "wick_pct_min": WICK_PCT_MIN,
        "wick_pts_min": WICK_PTS_MIN,
        "panic_vol_rel_min": PANIC_VOL_REL_MIN,
        "panic_taker_buy_max": PANIC_TAKER_BUY_MAX,
        "panic_close_pos_max": PANIC_CLOSE_POS_MAX,
        "stable_lookback_bars": STABLE_LOOKBACK_BARS,
        "scenario_tol_pts": SCENARIO_TOL_PTS,
        "stable_max_pin_range_mult": STABLE_MAX_PIN_RANGE_MULT,
        "swing_lookback_bars": SWING_LOOKBACK_BARS,
    }


def params_hash() -> str:
    """阈值的指纹。落库每行都带它 —— 阈值一改，全表的行就都成了旧口径。

    这里刻意**不**包含逻辑代码的哈希：代码改了要人重跑，指纹只负责回答
    「这一行是不是用现在这组阈值算的」。两件事分开才查得清。
    """
    import hashlib
    import json
    raw = json.dumps(params(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------- 几何
def _fmt_ts(ts) -> str:
    """毫秒时间戳 → `2026-11-08 04:45 (UTC)`。证据串给人看，不许印 13 位数字。"""
    if ts is None:
        return "?"
    try:
        import datetime
        return datetime.datetime.utcfromtimestamp(int(ts) / 1000).strftime(
            "%Y-%m-%d %H:%M") + " UTC"
    except (TypeError, ValueError, OSError):
        return str(ts)


def geometry(o: float, h: float, l: float, c: float) -> Geometry:
    """算一根 K 线的几何。全幅为 0（一字板）时用 1e-9 兜底，避免除零。"""
    o, h, l, c = float(o), float(h), float(l), float(c)
    rng = max(h - l, 1e-9)
    body = abs(c - o)
    up = h - max(o, c)
    dn = min(o, c) - l
    # 浮点残差可能让 up/dn 掉到 -1e-13，钳回 0
    up = up if up > 0 else 0.0
    dn = dn if dn > 0 else 0.0
    return Geometry(o=o, h=h, l=l, c=c, rng=rng, body=body, up=up, dn=dn,
                    up_pct=up / rng, dn_pct=dn / rng,
                    close_pos=(c - l) / rng)


def pin_side(g: Geometry,
             wick_pct_min: float = WICK_PCT_MIN,
             wick_pts_min: float = WICK_PTS_MIN) -> Optional[str]:
    """是不是插针，返回方向或 None。

    两个条件必须同时成立：
        1. 影线占全幅 >= wick_pct_min  —— 价格确实被打出去很远
        2. 影线 >= wick_pts_min 点      —— 占幅会被小 K 线放大，
                                          光看比例 3 点的影线也能占 70%

    不需要「收盘收回」这个条件：它由条件 1 保证（见模块 docstring 的推导）。
    """
    if g.dn_pct >= wick_pct_min and g.dn >= wick_pts_min:
        return SIDE_DOWN
    if g.up_pct >= wick_pct_min and g.up >= wick_pts_min:
        return SIDE_UP
    return None


# ---------------------------------------------------------------- 线收回
def recovered_lines(g: Geometry,
                    prev_lines: Optional[Dict[str, float]]) -> List[Tuple[str, float, str]]:
    """影线穿透了、收盘又回到另一侧的观察线。

    `prev_lines` 必须是**上一根**的线快照（来自表 0），不是本根的。
    用本根的线价 = 偷未来（线在本根收盘后才更新）。没有上一根 = 当时那条线不存在。

    从哪一侧来用**开盘价**判（与 D-6 的口径一致），否则一根跨线的 K 会两头都算：

        from_above（o > p）：最低穿到线下、收盘回到线上   -> `l < p < c`
        from_below（o < p）：最高穿到线上、收盘回到线下   -> `c < p < h`
        开盘正好在线上（o == p）：不判 —— 没有「从哪侧来」可言

    返回 [(line_key, line_price, "dn"|"up"), ...]，按 line_key 升序。
    """
    if not prev_lines:
        return []
    out: List[Tuple[str, float, str]] = []
    for key in sorted(prev_lines):
        p = prev_lines[key]
        if p is None:
            continue
        p = float(p)
        if g.o > p:
            if g.l < p < g.c:
                out.append((key, p, SIDE_DOWN))
        elif g.o < p:
            if g.c < p < g.h:
                out.append((key, p, SIDE_UP))
    return out


# ---------------------------------------------------------------- 恐慌盘
def panic_side(g: Geometry,
               close_pos_max: float = PANIC_CLOSE_POS_MAX) -> Optional[str]:
    """恐慌/亢奋的方向。

    **有影线就听影线的，没有影线才看收位。** 这一条踩过坑：

        2026-11-07 23:45（北京）下影 166 点 = 77%，收位也是 77%（打完被买回来）
        只看收位   -> 77% >= 75% -> 判成「上冲」
        只看影线   -> 下针       -> 判成「下压」  <- 正确

        2026-11-04 10:45（北京）下影 0.4 点，收位 0%（砸下去没回收）
        只看影线   -> 没有针     -> 判不出方向
        只看收位   -> 0%         -> 判成「下压」  <- 正确

    两根都需要，所以是「影线优先、收位兜底」：
        `pin_side(g)` 有结果就用它（价格探出去的方向才是被打的方向）
        否则用收位（收在极值 = 价格被按住没回来）

    参数必须 < 0.5，否则上下两个分支可能同时成立。
    """
    assert 0.0 < close_pos_max < 0.5, "close_pos_max 必须在 (0, 0.5) 内"
    wick = pin_side(g)
    if wick is not None:
        return wick
    if g.close_pos <= close_pos_max:
        return SIDE_DOWN
    if g.close_pos >= 1.0 - close_pos_max:
        return SIDE_UP
    return None


def is_panic(g: Geometry, vol_rel: Optional[float],
             taker_buy_ratio: Optional[float],
             side: Optional[str] = None,
             vol_rel_min: float = PANIC_VOL_REL_MIN,
             taker_buy_max: float = PANIC_TAKER_BUY_MAX,
             close_pos_max: float = PANIC_CLOSE_POS_MAX) -> bool:
    """放量 + 主动性占优 + 收盘落在被打的那一侧。

    **v2 起不再要求本根是针。** v1 把 `pin_side` 当前置条件，导致
    「收在最低点的大阴线」（数学上没有下影线）永远判不出恐慌盘，
    全库漏 181 根。见模块 docstring「漏洞一」。

    `side` 传 None 时用 `panic_side` 自己算（与 `pin_side` 无关）。
    量或主动性缺失时返回 False —— 缺数据不判成「是」，由 evidence 里说明。
    """
    if side is None:
        side = panic_side(g, close_pos_max=close_pos_max)
    if side is None:
        return False
    if vol_rel is None or taker_buy_ratio is None:
        return False
    if vol_rel < vol_rel_min:
        return False
    if side == SIDE_DOWN:
        return taker_buy_ratio <= taker_buy_max
    # 上冲的对称口径：主动买占优（>= 1 - 0.45 = 0.55）
    return taker_buy_ratio >= (1.0 - taker_buy_max)


# ---------------------------------------------------------------- 针：两种形态
def swing_pin_kind(bars: Sequence[dict], j: int, end: int,
                   look: int = SWING_LOOKBACK_BARS,
                   min_depth: float = WICK_PTS_MIN) -> Optional[str]:
    """`bars[j]` 是不是一个**已确认**的局部极值。`end` 是当前判定的那根（>= j）。

    「已确认」的定义严格限制在**今天为止**的数据上：

        j 是这段窗口 [j-look, end] 里最低的那一根，且低于它前一根，
        且从窗口最高点跌进这个低点的落差 >= min_depth            -> down

    **不许看 `end` 之后。** 写成「后面几根没破」就是偷未来，
    回测会拿这个赚钱，而实盘第 j 根收盘时根本不可能知道。

    `look` 取 8 而不是 3：实测 3 根窗口里几乎总有一个低点（全库 4,829 根都算针），
    那不是枢轴，是噪声。放到 8 根剩 3,063 根，「局部极值」才是个结构性的事实。

    `min_depth` 与长影线的 `WICK_PTS_MIN` 同一个数，含义也一样 ——
    都被要求「确实被打出去这么远」。实测这道门槛几乎不额外筛（8 根窗口下
    只从 3,063 减到 3,020），但少了它就等于允许 3 点的抖动当针。
    """
    if j <= 0 or end < j:
        return None
    b = bars[j]
    lo = max(0, j - look)
    lows = [bars[k]["l"] for k in range(lo, end + 1)]
    highs = [bars[k]["h"] for k in range(lo, end + 1)]
    if b["l"] <= min(lows) and b["l"] < bars[j - 1]["l"]:
        if lo < j and max(bars[k]["h"] for k in range(lo, j)) - b["l"] >= min_depth:
            return SIDE_DOWN
    if b["h"] >= max(highs) and b["h"] > bars[j - 1]["h"]:
        if lo < j and b["h"] - min(bars[k]["l"] for k in range(lo, j)) >= min_depth:
            return SIDE_UP
    return None


def swing_pin_depth(bars: Sequence[dict], j: int,
                    look: int = SWING_LOOKBACK_BARS) -> float:
    """局部极值针的幅度：从窗口最高点冲进极值的落差（上针反之）。

    与 `pin_amp` 的 `wick` 分支为什么不同：长影线针的幅度是**它自己的全幅**
    （影线是它自己的），而极值针没有「自己的全幅」可言 —— 它可能是个十字星。
    这里用「冲进极值的落差」，因为它正是「这波被打了多远」。
    """
    lo = max(0, j - look)
    if lo >= j:
        return 0.0
    down = max(bars[k]["h"] for k in range(lo, j)) - bars[j]["l"]
    up = bars[j]["h"] - min(bars[k]["l"] for k in range(lo, j))
    return max(down, up)


def pin_amp(bars: Sequence[dict], j: int,
            kind: str = PIN_KIND_WICK,
            look: int = SWING_LOOKBACK_BARS) -> float:
    """针的幅度（当尺用）—— 两种形态都取**极值那根自己的全幅**。

    swing 也用自己的全幅，与 wick 一致。试过另外两种，都不行：

        窗口内最宽那根的全幅   「稳住」32%，且与极值那根的大小无关，语义绕
        冲进极值的落差         11:00 那根算出 1,618 点，把尺撑得太大，
                               177.8 点只占 0.11 倍 -> 几乎全部判「稳住」，
                               「针后远离」从 2.2% 掉到 2.1% 且基本上是噪声

    落差只当**门槛**用（`swing_pin_kind` 的 `min_depth`），不当尺 ——
    门槛回答「这算不算被打出去」，尺回答「走出去多远算远离」，两件事。

    下限 `WICK_PTS_MIN` 是防止极值那根是十字星（全幅 2 点）时除出天文数字。
    """
    own = geometry(bars[j]["o"], bars[j]["h"], bars[j]["l"], bars[j]["c"]).rng
    return max(own, WICK_PTS_MIN)


# ---------------------------------------------------------------- 稳住
def find_pin_before_ex(bars: Sequence[dict], i: int,
                       look: int = STABLE_LOOKBACK_BARS,
                       side: str = SIDE_DOWN,
                       allow_swing: bool = True,
                       swing_look: int = SWING_LOOKBACK_BARS
                       ) -> Optional[Tuple[int, str]]:
    """往回 look 根找最近一根同方向的针，返回 `(下标, 形态)` 或 None。

    只看 `i` 之前的根（`i-1` 到 `i-look`），**不含 `i`**。
    同一根既像长影线又像极值时按 `wick` 报 —— 影线是更具体的证据。

    `allow_swing=False` 就退回 v1 的口径（只认长影线），用于对照测试。
    """
    if i <= 0:
        return None
    lo = max(0, i - look)
    for k in range(i - 1, lo - 1, -1):
        b = bars[k]
        g = geometry(b["o"], b["h"], b["l"], b["c"])
        if pin_side(g) == side:
            return k, PIN_KIND_WICK
    if not allow_swing:
        return None
    for k in range(i - 1, lo - 1, -1):
        if swing_pin_kind(bars, k, i - 1, look=swing_look) == side:
            return k, PIN_KIND_SWING
    return None


def find_pin_before(bars: Sequence[dict], i: int,
                    look: int = STABLE_LOOKBACK_BARS,
                    side: str = SIDE_DOWN,
                    allow_swing: bool = True) -> Optional[int]:
    """同 `find_pin_before_ex`，只返回下标（兼容 v1 调用方）。"""
    got = find_pin_before_ex(bars, i, look=look, side=side,
                             allow_swing=allow_swing)
    return None if got is None else got[0]


def pin_extreme_kept(bars: Sequence[dict], j: int, i: int, side: str,
                     tol_pts: float = SCENARIO_TOL_PTS) -> bool:
    """从针那一根的后一根到 `i`（含 `i`），针的极值有没有被**任何**一根实质破掉。

    这是「稳住」必须问的那个问题，也是 v3 补的那一刀：

        原来的 `after_pin` 只量**本根**（`g.l >= gp.l - tol_pts`）。
        于是一根 K 线在针的极值下方 40 点待了 2 根、又收回针上方，
        本根与针极值一比是「没破」—— 就叫成了「插针后稳住」。

    **这不是笔误，是量错了对象**：「稳住」说的是一段价格行为，
    不是某一根 K 线。只量本根 == 只看了这段的最后一眼。

    实测（BTCUSDT@15m，3,992 根稳住）：1,145 根（28.7%）的参照针
    并不是 `[j, i]` 这段里的最低点；按方向控制后（向下 290 根），
    其中 68 根（23.4%）中间某根真的破了针低 20 点以上又回来了。

    反过来也反证过：A 类（参照针确实是那段最低）1 根都没有「本根真破」，
    说明这条判据本身没写错，只是量的范围短了。
    """
    if i <= j:
        return True
    if side == SIDE_DOWN:
        floor = geometry(bars[j]["o"], bars[j]["h"], bars[j]["l"],
                         bars[j]["c"]).l - tol_pts
        for k in range(j + 1, i + 1):
            if bars[k]["l"] < floor:
                return False
        return True
    ceil = geometry(bars[j]["o"], bars[j]["h"], bars[j]["l"],
                    bars[j]["c"]).h + tol_pts
    for k in range(j + 1, i + 1):
        if bars[k]["h"] > ceil:
            return False
    return True


def after_pin_full(bars: Sequence[dict], i: int,
                   look: int = STABLE_LOOKBACK_BARS,
                   tol_pts: float = SCENARIO_TOL_PTS,
                   max_pin_range_mult: float = STABLE_MAX_PIN_RANGE_MULT,
                   allow_swing: bool = True,
                   path_aware: bool = True
                   ) -> Optional[Tuple[int, str, str]]:
    """`after_pin` 的完整版，多返回一个针的形态 `(下标, 状态, 'wick'|'swing')`。

    判定逻辑只有这一份，`after_pin` 是它的二值投影 —— 两份逻辑迟早漂移，
    而漂移的表现是「稳住」判出来了、落库的形态却是另一个。

    `path_aware=False` 退回 v2 口径（只量本根有没有破针极值），
    只用于对照测试与探针，证明 v3 这一刀改了哪些根 —— 生产路径永远是 True。
    """
    b = bars[i]
    g = geometry(b["o"], b["h"], b["l"], b["c"])
    if pin_side(g) is not None:
        return None
    for side in (SIDE_DOWN, SIDE_UP):
        got = find_pin_before_ex(bars, i, look=look, side=side,
                                 allow_swing=allow_swing)
        if got is None:
            continue
        j, kind = got
        pj = bars[j]
        gp = geometry(pj["o"], pj["h"], pj["l"], pj["c"])
        amp = pin_amp(bars, j, kind)
        if side == SIDE_DOWN:
            holds = g.l >= gp.l - tol_pts
            dist = g.l - gp.l
        else:
            holds = g.h <= gp.h + tol_pts
            dist = gp.h - g.h
        # 本根没破还不够 —— 中间那些根也不能破过（见 pin_extreme_kept）
        if holds and path_aware and not pin_extreme_kept(bars, j, i, side, tol_pts):
            holds = False
        firm = (g.c > g.o) or (g.close_pos >= 0.5)
        if not (holds and firm):
            continue
        if dist <= max_pin_range_mult * amp:
            return j, STATE_STABLE, kind
        return j, STATE_NEAR_PIN, kind
    return None


def after_pin(bars: Sequence[dict], i: int, **kw) -> Optional[Tuple[int, str]]:
    """本根在不在「一根针之后」，返回 (针的下标, 状态) 或 None。

    状态只有两种：
        `插针后稳住` —— 没破针的极值，且离针不远（<= max × 针自己的幅度）
        `针后远离`   —— 没破针的极值，但已经走远了（再叫「稳住」就是编）

    为什么不给「距针」写死一个点数：针本身有大有小，见模块 docstring。

    四个条件，缺一不可：
      1. 本根自己**不是针**（是针就是针，不能又叫稳住）
      2. 前 `look` 根内有同方向的针（长影线 或 已确认的局部极值，取决于 allow_swing）
      3. 没有实质破掉针的极值（`l >= pin.l - tol_pts`，上针则 `h <= pin.h + tol_pts`）
      4. 收盘不弱（收阳或收位 >= 50%）

    `tol_pts` 就是 `SCENARIO_TOL_PTS`，**只在这里用**。
    """
    got = after_pin_full(bars, i, **kw)
    return None if got is None else (got[0], got[1])


def stable_of(bars: Sequence[dict], i: int,
              look: int = STABLE_LOOKBACK_BARS,
              tol_pts: float = SCENARIO_TOL_PTS,
              max_pin_range_mult: float = STABLE_MAX_PIN_RANGE_MULT) -> Optional[int]:
    """只要「插针后稳住」（不要「针后远离」）时用这个。返回针的下标或 None。"""
    r = after_pin(bars, i, look=look, tol_pts=tol_pts,
                  max_pin_range_mult=max_pin_range_mult)
    if r is None or r[1] != STATE_STABLE:
        return None
    return r[0]


# ---------------------------------------------------------------- 跨根收回
def recovered_after_pin(g: Geometry, pin_bar: dict,
                        lines_at_pin: Optional[Dict[str, float]],
                        side: str) -> List[Tuple[str, float, str]]:
    """**跨根**收回：针那一根穿透了线但没收回来，本根收回来了。

    与 `recovered_lines` 的区别：

        recovered_lines      同一根 K 线内「穿下去又收上来」
        recovered_after_pin  针在 `j` 根（穿透未收回），`i` 根才收回来

    为什么需要它：2026-11-04 11:00（北京）打出全波最低 75,690.2，
    11:15 收回到 76,319.5 —— 收回发生在**针之后的根**上，
    任何只看本根 OHLC 的判据都看不见这件事。

    `lines_at_pin` 是**针那一根的上一根**的线快照（表 0 的 `ts = pin_ts - interval`），
    不是本根的线快照。用本根的线价 = 拿后来移动过的线去判当时有没有穿透。

    返回 [(line_key, line_price, "dn"|"up"), ...]，按 line_key 升序。
    """
    if not lines_at_pin:
        return []
    out: List[Tuple[str, float, str]] = []
    pg = geometry(pin_bar["o"], pin_bar["h"], pin_bar["l"], pin_bar["c"])
    for key in sorted(lines_at_pin):
        p = lines_at_pin[key]
        if p is None:
            continue
        p = float(p)
        if side == SIDE_DOWN:
            # 针那根穿透了（最低低于线）但收盘没收回来（收在线下）
            if not (pg.l < p and pg.c <= p):
                continue
            # 本根收回到线上
            if g.c > p:
                out.append((key, p, SIDE_DOWN))
        else:
            if not (pg.h > p and pg.c >= p):
                continue
            if g.c < p:
                out.append((key, p, SIDE_UP))
    return out


# ---------------------------------------------------------------- 合成
def _why_not_panic(g: Geometry, vol_rel, tbr, side,
                   vol_rel_min=PANIC_VOL_REL_MIN,
                   taker_buy_max=PANIC_TAKER_BUY_MAX,
                   close_pos_max=PANIC_CLOSE_POS_MAX) -> str:
    """没判成恐慌盘的理由。**必须说清是哪一条不够**，不许只说「否」。"""
    why = []
    if side is None:
        why.append("收位 %.0f%% 不在两侧（<=%.0f%% 或 >=%.0f%%），方向不明确"
                   % (g.close_pos * 100, close_pos_max * 100,
                      (1 - close_pos_max) * 100))
    if vol_rel is None or tbr is None:
        why.append("量能或主动性缺失")
        return "；".join(why)
    if vol_rel < vol_rel_min:
        why.append("量只 %.2f 倍 < %.1f" % (vol_rel, vol_rel_min))
    if side == SIDE_DOWN and tbr > taker_buy_max:
        why.append("主动买 %.3f > %.2f（卖方不占优）" % (tbr, taker_buy_max))
    if side == SIDE_UP and tbr < 1 - taker_buy_max:
        why.append("主动买 %.3f < %.2f（买方不占优）" % (tbr, 1 - taker_buy_max))
    return "；".join(why) if why else "?"


def judge(bars: Sequence[dict], i: int,
          prev_lines: Optional[Dict[str, float]] = None,
          look: int = STABLE_LOOKBACK_BARS,
          tol_pts: float = SCENARIO_TOL_PTS,
          max_pin_range_mult: float = STABLE_MAX_PIN_RANGE_MULT,
          lines_at_bar: Optional[Dict[int, Dict[str, float]]] = None,
          allow_swing: bool = True,
          allow_cross_bar: bool = False,
          path_aware: bool = True,
          n_touch: Optional[int] = None) -> Judgement:
    """读第 i 根：返回状态 + 证据。`bars` 元素是 dict，
    至少含 o/h/l/c，可选 vol_rel / taker_buy_ratio / ts。

    `prev_lines` 是**上一根**的线快照，用于同根内的「穿透后收回」。
    `lines_at_bar` 是「判第 j 根时能看到的那份线快照」按**下标**索引的全表
    （即 `lines_at_bar[j] == 表 0 里 ts = bars[j].ts - interval 那一行`），
    用于跨根收回。用下标而不是 ts 索引，是为了不在每根里遍历找前一根。

    不给 `lines_at_bar` 就跳过跨根收回，**证据里会写明跳过了** ——
    静默当成「没收回」等于把「没算」说成「没有」，那是假结论。

    只许用 `bars[0..i]` 的数据 —— 这是硬约束，有测试扫源码钉着。

    `path_aware=False` 退回 v2 口径（「稳住」只量本根有没有破针极值），
    只给对照测试和探针用；生产路径永远是 True，见 `pin_extreme_kept`。

    `n_touch`：本根碰到了几条观察线，**必须用表 1 落库值**（见 `LABEL_NO_ALL`
    上方那段说明）。给了就把「无」拆成「无·真空」/「无·贴线」；
    没给就退回「无」，并在证据里写明**没拆**，不许猜。
    """
    if not bars:
        return Judgement(states=(), label=STATE_NONE, side=None,
                         evidence=("没有 K 线数据",), pin_ts=None)
    if i < 0:
        i += len(bars)
    if i < 0 or i >= len(bars):
        raise IndexError("K 线下标越界: %s（共 %d 根）" % (i, len(bars)))

    b = bars[i]
    g = geometry(b["o"], b["h"], b["l"], b["c"])
    if prev_lines is None and lines_at_bar is not None:
        prev_lines = lines_at_bar.get(i)
    vol_rel = b.get("vol_rel")
    tbr = b.get("taker_buy_ratio")
    ev: List[str] = []
    states: List[str] = []
    side = pin_side(g)
    pin_ts: Optional[int] = None
    pin_kind: Optional[str] = None
    ref_pin_kind: Optional[str] = None
    ref_pin_side: Optional[str] = None

    ev.append("下影 %.1f 点 = 全幅 %.0f%%，上影 %.1f 点 = %.0f%%，收位 %.0f%%"
              % (g.dn, g.dn_pct * 100, g.up, g.up_pct * 100, g.close_pos * 100))

    # ---- 1. 本根自己是不是长影线针 ----
    if side is not None:
        states.append(STATE_PIN)
        pin_kind = PIN_KIND_WICK
        wick, pct = (g.dn, g.dn_pct) if side == SIDE_DOWN else (g.up, g.up_pct)
        ev.append("插针(%s)：影 %.1f 点 >= %.0f 点，占全幅 %.0f%% >= %.0f%%"
                  % (side, wick, WICK_PTS_MIN, pct * 100, WICK_PCT_MIN * 100))
        rec = recovered_lines(g, prev_lines)
        if rec:
            states.append(STATE_PIN_RECOVERED)
            ev.append("插针收回：同根内穿透后收回 %d 条线 —— %s"
                      % (len(rec), "、".join("%s(%.2f)" % (k, p) for k, p, _ in rec)))
        elif prev_lines is None:
            ev.append("插针收回：未提供上一根线快照，同根收回跳过")
        else:
            ev.append("插针收回：上一根 %d 条线里没有一条是「同根穿透后收回」"
                      % len(prev_lines))

    # ---- 2. 恐慌盘：**不再依赖 pin**（v2）----
    psd = panic_side(g)
    if is_panic(g, vol_rel, tbr, side=psd):
        states.append(STATE_PANIC)
        lo = PANIC_TAKER_BUY_MAX if psd == SIDE_DOWN else 1 - PANIC_TAKER_BUY_MAX
        # 方向是怎么来的必须说清：有影线听影线的，没影线才看收位。
        # 2026-11-07 23:45 收位 77% 却判成下压 —— 因为下影 166 点，
        # 价格是被打下去又被买回来的，收位高是「买回来」的结果，不是方向。
        if side is not None:
            dir_why = "影线方向 %s（收位 %.0f%% 是打完之后的结果，不参与定方向）" % (
                "下压" if psd == SIDE_DOWN else "上冲", g.close_pos * 100)
        else:
            dir_why = "收位 %.0f%% 落在%s侧（本根没有影线，只能看收位）" % (
                g.close_pos * 100, "下" if psd == SIDE_DOWN else "上")
        ev.append("恐慌盘(%s)：%s，量 %.2f 倍 >= %.1f，主动买 %.3f %s %.2f"
                  % (psd, dir_why, vol_rel, PANIC_VOL_REL_MIN, tbr,
                     "<=" if psd == SIDE_DOWN else ">=", lo))
        if side is None:
            ev.append("恐慌盘不要求本根是针 —— 收在极值的大%s线没有影线，"
                      "但它照样是把价格按住了"
                      % ("阴" if psd == SIDE_DOWN else "阳"))
    else:
        ev.append("够不上恐慌盘：%s" % _why_not_panic(g, vol_rel, tbr, psd))

    # ---- 3. 往回找针：稳住 / 远离 ----
    if side is not None:
        ev.append("稳住：本根自己是针，不参与「插针后稳住」")
    else:
        got = after_pin_full(bars, i, look=look, tol_pts=tol_pts,
                             max_pin_range_mult=max_pin_range_mult,
                             allow_swing=allow_swing,
                             path_aware=path_aware)
        if got is not None:
            j, which, kind = got
            states.append(which)
            pin_ts = bars[j].get("ts")
            ref_pin_kind = kind
            pj = bars[j]
            gp = geometry(pj["o"], pj["h"], pj["l"], pj["c"])
            amp = pin_amp(bars, j, kind)
            kind_cn = "长影线" if kind == PIN_KIND_WICK else "局部极值"
            # 沿「针的方向」量距离：下针看本根最低比针低高多少，
            # 上针看本根最高比针高低多少。负数就是已经穿过针的极值了。
            _down = (pin_side(gp) == SIDE_DOWN
                     or (kind == PIN_KIND_SWING
                         and swing_pin_kind(bars, j, i - 1) == SIDE_DOWN))
            # 参照针的方向要落库：表 5 的 side 只在「本根自己是针」时有值，
            # 佐证层要判「价格有没有守住参照针的极值」，缺这一列就无从判 ——
            # 与表 3 多写 held_side 是同一个理由：不写下来就没人能复核。
            ref_pin_side = SIDE_DOWN if _down else SIDE_UP
            if _down:
                dist = g.l - gp.l
                ev.append("前 %d 根内下针在 %s（%s针，针低 %.1f，针幅 %.1f），"
                          "本根最低 %.1f 高出针低 %.1f 点 = %.2f 倍针幅"
                          "（容差 %.0f 点，上限 %.1f 倍）"
                          % (look, _fmt_ts(pin_ts), kind_cn, gp.l, amp, g.l,
                             dist, dist / amp, tol_pts, max_pin_range_mult))
            else:
                dist = gp.h - g.h
                ev.append("前 %d 根内上针在 %s（%s针，针高 %.1f，针幅 %.1f），"
                          "本根最高 %.1f 低于针高 %.1f 点 = %.2f 倍针幅"
                          "（容差 %.0f 点，上限 %.1f 倍）"
                          % (look, _fmt_ts(pin_ts), kind_cn, gp.h, amp, g.h,
                             dist, dist / amp, tol_pts, max_pin_range_mult))
            if kind == PIN_KIND_SWING:
                ev.append("这根针是「局部极值」而不是长影线：往回看 %d 根，"
                          "它到今天为止还没被破" % SWING_LOOKBACK_BARS)
            if which == STATE_NEAR_PIN:
                ev.append("判「针后远离」而不是「稳住」：已经走出 %.2f 倍针幅 > %.1f 倍，"
                          "再说稳住就是编" % (dist / amp, max_pin_range_mult))
            ev.append("收盘 %.0f%% 位置，%s"
                      % (g.close_pos * 100,
                         "收阳" if g.c > g.o else "收阴但收位过半"
                         if g.close_pos >= .5 else "收阴且收位弱"))
        else:
            ev.append("非针：下影 %.1f 点(%.0f%%) / 上影 %.1f 点(%.0f%%)，"
                      "门槛 %.0f 点且 %.0f%%"
                      % (g.dn, g.dn_pct * 100, g.up, g.up_pct * 100,
                         WICK_PTS_MIN, WICK_PCT_MIN * 100))
            ev.append("稳住：前 %d 根内没有同方向的针（长影线或局部极值都算），"
                      "或破了针的极值，或收盘偏弱" % look)

    # ---- 4. 跨根收回：针在别的根上，本根才收回来 ----
    #
    # ⚠ 默认**关闭**（allow_cross_bar=False）。实测开着的代价（v2 口径，全库 17,654 根）：
    #   插针收回 1,197 -> 5,150（新增 3,953 = 22.4%）
    #   插针后稳住 3,992 -> 2,602（被抢走 1,348）
    #   针后远离     736 ->   196（被抢走 523）
    #   11-04 11:15 从「插针后稳住」变成「插针收回」—— 更具体的标签被它盖掉。
    #   22% 不是「少数事件」，说明这个定义测到的不是「针被收回」，而是
    #   「相邻两根里总有一根收在线的另一侧」—— 观察线很密（17 条线、平均每根
    #   12.96 条），这种交叉本来就在持续发生。所以默认关，功能留着给以后重新定义。
    #   （v1 时这套代码量到 6,555 / 37%，因为 v1 没有局部极值针；
    #    同一口径下 v2 是 3,953 / 22.4%。结论不变，数字变了。）
    #   见 D-21。
    if allow_cross_bar and STATE_PIN_RECOVERED not in states:
        if lines_at_bar is None:
            if side is None:
                ev.append("插针收回（跨根）：未提供全量线快照，跨根收回跳过")
        else:
            hit = None
            for k in range(1, look + 1):
                j = i - k
                if j < 0:
                    break
                kb = bars[j]
                kg = geometry(kb["o"], kb["h"], kb["l"], kb["c"])
                kside = pin_side(kg)
                if kside is None and allow_swing:
                    kside = swing_pin_kind(bars, j, i - 1)
                if kside is None:
                    continue
                rec = recovered_after_pin(g, kb, lines_at_bar.get(j), kside)
                if rec:
                    hit = (j, kside, rec)
                    break
            if hit:
                j, kside, rec = hit
                states.append(STATE_PIN_RECOVERED)
                if pin_ts is None:
                    pin_ts = bars[j].get("ts")
                ev.append("插针收回（跨根）：%s 那根%s针穿了 %d 条线没收回，本根收了回来 —— %s"
                          % (_fmt_ts(bars[j].get("ts")),
                             "下" if kside == SIDE_DOWN else "上", len(rec),
                             "、".join("%s(%.2f)" % (x, p) for x, p, _ in rec)))
            elif side is None:
                ev.append("插针收回（跨根）：前 %d 根内的针里没有一条线是「穿了没收回、本根收回」"
                          % look)

    # `states` 里**不放**「无」：「无」的意思是「一个形态都没成立」，
    # 它不是一种形态。所以没有状态时 states 是空 tuple，只有 label 才是「无」。
    # 这样落库后 `states=''` 与 `label ∈ 无/无·真空/无·贴线` 一一对应，
    # 读者不会以为「无」是六种形态里的一种（早先放进去了，表 5 与现算因此对不上）。
    label = STATE_NONE
    for s in STATE_ORDER:
        if s in states:
            label = s
            break
    # 「无」拆词（D-23.4）：没有形态时，按碰到了几条观察线再分一层。
    # 这一步只改 label，不改 states —— 「真空/贴线」不是形态，是**环境**。
    # 所以 states 仍然是空 tuple，`label ∈ LABEL_NO_ALL` 且 `states == ()`。
    if label == STATE_NONE:
        if n_touch is None:
            ev.append("「无」未拆词：调用方没给 n_touch（D-23.4）。"
                      "不许自己数 —— 自己数与表 1 落库值实测 14.3% 不一致")
        elif int(n_touch) < NO_SPLIT_BY_TOUCH:
            label = LABEL_NO_EMPTY
            ev.append("「无·真空」：本根 n_touch=0，一根观察线都没碰到 —— "
                      "价格在真空区，没有东西拦它")
        else:
            label = LABEL_NO_NEAR
            ev.append("「无·贴线」：本根 n_touch=%d，碰到了观察线 —— "
                      "价格被按在线附近（摩擦）" % int(n_touch))
    # states 按 STATE_ORDER 排序输出，不按追加顺序。
    # `show_kbar` 会原样打印，顺序乱掉后人读到的重点就变了。
    # STATE_NONE 不在 STATE_ORDER 里（它不是一种形态，是「没有形态」），排最后。
    _rank = dict((s, i2) for i2, s in enumerate(STATE_ORDER))
    states.sort(key=lambda s: _rank.get(s, len(STATE_ORDER)))
    return Judgement(states=tuple(states), label=label, side=side,
                     evidence=tuple(ev), pin_ts=pin_ts, pin_kind=pin_kind,
                     ref_pin_kind=ref_pin_kind, ref_pin_side=ref_pin_side)



def counts_by_label(judgements: Sequence[Judgement]) -> Dict[str, int]:
    """按 label 计数，用于探针与文档对账。"""
    out: Dict[str, int] = {}
    for j in judgements:
        out[j.label] = out.get(j.label, 0) + 1
    return out
