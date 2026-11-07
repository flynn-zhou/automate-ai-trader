# -*- coding: utf-8 -*-
"""资金费的真实代价：不是全局平均，而是【策略持仓期间】的平均。

为什么这个区别重要：
  全局平均资金费 = 所有时间段的平均，大部分时间策略是空仓的。
  但策略只在价格上穿 SMA200 时做多——那正是牛市，也正是资金费最高的时候。
  用全局平均会【低估】策略付出的资金费。

这个数字直接决定现货还是永续：
  永续成本 = 手续费(0.05%x2) + 持仓期间资金费
  现货成本 = 手续费(0.10%x2)
  哪个低用哪个。
"""
import json, os, sys, urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CACHE = "data/funding_btcusdt.json"
MS8H = 8 * 3600 * 1000


def _fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return json.loads(urllib.request.urlopen(req, timeout=25).read())


def download():
    if os.path.exists(CACHE):
        return json.load(open(CACHE))
    out, start = [], int(datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    while True:
        url = ("https://fapi.binance.com/fapi/v1/fundingRate?symbol=BTCUSDT"
               "&startTime=%d&limit=1000" % start)
        d = _fetch(url)
        if not d:
            break
        out += d
        start = d[-1]["fundingTime"] + 1
        print("  拉到 %d 条" % len(out))
        if len(d) < 1000:
            break
    os.makedirs("data", exist_ok=True)
    json.dump(out, open(CACHE, "w"))
    return out


def load_bars():
    """从本地库读 4h 收盘序列。"""
    import sqlite3
    c = sqlite3.connect("data/market.db", timeout=10)
    rows = list(c.execute(
        "SELECT open_time, c FROM bars WHERE interval_ms=14400000 "
        "ORDER BY open_time"))
    c.close()
    return [(int(r[0]), float(r[1])) for r in rows if r[1]]


def sma(vals, n, i):
    if i + 1 < n:
        return None
    return sum(vals[i - n + 1:i + 1]) / n


def main():
    print("=" * 66)
    print("资金费的真实代价（策略持仓期间，不是全局平均）")
    print("=" * 66)

    fund = download()
    if not fund:
        print("拿不到资金费数据")
        return 1
    fmap = {int(f["fundingTime"]): float(f["fundingRate"]) for f in fund}
    all_rates = list(fmap.values())
    print("资金费结算 %d 次（%s ~ %s）"
          % (len(all_rates),
             datetime.utcfromtimestamp(min(fmap) / 1000).date(),
             datetime.utcfromtimestamp(max(fmap) / 1000).date()))
    print()
    print("【全局】平均每次 %.6f%%  年化 %.2f%%"
          % (sum(all_rates) / len(all_rates) * 100,
             sum(all_rates) / len(all_rates) * 3 * 365 * 100))
    pos = sum(1 for r in all_rates if r > 0)
    print("       正费率占比 %.1f%%（多头付钱的时间）" % (pos / len(all_rates) * 100))

    bars = load_bars()
    if len(bars) < 250:
        print("\n!! 本地 4h K 线只有 %d 根，不足以计算 SMA200" % len(bars))
        print("   需要先补数据：python tools/download_klines.py")
        return 1

    closes = [b[1] for b in bars]
    n = 200
    held_ms = 0
    held_rates = []
    # 逐根 4h 判断：收盘价在 SMA200 之上 = 策略持仓
    for i in range(len(bars)):
        t, c = bars[i]
        m = sma(closes, n, i)
        if m is None:
            continue
        if c > m:
            held_ms += MS8H if i + 1 >= len(bars) else (bars[i + 1][0] - t)
            # 这根 4h 期间对应哪几次资金费结算？
            for k in range(3):
                ft = t + k * MS8H
                # 找最接近的结算时刻（币安整点：00/08/16 UTC）
                for cand in fmap:
                    if abs(cand - ft) <= MS8H // 3:
                        held_rates.append(fmap[cand])
                        break

    total_ms = bars[-1][0] - bars[0][0]
    frac = held_ms / total_ms if total_ms else 0
    print()
    print("【策略】4h SMA200 之上（= 持多仓）的时间占比 %.1f%%" % (frac * 100))
    print("       对应资金费结算 %d 次" % len(held_rates))
    if held_rates:
        avg_held = sum(held_rates) / len(held_rates)
        print("       持仓期间平均每次 %.6f%%" % (avg_held * 100))
        print("       若全程做多：年化 %.2f%%" % (avg_held * 3 * 365 * 100))
        print("       按实际持仓占比折算：年化 %.2f%%（占权益）"
              % (avg_held * 3 * 365 * frac * 100))
    print()
    print("-" * 66)
    print("结论：")
    print("  永续成本 = 手续费 0.10%%/往返 + 资金费（上面那个折算值）")
    print("  现货成本 = 手续费 0.20%%/往返 + 0 资金费")
    return 0


if __name__ == "__main__":
    sys.exit(main())
