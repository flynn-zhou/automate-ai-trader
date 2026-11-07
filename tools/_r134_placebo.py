# -*- coding: utf-8 -*-
"""决定性问题：引擎在【已知没有边际】的价格序列上会不会报出正收益？

如果 placebo 也报 +0.3%/笔、t=17，那 r134 的结论就是引擎自身的伪影，
而不是市场结构 —— 这正是铁律 3「验证手段本身会撒谎」。

三条 placebo（全部无记忆、无均值回归、无漂移）：
  P1 合成 GBM：标准几何布朗运动，5m 波动率按 BTC 实测水平
  P2 打乱 BTC 真实 5m 收益序列：保留收益分布（厚尾），摧毁时序结构
  P3 反向序列：把 BTC 价格路径倒过来（若正收益来自"跌了会弹"的前视，反向应崩）
预期：alpha 应在 0 附近（±0.02%），t 绝对值应很小。
"""
import math
import random
import sys

sys.path.insert(0, "/home/cryptobot/ai-trader/tools")
import research_limit_neighbor as R

CELLS = [(50, 48, 96, 0.05), (50, 48, 48, 0.02), (100, 48, 48, 0.02), (200, 96, 96, 0.10)]


def fmt(r):
    return ("n=%-5d raw %+0.4f%% t=%6.2f | beta %+0.4f%% alpha %+0.4f%% ta=%6.2f | 年 %d/%d 后30 %+0.4f(t=%5.2f) %s"
            % (r["n"], r["raw_mean"], r["t_raw"], r["base"], r["alpha"], r["t_alpha"],
               r["pos_years"], r["tot_years"], r["out30"], r["out30_t"],
               "PASS" if r["pass"] else "----"))


def run_series(name, rows):
    R.prepare(rows)
    R.ARRS["fund_ts"] = []
    R.ARRS["fund_cum"] = [0.0]
    cache = {}
    print("\n### %s  (%d 根)" % (name, len(rows)))
    for c in CELLS:
        r = R.evaluate(c[0], c[1], c[2], c[3], cache)
        if r is None:
            print("   W=%-3d HOLD=%-2d WAIT=%-2d off=%.2f  n<3" % c)
            continue
        print("   W=%-3d HOLD=%-2d WAIT=%-2d off=%.2f  %s" % (c[0], c[1], c[2], c[3], fmt(r)))
    return


T0 = 1567965300000
STEP = 300000


def synth_gbm(n, sigma=0.0015, seed=1, drift=0.0):
    rnd = random.Random(seed)
    rows = []
    px = 10000.0
    for i in range(n):
        o = px
        c = o * math.exp(rnd.gauss(drift, sigma))
        hi = max(o, c) * (1 + abs(rnd.gauss(0, sigma * 0.4)))
        lo = min(o, c) * (1 - abs(rnd.gauss(0, sigma * 0.4)))
        rows.append((T0 + i * STEP, o, hi, lo, c))
        px = c
    return rows


def from_returns(rets, seed_obj):
    """用给定收益序列重建 OHLC（wick 用随机比例，不引入结构）。"""
    rows = []
    px = 10000.0
    for i, r in enumerate(rets):
        o = px
        c = o * math.exp(r)
        w1 = abs(seed_obj.gauss(0, abs(r) + 1e-6))
        w2 = abs(seed_obj.gauss(0, abs(r) + 1e-6))
        rows.append((T0 + i * STEP, o, max(o, c) * (1 + w1), min(o, c) * (1 - w2), c))
        px = c
    return rows


def main():
    # --- P1 GBM
    for seed in (1, 2):
        run_series("P1 合成 GBM seed=%d（无记忆、无漂移、无均值回归）" % seed,
                   synth_gbm(150000, seed=seed))

    # --- 真实 BTC 序列（对照，应复现 r134 的高 alpha）
    rows, fr = R.load("BTCUSDT", "5m")
    R.prepare(rows)
    ts = [x[0] for x in fr]
    cum = [0.0]
    acc = 0.0
    for _, v in fr:
        acc += v
        cum.append(acc)
    R.ARRS["fund_ts"] = ts
    R.ARRS["fund_cum"] = cum
    cache = {}
    print("\n### 对照：真实 BTCUSDT 5m（带资金费）")
    for c in CELLS:
        r = R.evaluate(c[0], c[1], c[2], c[3], cache)
        print("   W=%-3d HOLD=%-2d WAIT=%-2d off=%.2f  %s" % (c[0], c[1], c[2], c[3], fmt(r)))

    # --- P2 打乱收益
    c_arr = [x[4] for x in rows]
    rets = [math.log(c_arr[i + 1] / c_arr[i]) for i in range(len(c_arr) - 1)]
    for seed in (7, 8):
        rr = list(rets)
        random.Random(seed).shuffle(rr)
        run_series("P2 打乱 BTC 5m 收益（保留厚尾、摧毁时序）seed=%d" % seed,
                   from_returns(rr, random.Random(seed + 100)))

    # --- P3 反向
    rev = list(reversed([x for x in rows]))
    rev = [(T0 + i * STEP, r[1], r[2], r[3], r[4]) for i, r in enumerate(rev)]
    run_series("P3 BTC 价格路径整体倒放", rev)


if __name__ == "__main__":
    main()
