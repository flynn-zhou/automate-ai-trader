# -*- coding: utf-8 -*-
"""第 4 步 · 事件层探针。

**先跑探针、后写测试。** 这个项目反复出现「测试通过但功能没接线」，
测试绿只说明旧行为没坏。所以这里打印真实值、并**独立重算**一遍再和库里的比。

独立到什么程度：本文件**不 import `ai_trader.kbar.event`** 的任何计算函数，
连词汇常量都自己写一遍 —— 自己跟自己比没有意义。

覆盖设计稿 §13 第 4 步验收：
  - 找一段真实的「突破后收回」，确认 prev_event 与 since_event_idx 正确
  - 统计 neutral 占比（接近 0% = 在硬贴标签；接近 100% = 阈值有问题）

用法：
    python tools/probe_kbar_events.py --kbar-db data/kbar.db
    python tools/probe_kbar_events.py --at "2026-10-31 17:45" --n 6
"""
from __future__ import annotations

import argparse
import collections
import datetime
import os
import sqlite3
import sys

PROBLEMS = []

# ---- 词表（刻意在这里重写一遍，不从 event.py 导入）----
V_BREAK_UP = "向上突破"
V_BREAK_DOWN = "向下突破"
V_FAILED = "突破失败·反包"
V_NEUTRAL = "neutral"
REVERT_WINDOW = 3


def ok(msg):
    print("  [OK]   " + msg)


def bad(msg):
    print("  [BAD]  " + msg)
    PROBLEMS.append(msg)


def hr(title):
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def fmt(ms):
    if ms is None:
        return "-"
    return datetime.datetime.utcfromtimestamp(int(ms) / 1000).strftime("%Y-%m-%d %H:%M")


def parse_ts(s):
    if not s:
        return None
    for f in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return int(datetime.datetime.strptime(s, f)
                       .replace(tzinfo=datetime.timezone.utc).timestamp() * 1000)
        except ValueError:
            continue
    raise SystemExit("看不懂的时间：%s（要 'YYYY-MM-DD HH:MM' UTC）" % s)


