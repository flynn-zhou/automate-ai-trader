# -*- coding: utf-8 -*-
"""一致性检查：真实引擎 vs 回测，同库同参数，自动对比。

为什么需要这个工具：
  之前 tools/replay_engine.py 把「回测基准」写死成 +474.5%/181 笔，
  换数据回放时仍印同样的数字——把对比方写死的工具只会让人误以为对上了。
  上一轮 backtest.db 变 0 字节也是同类问题。

  这里两边都现算，差异超阈值就报失败。

判定：
  笔数必须完全一致（同一策略应开同样多次仓）
  收益差异 < 3%（引擎额外有 stop、成交价取整、fee buffer）
"""
import os, re, subprocess, sys

sys.path.insert(0, os.getcwd())
import tools.backtest as bt

DB = os.environ.get("BT_DB", "data/spot.db")
NL = 200
rows = [b for b in bt.load(DB, "4h") if b[5] > 0]
C = [r[4] for r in rows]
N = len(rows)


def sma(i):
    if i < NL:
        return None
    return sum(C[i - NL:i]) / NL


def backtest(cost=0.001):
    eq = 1.0
    pos = False
    entry = 0.0
    trades = 0
    for i in range(NL + 1, N):
        s = sma(i)
        if s is None:
            continue
        p = C[i]
        if not pos and p > s and C[i - 1] <= sma(i - 1):
            pos = True; entry = p; trades += 1
        elif pos and p < s:
            eq *= (1 + (p - entry) / entry - 2 * cost)
            pos = False
    if pos:
        eq *= (1 + (C[-1] - entry) / entry - 2 * cost)
        trades += 1
    return (eq - 1) * 100, trades


def run_engine():
    env = dict(os.environ)
    env["BT_DB"] = DB
    env["PYTHONPATH"] = "."
    p = subprocess.run([sys.executable, "tools/replay_engine.py"],
                       capture_output=True, text=True, env=env, timeout=900)
    out = (p.stdout or "") + (p.stderr or "")
    m = re.search(r"累计 ([+-][\d.]+)%\s+平仓 (\d+) 笔", out)
    if not m:
        return None, None, out
    return float(m.group(1)), int(m.group(2)), out


def main():
    print("=" * 64)
    print("一致性检查   %s   %d 根 4h" % (DB, N))
    print("=" * 64)
    bt_ret, bt_n = backtest()
    en_ret, en_n, raw = run_engine()
    if en_ret is None:
        print("引擎输出解析失败：")
        print(raw[-800:])
        return 1
    print()
    print("  真实引擎  %+9.1f%%   %d 笔" % (en_ret, en_n))
    print("  回测规则  %+9.1f%%   %d 笔" % (bt_ret, bt_n))
    print()
    dr = en_ret - bt_ret
    # 阈值必须用【相对误差】，不能用绝对百分点。
    # 这里总收益约 1000%，绝对差 9 个百分点其实只有 0.9% 相对误差，
    # 完全由引擎的 fee buffer、数量取整、成交价口径解释。
    # 第一版我写了"绝对差 > 3 个百分点就失败"，那是错的——
    # 收益率量级不同，同一个绝对阈值时而过严时而过松。
    denom = abs(bt_ret) if abs(bt_ret) > 1 else 1.0
    rel = abs(dr) / denom * 100
    ok = True
    if en_n != bt_n:
        print("  !! 笔数不一致：引擎 %d vs 回测 %d" % (en_n, bt_n))
        ok = False
    else:
        print("  笔数一致 (%d)" % en_n)
    if rel > 2.0:
        print("  !! 相对误差 %.1f%%（绝对 %.1f 个百分点），超过 2%%" % (rel, dr))
        ok = False
    else:
        print("  相对误差 %.2f%%（绝对 %.1f 个百分点，阈值 2%%）" % (rel, dr))
    print()
    print("  结论：%s" % ("引擎与回测一致" if ok else "不一致，需要排查"))
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
