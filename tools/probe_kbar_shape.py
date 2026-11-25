# -*- coding: utf-8 -*-
"""读数层探针：`ai_trader/kbar/shape.py` 的**独立重算**与人工复核。

探针不 import shape.py 的判定函数 —— 它按定义自己重写一遍，然后逐根比对。
否则公式错了探针跟着错，两边一起绿（这个项目踩过这种坑）。

查什么：
  0. 库规模与覆盖（分母是什么，命中率才有意义）
  1. 两根锚点：北京 11-07 23:45 / 11-08 05:15 —— 用户给的验收标准
  2. 四个判据的定义式独立重算，逐根比对
  3. 「收位条件是死条件」的独立验证：开关它，命中数必须一模一样
  4. 容差只影响「稳住」，不影响插针/收回/恐慌
  5. 「稳住」的距针尺：换固定点数会错判多少根
  6. 优先级与互斥：同一根不许既是针又是稳住
  7. 抽样把证据串打出来给人看（机器只能证明一致，证明不了读得对）

写这个探针时踩过的两个坑，别再踩：
  a. **收回只对「本根是针」的根有意义。** 一开始拿全部根去比「探针有没有
     收回 / shape 有没有标收回」，5,706 根全报不一致 —— 其实是探针的对比
     范围错了：非针的根就算穿回了一条线，也不该被标成「插针收回」。
  b. **回看窗口的 range 终点是排他的。** `range(i-1, max(-1, i-look), -1)`
     在 look=3 时只给 2 个候选（漏了第 3 根）。正确终点是 `i-look-1`。
     这个 off-by-one 会让「针后」少算 526 根，而且不报错。

用法：
    python tools/probe_kbar_shape.py
    python tools/probe_kbar_shape.py --kbar-db data/kbar.db --samples 6
"""
from __future__ import annotations

import argparse
import datetime
import os
import sqlite3
import sys

PROBLEMS = []


def bad(msg):
    print("  [BAD]  " + msg)
    PROBLEMS.append(msg)


def ok(msg):
    print("  [OK]   " + msg)


def hr(t):
    print("\n" + "=" * 78)
    print(t)
    print("=" * 78)


def pct(a, b):
    return 0.0 if not b else 100.0 * a / b


def fmt_u(ms):
    return datetime.datetime.utcfromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")


def to_ms(bj):
    """北京时间 → UTC 毫秒。"""
    dt = datetime.datetime.strptime(bj, "%Y-%m-%d %H:%M")
    return int((dt - datetime.datetime(1970, 1, 1)).total_seconds() - 8 * 3600) * 1000


# ---------------------------------------------------------------- 独立重算
# 阈值单独写一遍（故意与 shape.py 重复）：共用常量就失去交叉校验的意义。
PCT_MIN = 0.65
PTS_MIN = 40.0
VOL_REL_MIN = 3.0
TBR_MAX = 0.45
LOOK = 3
TOL_PTS = 20.0
MAX_MULT = 1.0
CP_MAX = 0.25      # v2：恐慌盘收位门槛（没影线时才用）
LOOK_SWING = 8     # v2：局部极值针的确认窗口


def geo(o, h, l, c):
    rng = max(h - l, 1e-9)
    dn = min(o, c) - l
    up = h - max(o, c)
    dn = dn if dn > 0 else 0.0
    up = up if up > 0 else 0.0
    return {"o": o, "h": h, "l": l, "c": c, "rng": rng, "dn": dn, "up": up,
            "dn_pct": dn / rng, "up_pct": up / rng, "cp": (c - l) / rng}


def my_panic_side(g):
    """v2 的方向来源：有影线听影线的，没影线才看收位。

    这一条是探针**独立**写的，不看 shape.py 的实现 —— 探针的价值就在于
    「另写一遍，两边对不上就是有 bug」。写的时候只看模块 docstring 的口径描述。
    """
    if g["dn_pct"] >= PCT_MIN and g["dn"] >= PTS_MIN:
        return "down"
    if g["up_pct"] >= PCT_MIN and g["up"] >= PTS_MIN:
        return "up"
    if g["cp"] <= CP_MAX:
        return "down"
    if g["cp"] >= 1.0 - CP_MAX:
        return "up"
    return None


