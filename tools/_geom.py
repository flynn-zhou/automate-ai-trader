# -*- coding: utf-8 -*-
"""残差法能否成立，取决于 GBM 的 K 线几何是否与真实 BTC 匹配。
若不匹配，伪影大小不同，残差就被污染了。先量一量。
"""
import math, random, sys, statistics
sys.path.insert(0, "/home/cryptobot/ai-trader/tools")
import research_limit_neighbor as R

T0, STEP = 1567965300000, 300000


def geom(name, rows, cap=60000):
    rows = rows[-cap:] if len(rows) > cap else rows
    rng, upw, dnw, body = [], [], [], []
    for (t, o, h, l, c) in rows:
        hi, lo = max(o, c), min(o, c)
        rng.append((h - l) / lo * 100)
        if h > lo:
            upw.append((h - hi) / (h - lo))
            dnw.append((lo - l) / (h - lo))
        body.append(abs(c - o) / lo * 100)
    print("  %-22s bar数 %6d" % (name, len(rows)))
    print("    振幅(HL/close)  中位 %.4f%%  均值 %.4f%%" % (statistics.median(rng), sum(rng)/len(rng)))
    print("    实体(|C-O|)     中位 %.4f%%  均值 %.4f%%" % (statistics.median(body), sum(body)/len(body)))
    print("    上影占比        中位 %.3f        均值 %.3f" % (statistics.median(upw), sum(upw)/len(upw)))
    print("    下影占比        中位 %.3f        均值 %.3f" % (statistics.median(dnw), sum(dnw)/len(dnw)))
    return sum(rng)/len(rng), sum(upw)/len(upw), sum(dnw)/len(dnw)


def synth(n, seed=1, sigma=0.0015):
    rnd = random.Random(seed)
    rows = []
    px = 10000.0
    for i in range(n):
        o = px
        c = o * math.exp(rnd.gauss(0.0, sigma))
        hi = max(o, c) * (1 + abs(rnd.gauss(0, sigma * 0.4)))
        lo = min(o, c) * (1 - abs(rnd.gauss(0, sigma * 0.4)))
        rows.append((T0 + i * STEP, o, hi, lo, c))
        px = c
    return rows


rows, _ = R.load("BTCUSDT", "5m")
print("=" * 78)
print("K 线几何对照（最近 60000 根）")
print("=" * 78)
geom("真实 BTCUSDT 5m", rows)
print()
geom("合成 GBM sigma=0.15%", synth(150000, 1))
print()
print("判读：若 GBM 的振幅/影线占比与 BTC 差得远，残差法就不干净——")
print("      因为『买在最低点』的伪影大小正比于振幅。")
