# -*- coding: utf-8 -*-
"""第 5 步 · 事后层探针：从库里读出真值，**独立重算一遍**。

探针不 import `ai_trader.kbar.forward` 里的任何公式 —— 它自己重写一遍。
否则公式错了探针跟着错，两边一起绿（这个项目踩过这种坑）。

查什么：
  0. 表 3 的规模与覆盖
  1. 独立性红线：当下判定路径不许 import forward 模块
  2. ret_k 用**独立取价**重算，逐根比对
  3. ret_k 不许偷未来：必须等于 c[t+k]/c[t]，不是 c[t]/c[t+k]，也不是 c[t+k-1]
  4. held 用独立实现的「守住」重算，逐根比对
  5. held 的参照价必须等于表 2.5 里同一 (ts, on_line) 的 line_price
  6. 无事件的根 held 必须为空（不许填 0）
  7. 等间隔：每根到 t+k 的距离必须正好 k*interval
  8. 末尾不足 k 根的必须留空，不许拿已走过的凑

用法：
    python tools/probe_kbar_forward.py
    python tools/probe_kbar_forward.py --kbar-db data/kbar.db
"""
from __future__ import annotations

import argparse
import collections
import os
import re
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


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--kbar-db", default="data/kbar.db")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--interval", type=int, default=900_000)
    ap.add_argument("--held-bars", type=int, default=3)
    ap.add_argument("--tol-pct", type=float, default=0.0)
    ap.add_argument("--max-report", type=int, default=6)
    a = ap.parse_args(argv)

    if not os.path.exists(a.kbar_db):
        raise SystemExit("找不到 %s" % a.kbar_db)
    con = sqlite3.connect("file:%s?mode=ro"
                          % os.path.abspath(a.kbar_db).replace("\\", "/"), uri=True)
    sym, iv = a.symbol, a.interval
    k_bars = a.held_bars

    # ---------------------------------------------------------------- 0 规模
    hr("0. 表 3 的规模")
    try:
        n_fw = con.execute("select count(*) from kbar_forward where symbol=? "
                           "and interval_ms=?", (sym, iv)).fetchone()[0]
    except sqlite3.OperationalError as e:
        raise SystemExit("读不到 kbar_forward：%s（先跑 tools/backfill_kbar_forward.py）" % e)
    n_facts = con.execute("select count(*) from kbar_evolution where symbol=? "
                          "and interval_ms=?", (sym, iv)).fetchone()[0]
    print("  表 3 行数 = %d" % n_fw)
    print("  表 1 根数 = %d" % n_facts)
    if n_fw != n_facts:
        bad("表 3 行数（%d）≠ 表 1 根数（%d）—— 每根 K 线必须都有一行" % (n_fw, n_facts))
    else:
        ok("每根 K 线都有且只有一行")

    # ---------------------------------------------------------------- 1 独立性
    hr("1. 独立性红线：当下判定路径不许 import 事后层")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    kbar_dir = os.path.join(root, "ai_trader", "kbar")
    judgement = ("lines.py", "facts.py", "reaction.py", "event.py", "build.py")
    bad_files = []
    for fn in judgement:
        p = os.path.join(kbar_dir, fn)
        if not os.path.exists(p):
            continue
        src = open(p, encoding="utf-8").read()
        # build.py 允许定义第 5 步的入口（它必须能落库），但不许在 1~4 步的函数里调用它
        for m in re.finditer(r"^\s*(?:from\s+\.\s*forward|from\s+ai_trader\.kbar\.forward"
                             r"|import\s+ai_trader\.kbar\.forward)", src, re.M):
            line_no = src[:m.start()].count("\n") + 1
            bad_files.append("%s:%d" % (fn, line_no))
    if bad_files:
        bad(f"这些当下判定模块 import 了事后层：{bad_files}")
    else:
        ok("lines/facts/reaction/event 都没 import forward（红线未破）")

    # build.py 里第 1~4 步的函数不许调用 build_and_persist_forward
    bp = os.path.join(kbar_dir, "build.py")
    if os.path.exists(bp):
        src = open(bp, encoding="utf-8").read()
        calls = [m.start() for m in re.finditer(r"build_and_persist_forward\(", src)]
        # 定义处 1 次 + 若被别的函数调用则更多
        if len(calls) > 1:
            bad(f"build.py 里有 {len(calls) - 1} 处调用 build_and_persist_forward —— "
                "当下判定路径不许调事后层")
        else:
            ok("build.py 只定义、不调用事后层入口")

    # ---------------------------------------------------------------- 2 读真值
    hr("2. 从库里读出表 1 与表 3")
    bars = []
    for ts, o, h, l, c, ev, on_line, state in con.execute(
            "select ts,o,h,l,c,event,on_line,state from kbar_evolution "
            "where symbol=? and interval_ms=? order by ts", (sym, iv)):
        bars.append({"ts": int(ts), "o": o, "h": h, "l": l, "c": c,
                     "event": ev, "on_line": on_line, "state": state})
    print("  表 1 读出 %d 根" % len(bars))
    if not bars:
        raise SystemExit("表 1 是空的")
    ts_list = [b["ts"] for b in bars]
    if ts_list != sorted(ts_list):
        bad("表 1 不是按 ts 升序")

    # 等间隔（探针自己查，不看 build.py 的结论）
    gaps = collections.Counter()
    for i in range(1, len(bars)):
        gaps[ts_list[i] - ts_list[i - 1]] += 1
    print("  相邻间隔分布：%s" % dict(gaps))
    if list(gaps) != [iv]:
        bad(f"不是等间隔（期望只有 {iv}）：{dict(gaps)}")
    else:
        ok("全部等间隔 %d ms" % iv)

    idx = {ts: i for i, ts in enumerate(ts_list)}
    # 表 3 读成 {ts: row}
    fw = {}
    for r in con.execute(
            "select ts,ret1,ret3,ret5,held,held_line,held_ref_price,held_side "
            "from kbar_forward where symbol=? and interval_ms=? order by ts", (sym, iv)):
        fw[int(r[0])] = {"ret1": r[1], "ret3": r[2], "ret5": r[3], "held": r[4],
                         "held_line": r[5], "held_ref_price": r[6], "held_side": r[7]}
    print("  表 3 读出 %d 行" % len(fw))
    if set(fw) != set(ts_list):
        extra = set(fw) - set(ts_list)
        missing = set(ts_list) - set(fw)
        bad(f"表 3 与表 1 的根不一致：多 %d，少 %d" % (len(extra), len(missing)))

    # 参照价（探针自己从表 2.5 取，不用 held_ref_price 自证）
    ref = {}
    try:
        for ts, line_key, price in con.execute(
                "select ts,line_key,line_price from kbar_event "
                "where symbol=? and interval_ms=?", (sym, iv)):
            ref[(int(ts), str(line_key))] = None if price is None else float(price)
    except sqlite3.OperationalError:
        pass
    print("  表 2.5 参照价 %d 条" % len(ref))

    # ---------------------------------------------------------------- 3 ret 重算
    hr("3. ret_k：独立重算逐根比对")
    for k in (1, 3, 5):
        col = "ret%d" % k
        n_have = n_null = 0
        mismatch = []
        wrong_dir = 0
        off_by_one = 0
        for i, b in enumerate(bars):
            got = fw.get(b["ts"], {}).get(col)
            if i + k >= len(bars):
                n_null += 1
                if got is not None:
                    mismatch.append((b["ts"], "末尾不足 %d 根却给了 %r" % (k, got)))
                continue
            c0 = b["c"]
            ck = bars[i + k]["c"]
            if c0 is None or ck is None or float(c0) == 0.0:
                n_null += 1
                if got is not None:
                    mismatch.append((b["ts"], "价格缺失却给了 %r" % got))
                continue
            n_have += 1
            want = float(ck) / float(c0) - 1.0
            # 距离必须是正好 k 个 interval（防「取错根」）
            if bars[i + k]["ts"] - b["ts"] != k * iv:
                off_by_one += 1
            if got is None:
                mismatch.append((b["ts"], "该有值却空着（应为 %r）" % want))
            elif abs(float(got) - want) > 1e-9:
                mismatch.append((b["ts"], "库里 %r，重算 %r" % (got, want)))
            else:
                # 符号方向：库里除以重算必须 ≈ 1（防整体取反）
                pass
        print("  %s 有值 %6d / %6d (%5.1f%%)，空 %6d"
              % (col, n_have, len(bars), pct(n_have, len(bars)), n_null))
        if off_by_one:
            bad("%s 有 %d 根的 t+k 距离不等于 k*interval" % (col, off_by_one))
        if mismatch:
            bad("%s 有 %d 根与独立重算不一致" % (col, len(mismatch)))
            for ts, msg in mismatch[:a.max_report]:
                print("         ts=%s %s" % (ts, msg))
        else:
            ok("%s 全部与独立重算一致（%d 根）" % (col, n_have))

    # 方向体检：全体的中位数比值应为 1；若约等于 -1 说明符号整体反了
    ratios = []
    for i, b in enumerate(bars):
        got = fw.get(b["ts"], {}).get("ret1")
        if got is None or i + 1 >= len(bars):
            continue
        c0, c1 = b["c"], bars[i + 1]["c"]
        if not c0 or c1 is None:
            continue
        want = float(c1) / float(c0) - 1.0
        if abs(want) > 1e-12:
            ratios.append(float(got) / want)
    if ratios:
        ratios.sort()
        med = ratios[len(ratios) // 2]
        print("  ret1 与重算的比值中位数 = %.9f" % med)
        if abs(med - 1.0) > 1e-6:
            bad("ret1 的整体比值中位数不是 1（=%.6f）—— 取价方向可能整体反了" % med)
        else:
            ok("ret1 的方向与量级都对（比值中位数 = 1）")

    # ---------------------------------------------------------------- 4 held 重算
    hr("4. held：独立重算逐根比对")
    print("  独立实现：之后 %d 根收盘是否都在同一侧（参照价固定、容差 %.6f）"
          % (k_bars, a.tol_pct))

    def my_side(close, price):
        band = abs(float(price)) * a.tol_pct
        if float(close) > float(price) + band:
            return "above"
        if float(close) < float(price) - band:
            return "below"
        return "at"

    n1 = n0 = nnull = 0
    mismatch = []
    ref_missing = 0
    for i, b in enumerate(bars):
        row = fw.get(b["ts"], {})
        got = row.get("held")
        ev = b["event"]
        if ev is None:
            if got is not None:
                mismatch.append((b["ts"], "无事件的根却给了 held=%r" % got))
            continue
        key = (b["ts"], str(b["on_line"]))
        p = ref.get(key)
        if p is None:
            ref_missing += 1
            if got is not None:
                mismatch.append((b["ts"], "拿不到参照价却给了 held=%r" % got))
            continue
        # 方向：突破用事件自己的方向；反包用当根收盘所在侧
        if ev == "向上突破":
            side = "above"
        elif ev == "向下突破":
            side = "below"
        else:
            side = my_side(b["c"], p)
        if side == "at":
            if got is not None:
                mismatch.append((b["ts"], "当根压在线上（无方向）却给了 held=%r" % got))
            nnull += 1
            continue
        if i + k_bars >= len(bars):
            if got is not None:
                mismatch.append((b["ts"], "末尾不足 %d 根却给了 held=%r" % (k_bars, got)))
            nnull += 1
            continue
        want = 1
        for j in range(1, k_bars + 1):
            cj = bars[i + j]["c"]
            if cj is None:
                want = None
                break
            if my_side(cj, p) != side:
                want = 0
                break
        if want is None:
            nnull += 1
            if got is not None:
                mismatch.append((b["ts"], "未来价格缺失却给了 held=%r" % got))
            continue
        if want == 1:
            n1 += 1
        else:
            n0 += 1
        if got is None:
            mismatch.append((b["ts"], "该有值却空着（应为 %d）" % want))
        elif int(got) != want:
            mismatch.append((b["ts"], "库里 %r，重算 %d（side=%s ref=%s）"
                             % (got, want, side, p)))

    print("  重算得到：held=1 %6d  held=0 %6d  held 空 %6d（拿不到参照价 %d）"
          % (n1, n0, nnull, ref_missing))
    if mismatch:
        bad("held 有 %d 根与独立重算不一致" % len(mismatch))
        for ts, msg in mismatch[:a.max_report]:
            print("         ts=%s %s" % (ts, msg))
    else:
        ok("held 全部与独立重算一致")

    # 库里的 held 分布，与重算对比
    dist = collections.Counter(
        ("null" if r["held"] is None else int(r["held"])) for r in fw.values())
    print("  库里的分布：%s" % dict(dist))
    if dist.get(1, 0) != n1 or dist.get(0, 0) != n0:
        bad("库里的 held 分布（1=%d, 0=%d）与重算（1=%d, 0=%d）不一致"
            % (dist.get(1, 0), dist.get(0, 0), n1, n0))
    else:
        ok("库里 held 的 1/0 条数与重算一致")

    # ---------------------------------------------------------------- 5 参照价
    hr("5. held 的参照价与审计列")
    bad_ref = bad_line = bad_side = 0
    for ts, row in fw.items():
        if row["held"] is None:
            continue
        b = bars[idx[ts]]
        p = ref.get((ts, str(b["on_line"])))
        if p is not None and row["held_ref_price"] is not None:
            if abs(float(row["held_ref_price"]) - float(p)) > 1e-9:
                bad_ref += 1
        if row["held_line"] != b["on_line"]:
            bad_line += 1
        if row["held_side"] not in ("above", "below"):
            bad_side += 1
    if bad_ref:
        bad("%d 根的 held_ref_price 与表 2.5 的 line_price 不等" % bad_ref)
    else:
        ok("held_ref_price 全部等于表 2.5 同 (ts, on_line) 的 line_price")
    if bad_line:
        bad("%d 根的 held_line 与表 1 的 on_line 不等" % bad_line)
    else:
        ok("held_line 全部等于表 1 的 on_line")
    if bad_side:
        bad("%d 根的 held_side 不是 above/below" % bad_side)
    else:
        ok("held_side 取值合法")

    # ---------------------------------------------------------------- 6 无事件必须空
    hr("6. 无事件的根，held 必须为空")
    n_no_ev = sum(1 for b in bars if b["event"] is None)
    n_no_ev_held = sum(1 for b in bars if b["event"] is None
                       and fw.get(b["ts"], {}).get("held") is not None)
    print("  无事件的根 = %d，其中 held 非空的 = %d" % (n_no_ev, n_no_ev_held))
    if n_no_ev_held:
        bad("有 %d 根无事件却填了 held —— 等于说「它没守住一个不存在的线」"
            % n_no_ev_held)
    else:
        ok("无事件的根 held 全为空")

    # ---------------------------------------------------------------- 7 填充率
    hr("7. 填充率")
    for col in ("ret1", "ret3", "ret5", "held", "held_line", "held_ref_price"):
        n = con.execute("select count(%s) from kbar_forward where symbol=? "
                        "and interval_ms=?" % col, (sym, iv)).fetchone()[0]
        print("    %-16s %6d / %6d  (%5.1f%%)" % (col, n, n_fw, pct(n, n_fw)))

    con.close()
    print("\n" + "=" * 78)
    print("结论")
    print("=" * 78)
    if PROBLEMS:
        print("  发现 %d 个问题：" % len(PROBLEMS))
        for p in PROBLEMS:
            print("    - " + p)
        return 1
    print("  全部检查通过。上面的数都是探针从库里读出来、独立重算过的。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
