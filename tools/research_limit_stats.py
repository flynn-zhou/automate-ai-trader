# -*- coding: utf-8 -*-
"""B 的统计检验：不看终值（复利混沌），只看单笔收益分布。

上一轮发现：同一策略，出场费率 0.02% vs 0.05% 让终值差 4 倍；
随机丢掉 80% 的成交反而让终值涨 10 倍。这说明终值不可用。

改看四个不受复利影响的量：
  单笔均值 / t 值 / 逐年 / 样本外
并给出亏损尾巴（最差 5%、最差单笔）。

同时对比 A（市价追）与 B（限价等），同一持有期、同一成本口径。
成本口径统一为：进 maker 0.02%（B）或 taker 0.05%（A），出 taker 0.05%。
这是现实中最坏的组合，宁可悲观。
"""
import sqlite3, time, math

HIST = "/home/cryptobot/ai-trader/data/hist.db"
W, HOLD, WAIT = 100, 48, 48
BREAK = 0.10
F_IN_MAKER, F_IN_TAKER, F_OUT = 0.0002, 0.0005, 0.0005


def load(sym, iv):
    c = sqlite3.connect(HIST)
    r = c.execute("select open_time,o,h,l,c from k2 where symbol=? and interval=? order by open_time",
                  (sym, iv)).fetchall()
    fr = c.execute("select ts, rate from funding where symbol=? order by ts", (sym,)).fetchall()
    c.close()
    return r, fr


def fund_between(fr, t0, t1):
    return sum(x for ts, x in fr if t0 <= ts < t1)


def collect(rows, fr, mode):
    """返回每笔的净收益率（%，相对名义额），不做复利。"""
    n = len(rows)
    t = [r[0] for r in rows]
    o = [r[1] for r in rows]
    l = [r[3] for r in rows]
    c = [r[4] for r in rows]
    out = []
    placed = filled = 0
    i = W
    while i < n - HOLD:
        sup = min(l[i - W:i])
        if mode == "A":
            if c[i] >= sup * (1 - BREAK / 100.0):
                i += 1
                continue
            j = i + 1
            if j >= n:
                break
            entry, fill_idx, fin = o[j], j, F_IN_TAKER
        else:
            placed += 1
            limit_px = sup * 1.0002
            fill_idx, entry = None, 0.0
            for k in range(i, min(i + WAIT, n - HOLD)):
                if l[k] <= limit_px:
                    entry, fill_idx = limit_px, k
                    break
            if fill_idx is None:
                i += 1
                continue
            filled += 1
            fin = F_IN_MAKER
        exit_idx = min(fill_idx + HOLD, n - 1)
        if exit_idx <= fill_idx:
            i = fill_idx + 1
            continue
        gross = (c[exit_idx] - entry) / entry * 100.0
        f = fund_between(fr, t[fill_idx], t[exit_idx]) / entry * 100.0
        cost = (fin + F_OUT) * 100.0
        out.append(dict(ts=t[fill_idx], net=gross - f - cost, gross=gross,
                        drop=(min(l[fill_idx:exit_idx + 1]) - entry) / entry * 100.0))
        i = exit_idx + 1
    return out, placed, filled


def stats(xs):
    n = len(xs)
    if n < 3:
        return n, 0.0, 0.0
    m = sum(xs) / n
    var = sum((x - m) ** 2 for x in xs) / (n - 1)
    return n, m, m / math.sqrt(var / n) if var > 0 else 0.0


def report(name, rows, fr, mode):
    tr, placed, filled = collect(rows, fr, mode)
    net = [x["net"] for x in tr]
    n, m, tv = stats(net)
    s = sorted(net)
    print("  %-22s n=%-5d 单笔均值 %+0.4f%%  t=%5.2f" % (name, n, m, tv))
    if mode == "B":
        print("      挂单 %d 成交 %d（%.1f%%）  插针中位 %+.3f%%  最差5%% %+.2f%%  最差单笔 %+.2f%%"
              % (placed, filled, filled * 100.0 / max(1, placed),
                 sorted(x["drop"] for x in tr)[len(tr) // 2],
                 s[int(len(s) * 0.05)], s[0]))
    else:
        print("      插针中位 %+.3f%%  最差5%% %+.2f%%  最差单笔 %+.2f%%"
              % (sorted(x["drop"] for x in tr)[len(tr) // 2],
                 s[int(len(s) * 0.05)], s[0]))
    ys = {}
    for x in tr:
        ys.setdefault(time.gmtime(x["ts"] / 1000.0).tm_year, []).append(x["net"])
    line = ["%d:%+0.3f" % (y, sum(v) / len(v)) for y, v in sorted(ys.items()) if len(v) > 20]
    print("      逐年单笔均值: " + "  ".join(line))
    pos = sum(1 for y, v in sorted(ys.items()) if len(v) > 20 and sum(v) / len(v) > 0)
    tot = sum(1 for y, v in sorted(ys.items()) if len(v) > 20)
    print("      正年数 %d/%d" % (pos, tot))
    cut = int(len(tr) * 0.7)
    for tag, part in (("前70%", tr[:cut]), ("后30%", tr[cut:])):
        nn, mm, tt = stats([x["net"] for x in part])
        print("      %s  n=%-5d 均值 %+0.4f%%  t=%5.2f" % (tag, nn, mm, tt))
    return m, tv


def main():
    rows, fr = load("BTCUSDT", "5m")
    print("BTCUSDT 5m %d 根  %.2f 年  持有 %d 根（%.0f 小时）"
          % (len(rows), (rows[-1][0] - rows[0][0]) / (365.25 * 24 * 3600 * 1000),
             HOLD, HOLD * 5 / 60.0))
    print("成本口径：进 maker 0.02%%（B）/ taker 0.05%%（A），出 taker 0.05%%")
    print("=" * 96)
    report("A 市价追（跌破后买）", rows, fr, "A")
    print("-" * 96)
    report("B 限价等（挂支撑上）", rows, fr, "B")
    print("=" * 96)
    print("判据：单笔均值 > 0 且 t > 2 且 后30% 仍为正 -> 才算真边际")


if __name__ == "__main__":
    main()