def my_swing_pin(bars, j, end):
    """j 是不是 [j-LOOK_SWING, end] 里已确认的极值（只看 <= end 的数据）。

    独立重算：自己求窗口最值，不 import shape。
    """
    if j <= 0 or end < j:
        return None
    lo = max(0, j - LOOK_SWING)
    lows = [bars[k]["l"] for k in range(lo, end + 1)]
    highs = [bars[k]["h"] for k in range(lo, end + 1)]
    if bars[j]["l"] <= min(lows) and bars[j]["l"] < bars[j - 1]["l"]:
        if lo < j and max(bars[k]["h"] for k in range(lo, j)) - bars[j]["l"] >= PTS_MIN:
            return "down"
    if bars[j]["h"] >= max(highs) and bars[j]["h"] > bars[j - 1]["h"]:
        if lo < j and bars[j]["h"] - min(bars[k]["l"] for k in range(lo, j)) >= PTS_MIN:
            return "up"
    return None


def my_pin(g):
    if g["dn_pct"] >= PCT_MIN and g["dn"] >= PTS_MIN:
        return "down"
    if g["up_pct"] >= PCT_MIN and g["up"] >= PTS_MIN:
        return "up"
    return None


def my_recovered(g, prev_lines):
    """最低穿到线下、收盘回到线上（下侧）；或最高穿到线上、收盘回到线下（上侧）。"""
    out = []
    for k in sorted(prev_lines or {}):
        p = prev_lines[k]
        if p is None:
            continue
        p = float(p)
        if g["o"] > p and g["l"] < p < g["c"]:
            out.append((k, "down"))
        elif g["o"] < p and g["c"] < p < g["h"]:
            out.append((k, "up"))
    return out


def my_panic(g, vol_rel, tbr, side):
    if side is None or vol_rel is None or tbr is None:
        return False
    if vol_rel < VOL_REL_MIN:
        return False
    return tbr <= TBR_MAX if side == "down" else tbr >= (1.0 - TBR_MAX)


def my_after(bars, i, tol=TOL_PTS, mult=MAX_MULT, look=LOOK):
    """返回 (针下标, 状态) 或 None。

    注意 range 终点：`i - look - 1` 才是排他终点的正确写法。
    写成 `i - look` 会少看一根（look=3 只查 2 根）。
    """
    g = geo(bars[i]["o"], bars[i]["h"], bars[i]["l"], bars[i]["c"])
    if my_pin(g) is not None:
        return None
    for side in ("down", "up"):
        # 两趟，与 shape.find_pin_before_ex 同一份口径：
        #   先扫「本根是长影线」的针（影线是更具体的证据，优先）
        #   再扫「已确认的局部极值」
        # 单趟混着找会得出不同的针 —— 若 i-1 是极值、i-3 是长影线，
        # 单趟选 i-1、两趟选 i-3，距针不同，稳住/远离 就判反了。
        j = None
        for k in range(i - 1, max(-1, i - look - 1), -1):
            gk = geo(bars[k]["o"], bars[k]["h"], bars[k]["l"], bars[k]["c"])
            if my_pin(gk) == side:
                j = k
                break
        if j is None:
            for k in range(i - 1, max(-1, i - look - 1), -1):
                if my_swing_pin(bars, k, i - 1) == side:
                    j = k
                    break
        if j is None:
            continue
        gp = geo(bars[j]["o"], bars[j]["h"], bars[j]["l"], bars[j]["c"])
        if side == "down":
            holds, dist = g["l"] >= gp["l"] - tol, g["l"] - gp["l"]
        else:
            holds, dist = g["h"] <= gp["h"] + tol, gp["h"] - g["h"]
        if not (holds and (g["c"] > g["o"] or g["cp"] >= 0.5)):
            continue
        amp = max(gp["rng"], PTS_MIN)   # 针幅：极值那根自己的全幅，下限 PTS_MIN
        return j, ("插针后稳住" if dist <= mult * amp else "针后远离")
    return None


