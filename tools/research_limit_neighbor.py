# -*- coding: utf-8 -*-
"""第 134 轮：B 路径（提前挂限价等着）的参数邻域测试。

================================ 预注册判据 ================================
本文件在跑之前就把判据写死，跑完不许改。铁律 9：真实有效是一片高原，过拟合是一根针尖。

单格 PASS 需要同时满足（缺一不可）：
    n >= 100                     样本够
    raw_mean > 0                 单笔均值（含 beta）为正
    alpha_mean > 0               剥掉 beta 后的超额为正
    t_alpha > 2                  超额显著（t 打在 alpha 上，不打在 raw 上）
    后30% raw_mean > 0           样本外同号

高原判定（全部满足才算过邻域关）：
    G1 复制闸通过：基线格(WAIT48/HOLD48/W100/off0.02) 复现上一轮 +0.1228%/t=5.57（容差 15%/0.6）
    G2 基线格本身 PASS
    G3 全格 PASS 率 >= 70%
    G4 基线在四个维度上的紧邻格（WAIT 24/96, HOLD 24/96, W 50/200, off 0/0.05）全部 PASS
任一不满足 -> 判为针尖，B 埋掉。

================================ beta 剥离 ================================
alpha = 单笔净收益 - 同段行情、同持有期的无条件持有收益。
BASET = 全期所有 bar 的 HOLD 根收益均值（按年加权，权重=当年成交占比）。
t_alpha = (raw_mean - base) / se(raw)。
这一步是必做的：BTC 7.15 年涨约 20 倍，均摊到每根 5m 的正漂移足以让任何"下跌买入"看起来有效。

================================ 已知修正 ================================
research_limit_stats.py 的资金费算错了：sum(rate)/entry*100，量纲上把资金费缩小了 entry 倍
（entry 约几万），等于资金费被当成 0。本脚本改为 sum(rate)*100（正确的名义额百分比）。
差异量级约 0.005%/笔，虽小，但这是"验证手段本身会撒谎"那一族，必须写清楚。

用法：
    ./.venv/bin/python tools/research_limit_neighbor.py          # 全网格
    ./.venv/bin/python tools/research_limit_neighbor.py smoke    # 只跑复制闸
"""
import bisect
import json
import math
import os
import sqlite3
import sys
import time
from collections import deque

HIST = "/home/cryptobot/ai-trader/data/hist.db"
OUT = "/home/cryptobot/ai-trader/tools/r134_neighbor.json"

SYM, IV = "BTCUSDT", "5m"
F_IN_MAKER, F_OUT_TAKER = 0.0002, 0.0005

GRID_WAIT = [12, 24, 48, 96]
GRID_HOLD = [12, 24, 48, 96]
GRID_W = [50, 100, 200]
GRID_OFF = [0.0, 0.02, 0.05, 0.10, 0.20]   # 百分比：限价 = 支撑 * (1 + off/100)

BASE_CFG = dict(WAIT=48, HOLD=48, W=100, OFF=0.02)
REPL_MEAN, REPL_T = 0.1228, 5.57           # 上一轮 research_limit_stats.py 的读数


# ---------------------------------------------------------------- 数据
def load(sym, iv):
    c = sqlite3.connect(HIST)
    rows = c.execute(
        "select open_time,o,h,l,c from k2 where symbol=? and interval=? order by open_time",
        (sym, iv)).fetchall()
    fr = c.execute("select ts, rate from funding where symbol=? order by ts", (sym,)).fetchall()
    c.close()
    return rows, fr


def window_min(arr, w):
    """m[k] = min(arr[max(0,k-w+1) .. k])"""
    n = len(arr)
    m = [0.0] * n
    dq = deque()
    for k in range(n):
        v = arr[k]
        while dq and arr[dq[-1]] >= v:
            dq.pop()
        dq.append(k)
        if dq[0] <= k - w:
            dq.popleft()
        m[k] = arr[dq[0]]
    return m


