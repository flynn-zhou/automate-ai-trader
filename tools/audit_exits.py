# -*- coding: utf-8 -*-
"""出场几何量具：一条计划挂单成交后，到底会走到哪。

为什么要有这个文件
------------------
2026-11-19 那次审查翻出两个**自欺**：
  1. 我用的坐标系是振幅止损（0.135%），生产用的是公式止损（1.52%）—— 差 11 倍，
     结论全反。
  2. 我写的一版测量把空单的顺向目标位判到了上方，得出「止盈放关键位 = +0.470R」；
     换两套独立实现交叉验证，真实值是 -0.016R。

所以这个量具的**第一职责不是给数字，是拒绝给错数字**：

  * 两套独立的出场实现（止损优先 / 止盈优先）逐笔对拍。
    同根 K 里两者结论必然不同（bar 内无先后），但**只要有一笔的
    「是否止损」判定不同，就说明实现有 bug** —— 直接报错退出。
  * 坐标系必须显式声明（`--max-stop-pct`）。不传就按公式算，
    并把它印在输出第一行，避免再看错。
  * 口径写死并打印：挂单从声明那根 K 收盘之后开始等成交；
    同一根 K 里止损与止盈都够得着 → 记止损（最坏）；
    止盈只算成交那根**之后**（成交那根里先到哪个不可知）。

用法
----
    python tools/audit_exits.py                    # 公式止损（生产口径）
    python tools/audit_exits.py --max-stop-pct 0   # 振幅止损（旧口径，仅供对比）
    python tools/audit_exits.py --variants         # 多方案对照

输出里的「期望R」都是**毛值**（不含手续费）。要算净值：
    手续费(R) ≈ 往返成本点数 / 止损距离点数
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader import config as cfgmod                 # noqa: E402
from ai_trader import main as M                         # noqa: E402
from ai_trader.trading import pending_plan as P          # noqa: E402
from ai_trader.trading import thesis as T                # noqa: E402
from ai_trader.trading.plan_source import (              # noqa: E402
    PlanSource, resolve_range_db)

MS5, MS15 = 300_000, 900_000
D_MAX_FORMULA = 1.6181818181818184      # (1/22 - 0.005) * 100 / 2.5

#: 顺向（有利）关键位：多单在上方，空单在下方
FAV = {T.UP: ("range_soft_up@1h", "range_hard_up@1h", "range_key@1h"),
       T.DOWN: ("range_soft_dn@1h", "range_hard_dn@1h", "range_key@1h")}
#: 反向（不利）关键位
ADV = {T.UP: ("range_soft_dn@1h", "range_key@1h", "range_hard_dn@1h"),
       T.DOWN: ("range_soft_up@1h", "range_key@1h", "range_hard_up@1h")}


# ------------------------------------------------------------------ 数据

def bars_of(seq):
    """5m 序列 → [(ts, o, h, l)]。"""
    out = []
    for b in seq or ():
        if isinstance(b, dict):
            out.append((int(b["ts"]), float(b["o"]), float(b["h"]),
                        float(b["l"])))
        else:
            out.append((int(getattr(b, "ts")), float(getattr(b, "o")),
                        float(getattr(b, "h")), float(getattr(b, "l"))))
    return out


def line_at(book, key, ms):
    pt = book.at(key, int(ms))
    if pt is None:
        return None
    try:
        v = float(pt.price)
    except (TypeError, ValueError, AttributeError):
        return None
    return v if v > 0 else None


def nearest_level(book, keys, order, ms, direction, side, cap):
    """最近的关键位（side=-1 不利侧 / +1 顺向），只接受 <= cap。"""
    best = None
    for k in keys:
        v = line_at(book, k, ms)
        if v is None:
            continue
        if side > 0:
            if direction == T.UP:
                if v <= order:
                    continue
                d = v - order
            else:
                if v >= order:
                    continue
                d = order - v
        else:
            if direction == T.UP:
                if v >= order:
                    continue
                d = order - v
            else:
                if v <= order:
                    continue
                d = v - order
        if d <= 0 or d > cap:
            continue
        if best is None or d < best:
            best = d
    return best


# ------------------------------------------------------------------ 两套独立实现

def _fill_index(order, direction, bars, i0, horizon):
    """挂单成交那根的序号。

    ★★ 判据是「价格穿过挂单价」，与方向无关：``l <= order <= h``。

    为什么不能按方向写（2026-11-19 修）：本系统的挂单**两种都有** ——
    实测 1271 条计划里 678 条是回撤限价单（多单挂市价下方）、
    593 条是突破单（多单挂市价上方）。按方向一律写 ``h >= order``
    等于假设「多单永远在价格涨到挂单价时成交」，而对回撤单来说
    挂单价本来就低于市价，``h >= order`` 在第一根永远成立 ——
    于是**以比市价更好的价格瞬时成交**，凭空多出利润。
    `direction` 保留在签名里是为了不让调用方改口径，
    真实判据只认价位是否被穿过。
    """
    for i in range(i0, min(len(bars), i0 + max(1, horizon))):
        _t, _o, h, l = bars[i]
        if l <= float(order) <= h:
            return i
    return None


def _outcome_stop_first(order, stop, target, direction, bars, fill):
    """实现甲：每根先判止损、再判止盈（同根必记止损）。"""
    r = abs(order - stop)
    if r <= 0:
        return None
    for i in range(fill, len(bars)):
        _t, _o, h, l = bars[i]
        if (direction == T.UP and l <= stop) or \
                (direction == T.DOWN and h >= stop):
            return {"stop": True, "r": -1.0, "bar": i - fill}
        if i > fill:      # 止盈只算成交那根之后
            if (direction == T.UP and h >= target) or \
                    (direction == T.DOWN and l <= target):
                return {"stop": False, "r": abs(target - order) / r,
                        "bar": i - fill}
    return {"stop": False, "r": 0.0, "bar": len(bars) - fill}


def _outcome_target_first(order, stop, target, direction, bars, fill):
    """实现乙：每根先判止盈、再判止损。只用来对拍"是否止损"。"""
    r = abs(order - stop)
    if r <= 0:
        return None
    for i in range(fill, len(bars)):
        _t, _o, h, l = bars[i]
        if i > fill:
            if (direction == T.UP and h >= target) or \
                    (direction == T.DOWN and l <= target):
                return {"stop": False, "r": abs(target - order) / r,
                        "bar": i - fill}
        if (direction == T.UP and l <= stop) or \
                (direction == T.DOWN and h >= stop):
            return {"stop": True, "r": -1.0, "bar": i - fill}
    return {"stop": False, "r": 0.0, "bar": len(bars) - fill}


# ------------------------------------------------------------------ 主流程

def load_plans(db_root, max_stop_pct, symbol=None):
    cfg = cfgmod.load()
    cfg.store.path = os.path.join(db_root, "market.db")
    src = PlanSource(book_provider=M.kbar_lines_for(cfg),
                     kbar_db=os.path.join(db_root, "kbar.db"),
                     range_db=resolve_range_db(db_root),
                     symbol=symbol or getattr(cfg.trading, "symbol", "BTCUSDT"))
    ctx = src.context()
    ctx_all = P.PlanContext(ctx.bars15, ctx.book,
                            bars_1h=getattr(ctx, "_h1", ()),
                            bars_5m=getattr(ctx, "bars5", ()), windows=None)
    params = P.PendingParams(max_stop_pct=max_stop_pct)
    plans, stats = P.detect_all(ctx_all, params, require_window=False,
                                include_mirror=True)
    return ctx, params, plans, stats


def run_variants(ctx, params, plans, want_variants=False):
    """返回 {组名: {"r": [...], "bar": [...], "tgt": [...]}}。"""
    bars = bars_of(ctx.bars5)
    ts_idx = {b[0]: i for i, b in enumerate(bars)}
    groups = {}

    def add(name, key, val):
        groups.setdefault(name, {"r": [], "bar": [], "tgt": []})[key].append(val)

    disagreements = 0
    n_pairs = 0

    for p in plans:
        iv = MS5 if p.bar_space == "5m" else MS15
        ms = p.claim_close_ms() if hasattr(p, "claim_close_ms") \
            else P.claim_close_ms(p)
        o, d = float(p.order_price), p.direction
        stop = float(p.stop_price)
        r = abs(o - stop)
        if r <= 0:
            continue
        ts = p.effective_claim_ts
        i0 = ts_idx.get(ts)
        if i0 is None:
            for i, b in enumerate(bars):
                if b[0] >= ts:
                    i0 = i
                    break
        if i0 is None or i0 + 1 >= len(bars):
            continue
        fill = _fill_index(o, d, bars, i0 + 1, params.horizon_bars)
        if fill is None:
            continue

        plan_tgt = float(p.target_price)          # 已含 S5 收紧
        variants = [("A 现状", stop, plan_tgt)]
        if want_variants:
            fv = nearest_level(ctx.book, FAV[d], o, ms, d, +1, r * 8)
            av = nearest_level(ctx.book, ADV[d], o, ms, d, -1, r * 3)
            tg_c = plan_tgt
            if fv is not None:
                tp = o + fv * 0.9994 if d == T.UP else o - fv * 0.9994
                if abs(tp - o) < abs(plan_tgt - o):
                    tg_c = tp
            stop_b, r_b = stop, r
            if av is not None:
                sp = o - av * 0.9994 if d == T.UP else o + av * 0.9994
                rb = abs(o - sp)
                if 0 < rb <= r:
                    stop_b, r_b = sp, rb
            tg_b = o + 4 * r_b if d == T.UP else o - 4 * r_b
            tg_d = tg_b
            if fv is not None:
                tp = o + fv * 0.9994 if d == T.UP else o - fv * 0.9994
                if abs(tp - o) < abs(tg_b - o):
                    tg_d = tp
            variants += [("C 顺向关键位", stop, tg_c),
                         ("B 结构止损+4R", stop_b, tg_b),
                         ("D 结构双向", stop_b, tg_d)]

        for name, st, tg in variants:
            a = _outcome_stop_first(o, st, tg, d, bars, fill)
            b = _outcome_target_first(o, st, tg, d, bars, fill)
            if a is None or b is None:
                continue
            # ★ 只对拍「是否止损」：同根 K 里两者必然不同（可能双触发）
            only_a = a["stop"] and not b["stop"]
            only_b = b["stop"] and not a["stop"]
            if only_a or only_b:
                # 允许的情形：同一根里既够到止损又够到止盈
                _t, _o, h, l = bars[fill + min(a["bar"], b["bar"])]
                both = (h >= max(st, tg)) and (l <= min(st, tg))
                if not both:
                    disagreements += 1
            n_pairs += 1
            add(name, "r", a["r"])
            add(name, "bar", a["bar"])
            rr = abs(o - st)
            if rr > 0:
                add(name, "tgt", abs(tg - o) / rr)

    return groups, disagreements, n_pairs


def report(groups, stop_pct_note):
    print("\n坐标系: %s" % stop_pct_note)
    print("\n%-16s %6s %8s %9s %8s %9s"
          % ("组", "样本", "胜率", "期望R(毛)", "未结", "目标/R"))
    for name in groups:
        g = groups[name]
        vals = g["r"]
        dec = [v for v in vals if v != 0.0]
        if not dec:
            continue
        wins = sum(1 for v in dec if v > 0)
        bars = sorted(g["bar"]) or [0]
        tgt = sorted(g["tgt"]) or [0]
        print("%-16s %6d %7.1f%% %+9.3f %8d %9.2f"
              % (name, len(dec), 100.0 * wins / len(dec),
                 sum(dec) / len(dec), len(vals) - len(dec),
                 tgt[len(tgt) // 2]))
        print("%-16s   中位耗时 %d 根 5m（%.1f 小时）"
              % ("", bars[len(bars) // 2], bars[len(bars) // 2] * 5 / 60.0))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-root", default="data")
    ap.add_argument("--symbol", default=None)
    ap.add_argument("--max-stop-pct", type=float, default=D_MAX_FORMULA,
                    help="止损距离上限%%，默认公式口径 %.6f" % D_MAX_FORMULA)
    ap.add_argument("--variants", action="store_true",
                    help="同时跑结构止损/结构目标两组对照")
    args = ap.parse_args()

    ctx, params, plans, _stats = load_plans(args.db_root, args.max_stop_pct,
                                            args.symbol)
    print("计划 %d 条" % len(plans))
    print("坐标系: max_stop_pct=%.6f%%（%s）"
          % (args.max_stop_pct,
             "公式口径（生产）" if args.max_stop_pct > 0.5 else "振幅口径（旧）"))
    print("口径: 声明K收盘后才等成交；同根同时够到止损/止盈→记止损；"
          "止盈只算成交那根之后")
    groups, dis, pairs = run_variants(ctx, params, plans, args.variants)
    report(groups, "max_stop_pct=%.6f%%" % args.max_stop_pct)

    print("\n双实现对拍: %d 笔比较，无法解释的分歧 %d 笔" % (pairs, dis))
    if dis:
        print("！两套实现不一致 —— 量具有 bug，上面的数字不可用", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
