# -*- coding: utf-8 -*-
"""留出集：给「某个维度在某个结论词里有没有区分力」这个问题一个**没用过的样本**。

## 为什么必须冻结

留出集只有一个用途：**在没参与任何决定的样本上验一次**。所以它必须满足三条，
少一条就退化成「又一份训练集」：

1. **切点冻结**：写进 `kbar_meta`，带哈希，改切点 = 新版本 + 之前所有留出结论作废。
   可配置的切点不是冻结的切点 —— 那样可以试几个切点挑最好看的。
2. **embargo**：表 6/6b 的判定要往后看最多 12 根，所以训练段最后 12 根的「事后结论」
   是用留出段的价格算出来的。不清掉这 12 根就是真泄漏。
3. **评测登记**：每次在留出段上评测都记一笔（表 8）。第一次跑完，留出集就算用掉了；
   之后再跑，日志里会显示第几次 —— 谁都能看见，藏不住。

## 为什么报两个 z

`verify.lift_z()` 是**按独立样本**算的：

    se = sqrt(p0 * (1 - p0) / total)

但表 6b 每根的前视窗是 12 根，相邻两行的窗口共享 11/12 根 K 线。
17,654 行不是 17,654 个独立样本，有效样本约 1/12。

所以本模块同时报：

- `z_naive`：跟现有工具同口径（**高估约 √12 ≈ 3.5 倍**），用来跟历史结论对比
- `z_block`：在**不重叠**子样本上算（每 12 根取一根），牺牲样本换独立性

只报 `z_naive` 会给自己假信心；只报 `z_block` 又跟以前报过的数接不上。两个都摆出来。

## 为什么把候选空间枚举完

不限定的候选空间 = 无限次尝试 = 必然过拟合。

好在表 7 的分档只有那几个天然切点，所以候选格是**有限可枚举**的：
7 个结论词 × 39 个维度 × 每个维度 3~4 档 ≈ 900 格。

所以本模块**一次把全部格子跑完**，然后报「多少格过线 / 一共验了多少格 /
纯靠运气该有多少格过线」。这样「试了多少次」是桌面上的数，不是心里的数。
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from . import dims as D

# ---------------------------------------------------------------- 冻结切分
SPLIT_VERSION = "v1"

# 用户 2026-11-10 拍的：70/30。**改这里就是新版本，之前所有留出结论作废。**
TRAIN_FRAC = 0.70

# 前视窗最大值 = 表 6 的 max(VERIFY_HORIZONS) 与表 6b 的 SURVIVAL_MAX_N。
# 训练段最后一根的事后结论要往后看到这里，所以这一段必须整段剔掉（不进 train
# 也不进 holdout），否则训练段尾巴在偷看留出段。
EMBARGO_BARS = 12

# 一格至少要这么多行才报数。低于这个数，一个百分点就是噪声。
MIN_N = 30

# 「不重叠子样本」的步长。等于前视窗，保证子样本里任意两行的窗口不相交。
BLOCK_STEP = EMBARGO_BARS

Z_CRIT = 1.96


def split_hash() -> str:
    raw = json.dumps({"v": SPLIT_VERSION, "train_frac": TRAIN_FRAC,
                      "embargo": EMBARGO_BARS, "block_step": BLOCK_STEP},
                     sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def make_split(ts_sorted: Sequence[int]) -> Dict:
    """按 ts 把全段切成 train / embargo / holdout。

    返回的 dict 是要落进 `kbar_meta` 的**冻结凭证**：它带着每一段的边界 ts，
    所以任何人任何时候都能复核「留出段到底是哪一段」，不靠记忆。
    """
    ts = sorted(int(t) for t in ts_sorted)
    n = len(ts)
    if n == 0:
        return {"n": 0, "split_hash": split_hash(), "version": SPLIT_VERSION}

    n_hold = int(round(n * (1.0 - TRAIN_FRAC)))
    # holdout 起点下标
    i_hold = n - n_hold
    # 训练段最后一根：它的事后窗必须整段落在 holdout 之前
    #   i + EMBARGO_BARS <= i_hold - 1  →  i <= i_hold - 1 - EMBARGO_BARS
    i_train_end = i_hold - 1 - EMBARGO_BARS
    if i_train_end < 0:
        # 样本太少，连 embargo 都放不下 —— 明确报错，不许悄悄缩小 embargo
        return {"n": n, "error": "样本 %d 根不够切（embargo 要 %d 根）"
                % (n, EMBARGO_BARS + 1), "split_hash": split_hash()}

    return {
        "version": SPLIT_VERSION,
        "split_hash": split_hash(),
        "n": n,
        "train_frac": TRAIN_FRAC,
        "embargo_bars": EMBARGO_BARS,
        "block_step": BLOCK_STEP,
        "train_first_ts": ts[0],
        "train_last_ts": ts[i_train_end],
        "train_n": i_train_end + 1,
        "embargo_first_ts": ts[i_train_end + 1],
        "embargo_last_ts": ts[i_hold - 1],
        "embargo_n": EMBARGO_BARS,
        "holdout_first_ts": ts[i_hold],
        "holdout_last_ts": ts[-1],
        "holdout_n": n_hold,
    }


def phase_of(ts: int, split: Dict) -> str:
    """一根属于哪一段。**边界闭区间**，别用浮点比较。"""
    t = int(ts)
    if t <= split["train_last_ts"]:
        return "train"
    if t < split["holdout_first_ts"]:
        return "embargo"
    return "holdout"


# ---------------------------------------------------------------- 结果口径
# 表 6b 的三态（status）。**「未成熟」不算定型** —— 把没验过的当验过了是这里
# 最容易犯的错，所以它被单独列出来，不进分母。
STATUS_BROKE = "已破"
STATUS_LASTED = "活到最后"
STATUS_PENDING = "未成熟"


def outcome_ok(row: Dict) -> Optional[bool]:
    """这一行的预言「应验了吗」。

    - `must_hold == 1`（守住型）：活到最后才算应验
    - `must_hold == 0`（会破型）：破了才算应验
    - 未成熟 → None（不参与统计，**不是** False）
    """
    st = row.get("status")
    mh = row.get("must_hold")
    if st == STATUS_PENDING or mh is None:
        return None
    if int(mh) == 1:
        return st == STATUS_LASTED
    return st == STATUS_BROKE


# ---------------------------------------------------------------- 格子枚举
def _cell_key(dim: str, val) -> str:
    return "%s=%s" % (dim, val)


def candidate_cells(rows: Sequence[Dict],
                    max_rare_values: int = 8) -> List[Tuple[str, str, str]]:
    """把候选格枚举出来 → [(维度, 取值, 档/值)]。

    规则（全部无参数，只取决于数据形状）：
      - 有分档的维度（dims 里定义了 bucket）→ 用它的分档词
      - 没分档的维度 → 如果留出段上不同取值 ≤ `max_rare_values`，就用原值当格
        （`zone` `direction` `side` `close_side` `event` `line_kind` 都属于这类）
      - 取值太散（如 `line_price` `src_ts`）→ 不枚举，它们本来就不是分类变量
    """
    bucketed = {d.name for d in D.DIMS if d.bucket is not None}
    vals: Dict[str, set] = defaultdict(set)
    for r in rows:
        for dim, v in (r.get("buckets") or {}).items():
            if dim in bucketed and v:
                vals[dim].add(v)
        for dim, v in (r.get("values") or {}).items():
            if dim not in bucketed and v is not None and \
                    isinstance(v, (str, bool)):
                vals[dim].add(v)
    out = []
    for dim in sorted(vals):
        vs = vals[dim]
        if not vs or len(vs) > max_rare_values:
            continue
        for v in sorted(vs, key=str):
            out.append((dim, str(v), "bucket" if dim in bucketed else "raw"))
    return out


def cell_value_of(row: Dict, dim: str, kind: str):
    """取这一根在某个维度上的格值 —— 与 candidate_cells 的口径必须一致。"""
    if kind == "bucket":
        return (row.get("buckets") or {}).get(dim) or None
    v = (row.get("values") or {}).get(dim)
    if v is None or not isinstance(v, (str, bool)):
        return None
    return str(v)


# ---------------------------------------------------------------- 统计
def lift_z_naive(hits: int, total: int, base_pct: float) -> Tuple[float, float]:
    """与 `verify.lift_z` **同公式**（独立样本假设）。

    刻意在这里复制一份而不是 import：`verify` 是佐证层，这一层是评测层，
    两边口径要能各自独立演进，但公式含义要一致 —— 所以有测试钉住「同公式」。
    """
    if total <= 0:
        return 0.0, 0.0
    p0 = base_pct / 100.0
    p = hits / float(total)
    se = (p0 * (1.0 - p0) / total) ** 0.5
    return 100.0 * p - base_pct, ((p - p0) / se if se else 0.0)


def lift_z_block(hits: int, total: int, base_pct: float) -> Tuple[float, float]:
    """在不重叠子样本上算的 z（每 `BLOCK_STEP` 根取一根）。

    ⚠ **不要拿它当主口径。** 它把 11/12 的数据扔掉，`n_block` 常常只有 4~8，
    那样的 z 一样是噪声（2026-11-10 实测：恐慌盘 body_pct 小 那格 n_block=4，
    z_block=-2.59，看着显著，实际上 4 个样本什么都说明不了）。
    主口径是 `lift_z_cluster`，这个只作保守对照，且 `n_block` 太小就不该报。
    """
    return lift_z_naive(hits, total, base_pct)


# 子样本 z 的最小样本数。低于这个数，z_block 不报（None），也不计入「过线」。
MIN_N_BLOCK = 10

# 聚类数下限。少于这么多个独立块，cluster z 的渐近性质没成立。
#
# 为什么定 50 而不是 20：聚类方差估计本身的相对误差约 √(2/G)。
#   G=20 → 约 32%，G=24 → 约 29%，G=50 → 约 20%，G=100 → 约 14%。
# 实测过 G=24 的格子会出现 z_cluster 反超 z_naive 的情况（方差估计的抽样噪声，
# 方向不定）。而在全样本上块比例方差/iid 比值稳定在 1.03~1.40（>1），
# 说明正常情况下 cluster z 只会比 naive 小 —— 那些反超的格子就是小样本噪声。
#
# 代价：6 个月 15m 数据上，只有 n≥600 的结论词才够 G≥50。
# 小词（恐慌盘 199、针后远离 212、插针 258、插针收回 421）会被整片丢掉 ——
# **这是数据量的硬限制，不是调参能解决的**。丢掉比硬报一个不可信的 z 好。
MIN_CLUSTERS = 50


def lift_z_cluster(outcomes: Sequence[int], p0: float,
                   blocks: Optional[Sequence[int]] = None,
                   step: int = BLOCK_STEP
                   ) -> Tuple[float, float, int]:
    """**聚类稳健** z（cluster-robust / sandwich）→ (lift, z, 聚类数G)。

    ## 为什么要按时间分块

    表 6b 每根的前视窗是 12 根，相邻 12 行的窗口共享 11/12 根 K 线。
    按独立样本算 se 会把 z 高估。

    ⚠ **块必须按「连续的 12 根真实 K 线」划分，不能按「筛选后剩下的成员」
       顺序去切。** 踩过这个坑：`无·贴线` 在留出段占 40%，若拿它的成员
       顺序切 12 个一块，一块实际横跨约 30 根真实 K 线，块内相关性被稀释，
       结果 cluster z 反而比 naive 大 1.4 倍（−5.53 → −8.75）—— 完全错了。

    所以调用方要传入 `blocks`：每根在**完整时间序列**里的块号（`ts_index //
    step`）。这样同一块里的成员才是时间上真正相邻、窗口真正重叠的那些。

    ## 公式

        p̂  = Σx_i / n
        Var(p̂) = G/(G−1) · Σ_c (Σ_{i∈c} (x_i − p̂))² / n²      ← G = 非空聚类数

    `G/(G−1)` 是标准小样本修正。没它时少簇会偏（实测 `n_block=4~5` 的格子
    会出现 z_cluster 比 z_naive 还大）。

    嵌套性：每根自成一簇时（G=n），修正因子 ≈ 1，方差退化成
    `p̂(1−p̂)/n`。

    ⚠ **注意与 `lift_z_naive` 的一个本质差别**：朴素公式用的是**零假设方差**
    `p0(1−p0)/n`（二项检验），这里用的是**估计方差** `p̂(1−p̂)/n`（Wald 检验）。
    所以两者只在 `p̂ = p0` 时数值相等 —— 我的测试一开始就是按「必须完全相等」
    写的，结果差 0.45%，查下来是这个原因，不是 bug。
    两个都是合法检验，但**不能假设它们数值一致**；比较时只看「谁过线」就够了。

    ⚠ 返回的 G 一定要检查：**G < MIN_CLUSTERS 时这个 z 不可信**。
    """
    n = len(outcomes)
    if n <= 0:
        return 0.0, 0.0, 0
    if blocks is None:
        blocks = [i // max(1, int(step)) for i in range(n)]
    p = sum(outcomes) / float(n)
    per_block: Dict[int, float] = {}
    for x, b in zip(outcomes, blocks):
        per_block[b] = per_block.get(b, 0.0) + (x - p)
    g = len(per_block)
    var = sum(d * d for d in per_block.values()) / float(n * n)
    if g > 1:
        var *= float(g) / float(g - 1)
    lift = 100.0 * p - 100.0 * p0
    if var <= 0.0:
        return lift, 0.0, g
    return lift, (p - p0) / math.sqrt(var), g


def block_subsample(rows: Sequence[Dict], step: int = BLOCK_STEP
                    ) -> List[Dict]:
    """按 ts 排序后每 `step` 根取一根 → 窗口互不相交的子样本。"""
    rs = sorted(rows, key=lambda r: int(r["ts"]))
    return rs[::max(1, int(step))]


def binom_sf(k: int, n: int, p: float) -> float:
    """P(X >= k)，X~Bin(n,p)。用于「纯靠运气该有多少格过线」。"""
    if n <= 0:
        return 0.0
    p = min(max(p, 0.0), 1.0)
    if k <= 0:
        return 1.0
    if k > n:
        return 0.0
    # 直接累加即可（n 是格子数，最大也就几千）
    tot = 0.0
    for i in range(k, n + 1):
        tot += math.comb(n, i) * (p ** i) * ((1 - p) ** (n - i))
    return min(tot, 1.0)


# ---------------------------------------------------------------- 评测
def evaluate(rows: Sequence[Dict], label: str) -> Dict:
    """对一个结论词，把它的所有候选格在给定样本上跑一遍。

    `rows` 必须是**同一段**（留出段或训练段），且每行带 `label` / `values` /
    `buckets` / `status` / `must_hold` / `ts`。
    """
    sub = [r for r in rows if r.get("label") == label]
    settled = [r for r in sub if outcome_ok(r) is not None]
    if not settled:
        return {"label": label, "n": len(sub), "n_settled": 0, "cells": [],
                "base_pct": 0.0, "n_ok": 0}

    ok = [r for r in settled if outcome_ok(r)]
    base_pct = 100.0 * len(ok) / len(settled)

    # 全局时间块号：按**完整时序列**里这根的位置算，不按筛选后的顺序算。
    # 见 lift_z_cluster 的文档（踩过坑）。
    all_sorted = sorted(rows, key=lambda r: int(r["ts"]))
    block_of = {}
    for i, r in enumerate(all_sorted):
        block_of[int(r["ts"])] = i // BLOCK_STEP

    cells = []
    for dim, val, kind in candidate_cells(settled):
        grp = sorted((r for r in settled
                      if cell_value_of(r, dim, kind) == val),
                     key=lambda r: int(r["ts"]))
        n = len(grp)
        if n < MIN_N:
            continue
        outs = [1 if outcome_ok(r) else 0 for r in grp]
        blocks = [block_of[int(r["ts"])] for r in grp]
        h = sum(outs)
        lift, z_nv = lift_z_naive(h, n, base_pct)
        # 主口径：聚类稳健（按真实连续的 12 根一块去除相关）
        _lift_cl, z_cl, g_cl = lift_z_cluster(outs, base_pct / 100.0,
                                              blocks=blocks)
        # 聚类太少 → 这个 z 不可信，一个数都不许用来声称显著
        cluster_ok = g_cl >= MIN_CLUSTERS
        if not cluster_ok:
            z_cl = None

        # 对照口径：每 12 根只留一根（保守，但样本少时不可信 → 给 None）
        blk = grp[::BLOCK_STEP]
        nb = len(blk)
        hb = sum(1 for r in blk if outcome_ok(r))
        if nb >= MIN_N_BLOCK:
            _lb, z_blk = lift_z_block(hb, nb, base_pct)
            lift_blk = 100.0 * hb / nb - base_pct
        else:
            z_blk, lift_blk = None, None

        cells.append({
            "dim": dim, "val": val, "kind": kind,
            "n": n, "hits": h, "hit_pct": 100.0 * h / n,
            "base_pct": base_pct, "lift": lift, "z_naive": z_nv,
            "pass_naive": abs(z_nv) >= Z_CRIT,
            "z_cluster": z_cl, "n_clusters": g_cl, "cluster_ok": cluster_ok,
            "pass_cluster": cluster_ok and abs(z_cl) >= Z_CRIT,
            "n_block": nb, "hits_block": hb,
            "lift_block": lift_blk, "z_block": z_blk,
            "pass_block": (abs(z_blk) >= Z_CRIT) if z_blk is not None else False,
        })
    # 可信的（聚类够）排前面，再按 |z_cluster| 排
    cells.sort(key=lambda c: (not c["cluster_ok"],
                              -abs(c["z_cluster"] or 0.0)))
    return {"label": label, "n": len(sub), "n_settled": len(settled),
            "n_ok": len(ok), "base_pct": base_pct, "cells": cells}


def evaluate_all(rows: Sequence[Dict], labels: Sequence[str]) -> Dict:
    """全部结论词 × 全部维度档位。

    汇总时**必须**一起报三件事，少一件就是在稀释多重比较：
      - 一共验了多少格（n_tested）
      - 多少格 z_cluster 过线（n_pass_cluster）
      - 纯靠运气该有多少格过线（n_expected = 5% × n_tested）

    主口径用 `z_cluster`（聚类稳健）。`z_naive` / `z_block` 只作对照。
    """
    per = [evaluate(rows, lab) for lab in labels]
    flat = [c for p in per for c in p["cells"]]
    n_all = len(flat)
    # ⚠ 多重比较的分母只能是**可信的**格子数。把「聚类不足、z 没报」的格子
    #   算进分母，会稀释掉真正验过的次数，等于偷偷放宽门槛。
    usable = [c for c in flat if c["cluster_ok"]]
    n_tested = len(usable)
    n_pass_cl = sum(1 for c in usable if c["pass_cluster"])
    n_pass_nv = sum(1 for c in usable if c["pass_naive"])
    n_pass_blk = sum(1 for c in usable if c["pass_block"])
    expected = 0.05 * n_tested
    # Bonferroni：格数越多，单个格要过越高的门槛
    z_bonf = _z_for_two_sided(0.05 / n_tested) if n_tested else Z_CRIT
    n_pass_bonf = sum(1 for c in usable
                      if abs(c["z_cluster"] or 0.0) >= z_bonf)
    return {
        "per_label": per,
        "n_cells_all": n_all,
        "n_tested": n_tested,
        "n_dropped_few_clusters": n_all - n_tested,
        "n_pass_naive": n_pass_nv,
        "n_pass_cluster": n_pass_cl,
        "n_pass_block": n_pass_blk,
        "n_expected_by_chance": expected,
        "z_bonferroni": z_bonf,
        "n_pass_bonferroni": n_pass_bonf,
        "p_at_least_pass_cluster": binom_sf(n_pass_cl, n_tested,
                                            0.05) if n_tested else 1.0,
    }


def _z_for_two_sided(alpha: float) -> float:
    """双侧 alpha 对应的 z。用二分找 Φ⁻¹(1-α/2)，不依赖 scipy。"""
    if alpha >= 1.0:
        return 0.0
    target = 1.0 - alpha / 2.0

    def phi(x):
        return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

    lo, hi = 0.0, 10.0
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if phi(mid) < target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0