def stats(xs):
    n = len(xs)
    if n < 3:
        return n, 0.0, 0.0, 0.0
    m = sum(xs) / n
    var = sum((x - m) ** 2 for x in xs) / (n - 1)
    se = math.sqrt(var / n) if var > 0 else 0.0
    return n, m, se, (m / se if se > 0 else 0.0)


# ---------------------------------------------------------------- 单格
ARRS = {}


def prepare(rows):
    t = [r[0] for r in rows]
    o = [r[1] for r in rows]
    h = [r[2] for r in rows]
    lo = [r[3] for r in rows]
    c = [r[4] for r in rows]
    ARRS.update(t=t, o=o, h=h, l=lo, c=c, n=len(rows))


def run_cfg(W, HOLD, WAIT, OFF, cache):
    t, o, h, l, c = ARRS["t"], ARRS["o"], ARRS["h"], ARRS["l"], ARRS["c"]
    n = ARRS["n"]
    fund_ts, fund_cum = ARRS["fund_ts"], ARRS["fund_cum"]

    if W not in cache:
        cache[W] = window_min(l, W)
    rminW = cache[W]                                  # min(l[i-W:i]) = rminW[i-1]
    swkey = WAIT
    if ("sw", swkey) not in cache:
        rev = window_min(l[::-1], WAIT)
        cache[("sw", swkey)] = rev
    revmin = cache[("sw", swkey)]                     # min(l[i:i+WAIT]) = revmin[n-1-i]

    mult = 1.0 + OFF / 100.0
    end = n - HOLD
    nets, drops, fts = [], [], []
    placed = filled = 0
    i = W
    while i < end:
        if i == 0:
            i += 1
            continue
        sup = rminW[i - 1]
        placed += 1
        lp = sup * mult
        hi = i + WAIT
        if hi > end:
            hi = end
        # 快速否决：整段最低价都碰不到限价
        if hi - i == WAIT:
            k = n - 1 - i
            if k < n and revmin[k] > lp:
                i += 1
                continue
        fi = -1
        k = i
        while k < hi:
            if l[k] <= lp:
                fi = k
                break
            k += 1
        if fi < 0:
            i += 1
            continue
        filled += 1
        entry = lp                                    # 保守：只在限价成交，不许跳空改善
        exit_idx = fi + HOLD
        if exit_idx > n - 1:
            exit_idx = n - 1
        if exit_idx <= fi:
            i = fi + 1
            continue
        exit_px = c[exit_idx]
        gross = (exit_px - entry) / entry * 100.0
        a = bisect.bisect_left(fund_ts, t[fi])
        b = bisect.bisect_left(fund_ts, t[exit_idx])
        fund_pct = (fund_cum[b] - fund_cum[a]) * 100.0  # 修正：名义额百分比
        cost = (F_IN_MAKER + F_OUT_TAKER) * 100.0
        nets.append(gross - fund_pct - cost)
        fts.append(t[fi])
        drops.append((min(l[fi:exit_idx + 1]) - entry) / entry * 100.0)
        i = exit_idx + 1

    return nets, drops, fts, placed, filled


# ---------------------------------------------------------------- beta 基线
def beta_base(HOLD, cache):
    """无条件持有 HOLD 根的收益均值：全期 + 按年。"""
    key = ("base", HOLD)
    if key in cache:
        return cache[key]
    c, t, n = ARRS["c"], ARRS["t"], ARRS["n"]
    by_year = {}
    allr = []
    for i in range(0, n - HOLD):
        r = (c[i + HOLD] - c[i]) / c[i] * 100.0
        allr.append(r)
        y = time.gmtime(t[i] / 1000.0).tm_year
        d = by_year.get(y)
        if d is None:
            d = by_year[y] = [0.0, 0]
        d[0] += r
        d[1] += 1
    by_year = {y: v[0] / v[1] for y, v in by_year.items()}
    res = (sum(allr) / len(allr), by_year)
    cache[key] = res
    return res