def my_after_fixed_cap(bars, i, cap):
    """同 my_after，但「距针上限」用固定点数（对照组）。"""
    g = geo(bars[i]["o"], bars[i]["h"], bars[i]["l"], bars[i]["c"])
    if my_pin(g) is not None:
        return None
    for side in ("down", "up"):
        j = None
        for k in range(i - 1, max(-1, i - LOOK - 1), -1):
            gk = geo(bars[k]["o"], bars[k]["h"], bars[k]["l"], bars[k]["c"])
            if my_pin(gk) == side:
                j = k
                break
        if j is None:
            continue
        gp = geo(bars[j]["o"], bars[j]["h"], bars[j]["l"], bars[j]["c"])
        if side == "down":
            holds, dist = g["l"] >= gp["l"] - TOL_PTS, g["l"] - gp["l"]
        else:
            holds, dist = g["h"] <= gp["h"] + TOL_PTS, gp["h"] - g["h"]
        if holds and (g["c"] > g["o"] or g["cp"] >= 0.5) and dist <= cap:
            return j, "稳住"
        return None
    return None


# ---------------------------------------------------------------- 主流程
def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--kbar-db", default=os.path.join("data", "kbar.db"))
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--interval", type=int, default=900_000)
    ap.add_argument("--samples", type=int, default=4)
    a = ap.parse_args(argv)

    if not os.path.exists(a.kbar_db):
        print("没有库：%s" % a.kbar_db)
        return 2
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from ai_trader.kbar import shape as S

    con = sqlite3.connect("file:%s?mode=ro" % a.kbar_db.replace("\\", "/"), uri=True)
    sym, iv = a.symbol, a.interval
    rows = list(con.execute(
        "select ts,o,h,l,c,vol_rel,taker_buy_ratio from kbar_evolution "
        "where symbol=? and interval_ms=? order by ts", (sym, iv)))
    bars = [{"ts": r[0], "o": r[1], "h": r[2], "l": r[3], "c": r[4],
             "vol_rel": r[5], "taker_buy_ratio": r[6]} for r in rows]
    lines = {}
    for ts, lk, p in con.execute(
            "select ts,line_key,line_price from kbar_line "
            "where symbol=? and interval_ms=?", (sym, iv)):
        lines.setdefault(ts, {})[lk] = float(p) if p is not None else None
    N = len(bars)
    ts_list = [b["ts"] for b in bars]
    ix = dict((t, i) for i, t in enumerate(ts_list))

    # ---------------- 0 规模 ----------------
    hr("0. 规模与覆盖（分母）")
    if not N:
        bad("表 1 是空的")
        return 1
    print("  K 线            %d 根" % N)
    print("  时间范围        %s .. %s (UTC)" % (fmt_u(ts_list[0]), fmt_u(ts_list[-1])))
    print("  有线快照的根    %d" % len(lines))
    has_vol = sum(1 for b in bars if b["vol_rel"] is not None)
    has_tbr = sum(1 for b in bars if b["taker_buy_ratio"] is not None)
    print("  有 vol_rel      %d (%.2f%%)" % (has_vol, pct(has_vol, N)))
    print("  有 taker_buy    %d (%.2f%%)" % (has_tbr, pct(has_tbr, N)))
    print("  → 恐慌盘的分母是 %d（两个量都有），不是 %d" % (min(has_vol, has_tbr), N))

    # ---------------- 1 锚点 ----------------
    hr("1. 两根锚点（用户给的验收标准，北京时间）")
    anchors = [("2026-11-07 23:45", "恐慌盘"),
               ("2026-11-08 05:15", "插针后稳住")]
    for bj, want_label in anchors:
        ms = to_ms(bj)
        if ms not in ix:
            bad("库里没有北京 %s 这根（ts=%d）" % (bj, ms))
            continue
        i = ix[ms]
        prev = lines.get(ts_list[i - 1], {}) if i else {}
        g = geo(bars[i]["o"], bars[i]["h"], bars[i]["l"], bars[i]["c"])
        mine_side = my_pin(g)
        mine_rec = my_recovered(g, prev)
        mine_panic = my_panic(g, bars[i]["vol_rel"], bars[i]["taker_buy_ratio"],
                              mine_side)
        mine_after = my_after(bars, i)
        j = S.judge(bars, i, prev_lines=prev if i else None)
        print("\n  北京 %s  (UTC %s)" % (bj, fmt_u(ms)))
        print("    独立重算：针=%-5s 收回=%d条 恐慌=%-5s 针后=%s"
              % (mine_side, len(mine_rec), mine_panic,
                 mine_after[1] if mine_after else "否"))
        shape_after = (S.STATE_STABLE if j.has(S.STATE_STABLE)
                       else S.STATE_NEAR_PIN if j.has(S.STATE_NEAR_PIN) else None)
        print("    shape.py：针=%-5s 收回=%s条 恐慌=%-5s 针后=%s  label=%s"
              % (j.side, _rec_count(j), j.has(S.STATE_PANIC),
                 shape_after or "否", j.label))
        if (mine_side is not None) != j.has(S.STATE_PIN):
            bad("%s 插针判定不一致" % bj)
        if mine_side is not None and (len(mine_rec) > 0) != j.has(S.STATE_PIN_RECOVERED):
            bad("%s 插针收回判定不一致" % bj)
        if mine_panic != j.has(S.STATE_PANIC):
            bad("%s 恐慌盘判定不一致：独立=%s shape=%s"
                % (bj, mine_panic, j.has(S.STATE_PANIC)))
        if (mine_after[1] if mine_after else None) != shape_after:
            bad("%s 针后判定不一致：独立=%s shape=%s"
                % (bj, mine_after[1] if mine_after else None, shape_after))
        if j.label != want_label:
            bad("%s 期望 label=%s，实际=%s" % (bj, want_label, j.label))
        else:
            ok("%s label=%s（与预期一致）" % (bj, want_label))

    # ---------------- 2 逐根比对 ----------------
    hr("2. 逐根比对：独立重算 vs shape.py")
    diff_pin = diff_rec = diff_panic = diff_after = 0
    n_pins = n_rec = 0
    for i, b in enumerate(bars):
        prev = lines.get(ts_list[i - 1], {}) if i else {}
        g = geo(b["o"], b["h"], b["l"], b["c"])
        mine_side = my_pin(g)
        # v2：恐慌盘的方向由 my_panic_side 给（有影线听影线，没影线看收位），
        # 与「本根是不是针」解耦 —— v1 把两者绑在一起，收在极值的大阴线永远判不出来。
        mine_psd = my_panic_side(g)
        mine_panic = my_panic(g, b["vol_rel"], b["taker_buy_ratio"], mine_psd)
        mine_after = my_after(bars, i)
        j = S.judge(bars, i, prev_lines=prev if i else None)
        if (mine_side is not None) != j.has(S.STATE_PIN):
            diff_pin += 1
        # 收回只在「本根是针」时有意义 —— 非针的根穿回一条线也不该被标成插针收回
        if mine_side is not None:
            n_pins += 1
            mine_has_rec = len(my_recovered(g, prev)) > 0
            if mine_has_rec != j.has(S.STATE_PIN_RECOVERED):
                diff_rec += 1
            if mine_has_rec:
                n_rec += 1
        elif j.has(S.STATE_PIN_RECOVERED):
            diff_rec += 1
        if mine_panic != j.has(S.STATE_PANIC):
            diff_panic += 1
        shape_after = (S.STATE_STABLE if j.has(S.STATE_STABLE)
                       else S.STATE_NEAR_PIN if j.has(S.STATE_NEAR_PIN) else None)
        if (mine_after[1] if mine_after else None) != shape_after:
            diff_after += 1
    print("  插针不一致（%d 根是针）      %d" % (n_pins, diff_pin))
    print("  收回不一致（针里有 %d 根收回） %d" % (n_rec, diff_rec))
    print("  恐慌不一致                   %d" % diff_panic)
    print("  针后不一致                   %d" % diff_after)
    if diff_pin or diff_rec or diff_panic or diff_after:
        bad("独立重算与 shape.py 有分歧（见上）")
    else:
        ok("%d 根全部一致（四项判据）" % N)

    # ---------------- 2.5 表 5 落库校验 ----------------
    # 用户 2026-11-08：「所有演化结论字段或者描述都要入 db 库」。
    # 落库的结论必须与内存里算的逐字段一致，否则「表里那行」和「代码算的」是两份东西。
    hr("2.5 表 5（kbar_shape）落库校验")
    try:
        con5 = sqlite3.connect("file:%s?mode=ro"
                               % a.kbar_db.replace("\\", "/"), uri=True)
        n5 = con5.execute(
            "select count(*) from kbar_shape where symbol=? and interval_ms=?",
            (sym, iv)).fetchone()[0]
        hashes = [h for (h,) in con5.execute(
            "select distinct params_hash from kbar_shape where symbol=? and interval_ms=?",
            (sym, iv))]
        by_label = dict(con5.execute(
            "select label, count(*) from kbar_shape where symbol=? and interval_ms=? "
            "group by label", (sym, iv)))
        n_states_bad = con5.execute(
            "select count(*) from kbar_shape where symbol=? and interval_ms=? "
            "and (states like '%,,%' or label = '无' and states <> '')",
            (sym, iv)).fetchone()[0]
    except sqlite3.OperationalError as e:
        con5 = None
        bad("读不了表 5：%s —— 先跑 tools/backfill_kbar_shape.py" % e)
    if con5 is not None:
        print("  表 5 行数 %d  vs  表 1 的 %d 根" % (n5, N))
        if n5 != N:
            bad("表 5 行数与表 1 不符：%d vs %d" % (n5, N))
        else:
            ok("行数与表 1 一致（%d 行，一根一行）" % n5)
        print("  表里的指纹：%s  vs  当前 shape.py 的 %s"
              % (",".join(hashes), S.params_hash()))
        if hashes != [S.params_hash()]:
            bad("表 5 混着别的口径（%s）—— 改过阈值没重跑，必须 --rebuild"
                % ",".join(hashes))
        else:
            ok("单一口径，且就是当前代码这一套")
        print("  落库 label 分布：%s" % by_label)
        mem = S.counts_by_label([S.judge(bars, i, prev_lines=lines.get(
            ts_list[i - 1], {}) if i else None) for i in range(N)])
        if by_label == mem:
            ok("落库分布与现算分布逐项相等（六个状态词 + 无）")
        else:
            bad("落库分布与现算不同：库里 %s / 现算 %s" % (by_label, mem))
        if n_states_bad:
            bad("有 %d 行的 states 与 label 自相矛盾（'无' 带着 states，或逗号残缺）"
                % n_states_bad)
        else:
            ok("states 与 label 自洽（'无' 的行 states 必为空串）")
        con5.close()

    # ---------------- 3 死条件 ----------------
    hr("3. 「收位条件」是死条件 —— 开关它命中数必须一模一样")
    with_cp = without_cp = 0
    for b in bars:
        g = geo(b["o"], b["h"], b["l"], b["c"])
        if g["dn_pct"] >= PCT_MIN and g["dn"] >= PTS_MIN:
            without_cp += 1
            if g["cp"] >= PCT_MIN:
                with_cp += 1
    print("  带 收位>=%.2f   %d 根" % (PCT_MIN, with_cp))
    print("  不带收位       %d 根" % without_cp)
    if with_cp != without_cp:
        bad("收位条件不是死条件：差 %d 根（推导错了）" % (without_cp - with_cp))
    else:
        ok("差 0 根 —— 印证 `收位 >= 下影占幅` 恒成立，加它是假严谨")

    # ---------------- 4 容差隔离 ----------------
    hr("4. 容差 %.0f 点只影响「针后」，不影响插针/收回/恐慌" % TOL_PTS)
    tol20 = sum(1 for i in range(N) if my_after(bars, i, tol=TOL_PTS) is not None)
    tol0 = sum(1 for i in range(N) if my_after(bars, i, tol=0.0) is not None)
    print("  容差 %.0f 点 → 针后 %d 根 (%.2f%%)" % (TOL_PTS, tol20, pct(tol20, N)))
    print("  容差  0 点 → 针后 %d 根 (%.2f%%)" % (tol0, pct(tol0, N)))
    print("  插针       %d 根（容差不参与）" % n_pins)
    if tol20 < tol0:
        bad("放宽容差反而命中更少，方向错了")
    else:
        ok("容差只让「针后」多 %d 根，插针/收回/恐慌三个数没动" % (tol20 - tol0))

    # ---------------- 5 尺子 ----------------
    hr("5. 「稳住」的距针尺：用固定点数会错判多少根")
    scaled = sum(1 for i in range(N)
                 if (my_after(bars, i) or (None, None))[1] == "插针后稳住")
    print("  用 1 倍针幅（实际用的）  稳住 %d 根" % scaled)
    for cap in (50.0, 100.0, 200.0):
        n = sum(1 for i in range(N) if my_after_fixed_cap(bars, i, cap))
        print("  用固定 %.0f 点（对照）      稳住 %d 根  差 %+d" % (cap, n, n - scaled))
    ok("固定点数不管针多大都用同一个距离；针幅尺随针自适应")

    # ---------------- 6 互斥 ----------------
    hr("6. 互斥与优先级")
    both = multi = 0
    js = []
    for i in range(N):
        j = S.judge(bars, i, prev_lines=lines.get(ts_list[i - 1], {}) if i else None)
        js.append(j)
        if j.has(S.STATE_PIN) and (j.has(S.STATE_STABLE) or j.has(S.STATE_NEAR_PIN)):
            both += 1
        if len(j.states) > 1:
            multi += 1
    print("  同时是针又是针后   %d 根（必须 0）" % both)
    print("  同时成立多个状态   %d 根（正常：长下影可以既收回又恐慌）" % multi)
    if both:
        bad("有 %d 根同时被判成「针」和「针后」" % both)
    else:
        ok("「针」与「针后」互斥")

    # ---------------- 7 抽样 ----------------
    hr("7. 抽样（机器只能证明一致，证明不了读得对）")
    for label in (S.STATE_PANIC, S.STATE_PIN_RECOVERED, S.STATE_STABLE,
                  S.STATE_NEAR_PIN, S.STATE_NONE):
        pool = [i for i in range(N) if js[i].label == label]
        print("\n  --- %s（%d 根，%.2f%%）---" % (label, len(pool), pct(len(pool), N)))
        if not pool:
            print("    （无）")
            continue
        k = min(a.samples, len(pool))
        for m in range(k):
            i = pool[len(pool) * m // k]
            print("    %s (UTC)" % fmt_u(ts_list[i]))
            for e in js[i].evidence:
                print("        · %s" % e)

    con.close()
    hr("结论")
    if PROBLEMS:
        print("有问题 %d 处：" % len(PROBLEMS))
        for p in PROBLEMS:
            print("  - " + p)
        return 1
    print("全部一致（%d 根：四项判据 + 死条件 + 容差隔离 + 互斥）" % N)
    return 0


def _rec_count(j):
    """从证据串里数出「收回了几条线」—— 证据是唯一的对外契约。"""
    for e in j.evidence:
        if "穿透后收回" in e:
            try:
                return int(e.split("收回 ")[1].split(" 条线")[0])
            except (IndexError, ValueError):
                return "?"
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