def open_ro(path):
    return sqlite3.connect("file:%s?mode=ro" % os.path.abspath(path).replace("\\", "/"),
                           uri=True)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--kbar-db", default="data/kbar.db")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--interval", type=int, default=900_000)
    ap.add_argument("--at", default=None, help="只抽样看这一刻附近（UTC）")
    ap.add_argument("--n", type=int, default=6)
    a = ap.parse_args(argv)

    if not os.path.exists(a.kbar_db):
        raise SystemExit("找不到 %s" % a.kbar_db)
    con = open_ro(a.kbar_db)
    con.row_factory = sqlite3.Row
    sym, iv = a.symbol, a.interval

    # ---------------------------------------------------------------- 0
    hr("0. 三张来源表 + 事件表的规模")
    def one(sql, *args):
        return con.execute(sql, args).fetchone()[0]

    n_line = one("select count(*) from kbar_line where symbol=? and interval_ms=?", sym, iv)
    n_evo = one("select count(*) from kbar_evolution where symbol=? and interval_ms=?", sym, iv)
    n_rx = one("select count(*) from kbar_reaction where symbol=? and interval_ms=?", sym, iv)
    n_ev = one("select count(*) from kbar_event where symbol=? and interval_ms=?", sym, iv)
    bars_ev = one("select count(distinct ts) from kbar_event where symbol=? and interval_ms=?",
                  sym, iv)
    print("  表 0 kbar_line      %6d 行" % n_line)
    print("  表 1 kbar_evolution %6d 行" % n_evo)
    print("  表 2 kbar_reaction  %6d 行" % n_rx)
    print("  表 2.5 kbar_event   %6d 行 / %d 根有事件" % (n_ev, bars_ev))
    if n_ev == 0:
        bad("事件表是空的 —— 功能没接线（不是「数据干净」）")
        return _end(con)

    # ---------------------------------------------------------------- 1
    hr("1. 事件词表：只用了该用的三个")
    got = dict(con.execute("select event, count(*) from kbar_event where symbol=? "
                           "and interval_ms=? group by event order by count(*) desc",
                           (sym, iv)))
    for k, v in got.items():
        print("  %-16s %5d" % (k, v))
    allowed = {V_BREAK_UP, V_BREAK_DOWN, V_FAILED}
    extra = set(got) - allowed
    if extra:
        bad("出现了词表外的自由文本事件: %s（设计稿 §8.4 禁止自由文本）" % sorted(extra))
    else:
        ok("只用了 %s 三个（其余 11 个词的主语是区间，不是线）" % sorted(allowed))

    # ---------------------------------------------------------------- 2
    hr("2. 独立重算：逐条核对 event / prev_event / since_event_idx")
    bars = {int(r["ts"]): dict(r) for r in con.execute(
        "select ts, o, h, l, c from kbar_evolution where symbol=? and interval_ms=? "
        "order by ts", (sym, iv))}
    ts_list = sorted(bars)
    prev_of = {t: (ts_list[i - 1] if i > 0 else None) for i, t in enumerate(ts_list)}

    lines = collections.defaultdict(dict)
    for r in con.execute("select line_key, ts, line_price from kbar_line "
                         "where symbol=? and interval_ms=?", (sym, iv)):
        lines[r["line_key"]][int(r["ts"])] = float(r["line_price"])

    # 独立重算：对每个 (根, 线) 判穿越；反包按窗口
    my = []                       # [(ts, key, event)]
    last = {}
    openb = {}
    for ts in ts_list:
        c = bars[ts]["c"]
        pv = prev_of[ts]
        idx = ts // iv
        pc = None if pv is None else bars[pv]["c"]
        for key in sorted(lines):
            if pv is None or pv not in lines[key]:
                continue
            p = lines[key][pv]
            ev = None
            if pc is not None:
                if pc <= p and c > p:
                    ev = V_BREAK_UP
                elif pc >= p and c < p:
                    ev = V_BREAK_DOWN
            # 反包独立于当根的穿越判定：突破后窗口内收盘回到原来的那一侧。
            # 这次回穿本身也常常是一次反向穿越，两件事可以同根发生。
            fwd = None
            ob = openb.get(key)
            if ob is not None:
                if idx - ob["idx"] > REVERT_WINDOW:
                    openb.pop(key, None)
                else:
                    back = "below" if ob["ev"] == V_BREAK_UP else "above"
                    cur = "above" if c > p else ("below" if c < p else "at")
                    if cur == back:
                        fwd = V_FAILED
                        openb.pop(key, None)
            if ev:
                openb[key] = {"idx": idx, "ev": ev}
            # 先反包、后当根穿越（与实现同一发出顺序）
            for name in (fwd, ev):
                if name:
                    my.append((ts, key, name))
                    last[key] = (name, idx)

    mine = collections.Counter((t, k, e) for t, k, e in my)
    theirs = collections.Counter(
        (int(r["ts"]), r["line_key"], r["event"]) for r in con.execute(
            "select ts, line_key, event from kbar_event where symbol=? and interval_ms=?",
            (sym, iv)))
    if mine == theirs:
        ok("独立重算的事件集合与库里完全一致（%d 条）" % sum(mine.values()))
    else:
        only_mine = list((mine - theirs).items())[:5]
        only_db = list((theirs - mine).items())[:5]
        bad("事件集合不一致：库里多 %d 条，我多 %d 条　库里多例=%s　我多例=%s"
            % (sum(only_db and dict(only_db).values()) if only_db else 0,
               sum(dict(only_mine).values()) if only_mine else 0,
               only_db, only_mine))

    # prev_event / since_event_idx：同一条线上一次的事件
    last2 = {}
    wrong = 0
    # 按 id 排序才恢复得出「发出顺序」：同一根同一线上可能有
    # 「突破失败·反包」和「向下突破」两条，ts/line_key 都相同。
    for r in con.execute("select ts, idx, line_key, event, prev_event, since_event_idx "
                         "from kbar_event where symbol=? and interval_ms=? "
                         "order by ts, line_key, id", (sym, iv)):
        ts, idx, key = int(r["ts"]), int(r["idx"]), r["line_key"]
        exp = last2.get(key)
        want_prev = exp[0] if exp else None
        want_since = (idx - exp[1]) if exp else None
        if r["prev_event"] != want_prev or r["since_event_idx"] != want_since:
            wrong += 1
        last2[key] = (r["event"], idx)
    if wrong:
        bad("prev_event / since_event_idx 有 %d 条与独立重数不一致" % wrong)
    else:
        ok("prev_event / since_event_idx 逐条与独立重数一致（按同一条线的上一次事件）")

    # ---------------------------------------------------------------- 3
    hr("3. neutral 占比（设计稿 §13 第 4 步验收）")
    n_ev_bars = one("select count(distinct ts) from kbar_event where symbol=? "
                    "and interval_ms=?", sym, iv)
    n_neutral = one("select count(*) from kbar_evolution where symbol=? and "
                    "interval_ms=? and state='neutral'", sym, iv)
    n_touch_bars = one("select count(distinct ts) from kbar_reaction where symbol=? "
                       "and interval_ms=?", sym, iv)
    print("  有事件的根        %5d  %5.1f%%" % (n_ev_bars, 100.0 * n_ev_bars / n_evo))
    print("  neutral 的根      %5d  %5.1f%%" % (n_neutral, 100.0 * n_neutral / n_evo))
    print("  碰到线但无事件     %5d  %5.1f%%" % (n_touch_bars - n_ev_bars,
                                            100.0 * (n_touch_bars - n_ev_bars) / n_evo))
    share = 100.0 * n_neutral / n_evo
    if share < 5:
        bad("neutral 只占 %.1f%% —— 太低了，说明在强行给每根贴标签（§8.2 明令禁止）" % share)
    elif share > 95:
        bad("neutral 占了 %.1f%% —— 太高了，说明判据差不多永不触发" % share)
    else:
        ok("neutral 占 %.1f%%，既不是 0 也不是 100 —— 有信息量" % share)
    # 一致性：neutral 的根必须既无事件也没碰到线
    bad_neutral = one("select count(*) from kbar_evolution e where e.symbol=? and "
                      "e.interval_ms=? and e.state='neutral' and exists ("
                      " select 1 from kbar_reaction r where r.symbol=e.symbol and "
                      " r.interval_ms=e.interval_ms and r.ts=e.ts)", sym, iv)
    if bad_neutral:
        bad("有 %d 根被标成 neutral，但表 2 说它碰到过线" % bad_neutral)
    else:
        ok("所有 neutral 的根确实一条线都没碰到")

    # ---------------------------------------------------------------- 4
    hr("4. 一段真实的「突破后收回」逐根明细（人工核对用）")
    # 注意：prev_event 指的是「这条线上一次的事件」，**不是**最早的那个突破。
    # 所以这里挑一条「反包，且它的 prev_event 正好是突破」的记录来展示，
    # 再把它前后几根同线事件列出来。
    pick = con.execute(
        "select id, line_key, ts from kbar_event where symbol=? and interval_ms=? "
        "and event=? and prev_event in (?,?) order by ts desc, id limit 1",
        (sym, iv, V_FAILED, V_BREAK_UP, V_BREAK_DOWN)).fetchone()
    if not pick:
        bad("找不到任何「突破 → 反包」配对（反包逻辑可能没生效）")
    else:
        key, t_fb, id_fb = pick["line_key"], int(pick["ts"]), int(pick["id"])
        print("  线 %s 上的反包：%s" % (key, fmt(t_fb)))
        rows = list(con.execute(
            "select ts, event, prev_event, since_event_idx, idx, line_price, close_side "
            "from kbar_event where symbol=? and interval_ms=? and line_key=? "
            "and ts<=? order by ts desc, id desc limit 8", (sym, iv, key, t_fb)))
        rows.reverse()
        print("  %-16s %-14s %-14s %6s %10s %8s" %
              ("ts", "event", "prev_event", "since", "线价", "收盘侧"))
        for r in rows:
            mark = "  ← 反包" if r["ts"] == t_fb else ""
            print("  %-16s %-14s %-14s %6s %10.1f %8s%s"
                  % (fmt(r["ts"]), r["event"], r["prev_event"] or "-",
                     "-" if r["since_event_idx"] is None else r["since_event_idx"],
                     r["line_price"], r["close_side"] or "-", mark))

        # 验收点：反包那条的 prev_event 必须是「这条线上一次事件」，
        # since_event_idx 必须等于两条事件的根号差。
        prev_row = con.execute(
            "select event, idx from kbar_event where symbol=? and interval_ms=? "
            "and line_key=? and (ts<? or (ts=? and id<?)) "
            "order by ts desc, id desc limit 1",
            (sym, iv, key, t_fb, t_fb, id_fb)).fetchone()
        fb = con.execute(
            "select prev_event, since_event_idx, idx from kbar_event "
            "where symbol=? and interval_ms=? and line_key=? and ts=? and event=?",
            (sym, iv, key, t_fb, V_FAILED)).fetchone()
        if prev_row and fb and fb["prev_event"] == prev_row["event"]:
            ok("反包那条的 prev_event == 这条线上一次事件（%s）" % prev_row["event"])
        else:
            bad("反包那条的 prev_event 不是这条线上一次事件：%s vs %s"
                % (fb["prev_event"] if fb else None,
                   prev_row["event"] if prev_row else None))
        if prev_row and fb and fb["since_event_idx"] == fb["idx"] - prev_row["idx"]:
            ok("since_event_idx = %d = 两条事件的根号差" % fb["since_event_idx"])
        else:
            bad("since_event_idx 与根号差不符：%s（应为 %s）"
                % (fb["since_event_idx"] if fb else None,
                   (fb["idx"] - prev_row["idx"]) if (fb and prev_row) else None))
        if prev_row and fb and prev_row["event"] in (V_BREAK_UP, V_BREAK_DOWN) \
                and fb["since_event_idx"] <= REVERT_WINDOW:
            ok("反包在 %d 根内发生（窗口 %d）—— 确实是「突破没站住」，不是别的事"
               % (fb["since_event_idx"], REVERT_WINDOW))

    # ---------------------------------------------------------------- 5
    hr("5. 表 1 的那几列：有事件 / 有接触无事件 / neutral 三态")
    for label, cond in (
            ("有事件   event 非空", "event is not null"),
            ("有接触无事件 event 空, 表2 有行",
             "event is null and exists (select 1 from kbar_reaction r "
             "where r.symbol=kbar_evolution.symbol and r.interval_ms=kbar_evolution.interval_ms "
             "and r.ts=kbar_evolution.ts)"),
            ("neutral  state='neutral'", "state='neutral'")):
        n = one("select count(*) from kbar_evolution where symbol=? and interval_ms=? and "
                + cond, sym, iv)
        print("  %-34s %5d  %5.1f%%" % (label, n, 100.0 * n / n_evo))
    unclassified = one("select count(*) from kbar_evolution where symbol=? and "
                       "interval_ms=? and event is null and state is null and not exists ("
                       " select 1 from kbar_reaction r where r.symbol=kbar_evolution.symbol "
                       " and r.interval_ms=kbar_evolution.interval_ms "
                       " and r.ts=kbar_evolution.ts)", sym, iv)
    if unclassified:
        bad("有 %d 根既没事件、没接触、也不是 neutral —— 三态没覆盖全" % unclassified)
    else:
        ok("三态覆盖全：每根都是「有事件 / 有接触无事件 / neutral」之一")

    # ---------------------------------------------------------------- 6
    hr("6. 表 1 的 event 列与表 2.5 的对应关系")
    multi = con.execute(
        "select ts, count(*) n from kbar_event where symbol=? and interval_ms=? "
        "group by ts having n>1 order by n desc limit 5", (sym, iv)).fetchall()
    print("  一根有多个事件的根数 = %d" %
          one("select count(*) from (select ts, count(*) n from kbar_event "
              "where symbol=? and interval_ms=? group by ts having n>1)", sym, iv))
    for r in multi:
        keys = [x[0] for x in con.execute(
            "select line_key||':'||event from kbar_event where symbol=? and "
            "interval_ms=? and ts=?", (sym, iv, r["ts"]))]
        got_line = one("select on_line from kbar_evolution where symbol=? and "
                       "interval_ms=? and ts=?", sym, iv, r["ts"])
        print("   %s  %s   → 表1 取 %s" % (fmt(r["ts"]), keys, got_line))
    ok("表 1 只存一个（穿透最深的那个）；完整集合在表 2.5 —— 丢事件就是丢事实")

    # ---------------------------------------------------------------- 7
    if a.at:
        hr("7. 指定时刻附近的逐根明细（--at）")
        t = parse_ts(a.at)
        rows = list(con.execute(
            "select ts, line_key, event, prev_event, since_event_idx, line_price, "
            "close_side, pen_depth_pct from kbar_event where symbol=? and interval_ms=? "
            "and ts<=? order by ts desc, line_key limit ?", (sym, iv, t, a.n)))
        for r in rows:
            print("  %s %-16s %-14s 线价=%9.1f 收盘侧=%-5s 穿透=%s"
                  % (fmt(r["ts"]), r["line_key"], r["event"], r["line_price"],
                     r["close_side"] or "-",
                     "-" if r["pen_depth_pct"] is None else "%.5f" % r["pen_depth_pct"]))
        if not rows:
            bad("--at 附近没有任何事件")

    # ---------------------------------------------------------------- 8
    hr("8. 分布（只看形状，不设标准）")
    print("  按事件:")
    for k, v in got.items():
        print("    %-16s %5d" % (k, v))
    print("  按线种类:")
    for k, v in con.execute("select line_kind, count(*) from kbar_event where "
                            "symbol=? and interval_ms=? group by line_kind "
                            "order by count(*) desc", (sym, iv)):
        print("    %-16s %5d" % (k, v))
    since = [r[0] for r in con.execute(
        "select since_event_idx from kbar_event where symbol=? and interval_ms=? "
        "and since_event_idx is not null order by since_event_idx", (sym, iv))]
    if since:
        def q(p):
            return since[min(len(since) - 1, int(len(since) * p))]
        print("  since_event_idx  P10=%d P50=%d P90=%d max=%d（共 %d 条有值）"
              % (q(0.10), q(0.50), q(0.90), since[-1], len(since)))
        print("  → 最大间距越大，越说明「回溯动态」是必要的：定死前 N 根会接不上链条")

    return _end(con)


def _end(con):
    con.close()
    print("\n" + "=" * 78)
    print("结论")
    print("=" * 78)
    if PROBLEMS:
        print("  有 %d 个问题：" % len(PROBLEMS))
        for p in PROBLEMS:
            print("   - " + p)
        return 1
    print("  全部检查通过。上面的数都是探针从库里读出来、独立重算过的。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