def beta_for_trades(fts, HOLD, cache):
    """按成交年份加权的 beta 期望。"""
    base_all, by_year = beta_base(HOLD, cache)
    acc = 0.0
    for ts in fts:
        y = time.gmtime(ts / 1000.0).tm_year
        acc += by_year.get(y, base_all)
    return acc / len(fts) if fts else 0.0


# ---------------------------------------------------------------- 报告
def evaluate(W, HOLD, WAIT, OFF, cache):
    nets, drops, fts, placed, filled = run_cfg(W, HOLD, WAIT, OFF, cache)
    if len(nets) < 3:
        return None
    n, m, se, tv = stats(nets)
    base = beta_for_trades(fts, HOLD, cache)
    alpha = m - base
    t_alpha = alpha / se if se > 0 else 0.0

    ys = {}
    for ts, x in zip(fts, nets):
        ys.setdefault(time.gmtime(ts / 1000.0).tm_year, []).append(x)
    year_mean = {y: sum(v) / len(v) for y, v in ys.items() if len(v) > 20}
    pos_y = sum(1 for v in year_mean.values() if v > 0)
    tot_y = len(year_mean)

    cut = int(n * 0.7)
    _, m_in, _, t_in = stats(nets[:cut])
    _, m_out, _, t_out = stats(nets[cut:])
    sd = sorted(drops)
    srt = sorted(nets)
    res = dict(W=W, HOLD=HOLD, WAIT=WAIT, OFF=OFF, n=n, placed=placed, filled=filled,
               fill_rate=filled * 100.0 / max(1, placed),
               raw_mean=m, se=se, t_raw=tv, base=base, alpha=alpha, t_alpha=t_alpha,
               pos_years=pos_y, tot_years=tot_y,
               year_mean={str(k): round(v, 4) for k, v in sorted(year_mean.items())},
               in70=m_in, in70_t=t_in, out30=m_out, out30_t=t_out,
               adverse_med=sd[len(sd) // 2] if sd else 0.0,
               worst5=srt[int(len(srt) * 0.05)], worst=srt[0])
    res["pass"] = bool(n >= 100 and m > 0 and alpha > 0 and t_alpha > 2 and m_out > 0)
    return res


def fmt(r):
    return ("W=%-3d HOLD=%-2d WAIT=%-2d off=%.2f%% | n=%-5d raw %+0.4f%% t=%5.2f | "
            "beta %+0.4f%% alpha %+0.4f%% ta=%5.2f | 年 %d/%d 前70 %+0.4f 后30 %+0.4f(t=%4.2f) %s"
            % (r["W"], r["HOLD"], r["WAIT"], r["OFF"], r["n"], r["raw_mean"], r["t_raw"],
               r["base"], r["alpha"], r["t_alpha"], r["pos_years"], r["tot_years"],
               r["in70"], r["out30"], r["out30_t"], "PASS" if r["pass"] else "----"))


def main():
    smoke = len(sys.argv) > 1 and sys.argv[1] == "smoke"
    t0 = time.time()
    rows, fr = load(SYM, IV)
    prepare(rows)
    ARRS["fund_ts"] = [x[0] for x in fr]
    ARRS["fund_cum"] = [0.0]
    acc = 0.0
    for _, r in fr:
        acc += r
        ARRS["fund_cum"].append(acc)
    yrs = (rows[-1][0] - rows[0][0]) / (365.25 * 24 * 3600 * 1000)
    print("%s %s  %d 根  %.2f 年  资金费 %d 条" % (SYM, IV, len(rows), yrs, len(fr)), flush=True)
    cache = {}

    # ---- G1 复制闸
    print("=" * 118)
    print("G1 复制闸：基线格 W=100 HOLD=48 WAIT=48 off=0.02%% 应复现 +%.4f%% / t=%.2f" % (REPL_MEAN, REPL_T))
    rep = evaluate(BASE_CFG["W"], BASE_CFG["HOLD"], BASE_CFG["WAIT"], BASE_CFG["OFF"], cache)
    print("   " + fmt(rep))
    dm = abs(rep["raw_mean"] - REPL_MEAN) / REPL_MEAN
    dt = abs(rep["t_raw"] - REPL_T) / REPL_T
    g1 = dm <= 0.15 and dt <= 0.6
    print("   偏差：均值 %.1f%%  t %.1f%%   -> G1 %s" % (dm * 100, dt * 100, "通过" if g1 else "不通过"))
    if not g1:
        print("   !! 复制不出来，说明本脚本与上一轮口径不同，网格结果不可信。先停下修口径。", flush=True)
    if smoke:
        return

    # ---- 全网格
    print("=" * 118)
    print("全网格 %d 格（WAIT %s x HOLD %s x W %s x off %s）"
          % (len(GRID_WAIT) * len(GRID_HOLD) * len(GRID_W) * len(GRID_OFF),
             GRID_WAIT, GRID_HOLD, GRID_W, GRID_OFF), flush=True)
    results = []
    k = 0
    for W in GRID_W:
        for HOLD in GRID_HOLD:
            for WAIT in GRID_WAIT:
                for OFF in GRID_OFF:
                    k += 1
                    r = evaluate(W, HOLD, WAIT, OFF, cache)
                    if r is None:
                        continue
                    results.append(r)
                    print("[%3d] %s  (%.0fs)" % (k, fmt(r), time.time() - t0), flush=True)

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=1)
    print("=" * 118)
    print("已写 %s" % OUT)

    # ---- 判定
    npass = sum(1 for r in results if r["pass"])
    rate = npass * 100.0 / len(results)
    base_r = [r for r in results
              if r["W"] == BASE_CFG["W"] and r["HOLD"] == BASE_CFG["HOLD"]
              and r["WAIT"] == BASE_CFG["WAIT"] and r["OFF"] == BASE_CFG["OFF"]][0]

    def nbr(key, vals):
        out = []
        for v in vals:
            m = [r for r in results if r[key] == v and all(
                r[k2] == base_r[k2] for k2 in ("W", "HOLD", "WAIT", "OFF") if k2 != key)]
            if m:
                out.append((v, m[0]["pass"], m[0]["alpha"], m[0]["t_alpha"]))
            else:
                out.append((v, None, 0, 0))
        return out

    nb = {"WAIT": nbr("WAIT", [24, 96]), "HOLD": nbr("HOLD", [24, 96]),
          "W": nbr("W", [50, 200]), "OFF": nbr("OFF", [0.0, 0.05])}
    g4 = all(v[1] for vs in nb.values() for v in vs if v[1] is not None)

    print()
    print("G2 基线格 PASS            : %s" % base_r["pass"])
    print("G3 全格 PASS 率           : %d/%d = %.1f%%  (需 >= 70%%)" % (npass, len(results), rate))
    print("G4 基线四维紧邻全 PASS    : %s" % g4)
    for key, vs in nb.items():
        print("     %-5s " % key + "  ".join(
            "%s:%s(alpha %+0.4f t=%4.2f)" % (v[0], "P" if v[1] else ("-" if v[1] is not None else "?"),
                                             v[2], v[3]) for v in vs))
    print()
    verdict = g1 and base_r["pass"] and rate >= 70.0 and g4
    print("=" * 118)
    print("结论：%s" % ("高原 —— B 通过邻域关，可进入工程化" if verdict else "针尖 —— B 未通过邻域关"))
    print("（判据在文件头预注册，跑前写死）")

    top = sorted(results, key=lambda r: -r["t_alpha"])[:10]
    print("\nt_alpha 前十：")
    for r in top:
        print("  " + fmt(r))


if __name__ == "__main__":
    main()
