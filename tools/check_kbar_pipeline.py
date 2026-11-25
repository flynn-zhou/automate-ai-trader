# -*- coding: utf-8 -*-
"""K 线演化 · 流水线体检（第 1~4 步）。

为什么需要它：四步是**链式依赖**的，而且断链时**不报错**。

   第 1 步 表 0 ──► 第 2 步 表 1 ──► 第 3 步 表 2 ──► 第 4 步 表 2.5
                        │                  │
                        └── 表 1 的 5 列 ◄──┘（第 4 步回填，读表 2）

已知会静默断链的两条路：
  1. 第 2 步 `--rebuild` 会 delete_evolution 整表，把第 3 步的 n_touch 和
     第 4 步的 5 列一起删掉；表 1 仍然有 O/H/L/C，看起来「有数」。
  2. 第 3 步重跑了但第 4 步没重跑 → 表 1 的 state=neutral 是按旧表 2 判的。

本工具只读，不修。发现不一致就打印「该重跑哪一步」。

用法：
    python tools/check_kbar_pipeline.py
    python tools/check_kbar_pipeline.py --kbar-db data/kbar.db
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys

PROBLEMS = []
WARNS = []


def bad(msg):
    print("  [BAD]  " + msg)
    PROBLEMS.append(msg)


def warn(msg):
    print("  [WARN] " + msg)
    WARNS.append(msg)


def ok(msg):
    print("  [OK]   " + msg)


def period_label(ms):
    """周期 -> line_key 后缀（"5m" / "15m" / "1h"）。

    **权威只有 ai_trader.kbar.lines.period_label 一处**，这里不复制它的规则：
    复制出来的第二份迟早会漏掉新周期，而漏掉的表现是「键拼错 → 报某项没跑过」，
    看起来像数据问题，其实是工具问题（5m 第一次跑就撞上了这个）。
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        from ai_trader.kbar.lines import period_label as _pl
        return _pl(int(ms))
    except Exception as exc:                            # noqa: BLE001
        bad("读不了 lines.period_label（%s），周期键退化成数字，"
            "下面关于 meta 的结论不可信" % exc)
        return str(int(ms))


def hr(t):
    print("\n" + "=" * 76)
    print(t)
    print("=" * 76)


def one(con, sql, *a):
    return con.execute(sql, a).fetchone()[0]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--kbar-db", default="data/kbar.db")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--interval", type=int, default=900_000)
    a = ap.parse_args(argv)

    if not os.path.exists(a.kbar_db):
        raise SystemExit("找不到 %s" % a.kbar_db)
    con = sqlite3.connect("file:%s?mode=ro"
                          % os.path.abspath(a.kbar_db).replace("\\", "/"), uri=True)
    sym, iv = a.symbol, a.interval

    # ---------------------------------------------------------------- 1 规模
    hr("1. 四张表的规模")
    sizes = {}
    for t in ("kbar_line", "kbar_evolution", "kbar_reaction", "kbar_event",
              "kbar_forward"):
        sizes[t] = one(con, "select count(*) from %s where symbol=? and interval_ms=?"
                       % t, sym, iv)
        print("  %-16s %7d" % (t, sizes[t]))
    if not sizes["kbar_line"]:
        bad("表 0 是空的 → 先跑 tools/backfill_kbar_lines.py")
    if not sizes["kbar_evolution"]:
        bad("表 1 是空的 → 先跑 tools/backfill_kbar_facts.py")
    if sizes["kbar_line"] and not sizes["kbar_reaction"]:
        bad("表 2 是空的 → 先跑 tools/backfill_kbar_reactions.py")
    if sizes["kbar_reaction"] and not sizes["kbar_event"]:
        bad("表 2.5 是空的 → 先跑 tools/backfill_kbar_events.py")
    if sizes["kbar_evolution"] and not sizes["kbar_forward"]:
        warn("表 3（事后层）是空的 → 第 5 步还没回填："
             "python tools/backfill_kbar_forward.py")

    # ---------------------------------------------------------------- 2 断链
    hr("2. 断链检查：第 2 步 rebuild 之后，第 3/4 步的列是不是被删了")
    n_bars = sizes["kbar_evolution"]
    if n_bars:
        n_nt = one(con, "select count(*) from kbar_evolution where symbol=? "
                        "and interval_ms=? and n_touch is not null", sym, iv)
        print("  n_touch 非空  %6d / %6d (%.1f%%)"
              % (n_nt, n_bars, 100.0 * n_nt / n_bars))
        if n_nt < n_bars:
            bad("n_touch 有 %d 根是空的 → 第 3 步没在这批行上跑过。"
                "重跑 tools/backfill_kbar_reactions.py，再重跑第 4 步"
                % (n_bars - n_nt))
        else:
            ok("n_touch 全填（第 3 步跑过）")

        n_ev = one(con, "select count(*) from kbar_evolution where symbol=? "
                        "and interval_ms=? and (event is not null or state is not null)",
                   sym, iv)
        print("  事件列非空     %6d / %6d" % (n_ev, n_bars))
        if n_ev == 0 and sizes["kbar_event"]:
            bad("表 2.5 有 %d 条事件，但表 1 那五列一个都没填 → 第 4 步的"
                " set_bar_events 没跑（或跑在删除之前）" % sizes["kbar_event"])

    # ---------------------------------------------------------------- 3 n_touch vs 表2
    hr("3. 表 1 的 n_touch 与表 2 是否对得上")
    if n_bars and sizes["kbar_reaction"]:
        n_pos = one(con, "select count(*) from kbar_evolution where symbol=? and "
                         "interval_ms=? and n_touch > 0", sym, iv)
        n_rx_bars = one(con, "select count(distinct ts) from kbar_reaction "
                             "where symbol=? and interval_ms=?", sym, iv)
        print("  表1 n_touch>0 的根 = %d   表2 覆盖的根 = %d" % (n_pos, n_rx_bars))
        if n_pos != n_rx_bars:
            bad("对不上 → 表 2 换过而 n_touch 没跟着更新（或反过来）。"
                "第 3 步整段重跑一次，再重跑第 4 步")
        else:
            ok("一致")
        # 逐根核对（不只比总数：两边各错一根会互相抵消）
        diff = one(con, """
            select count(*) from (
              select ts, count(*) n from kbar_reaction
               where symbol=? and interval_ms=? group by ts
            ) r join kbar_evolution v
              on v.symbol=? and v.interval_ms=? and v.ts=r.ts
            where v.n_touch is null or v.n_touch != r.n
        """, sym, iv, sym, iv)
        if diff:
            bad("有 %d 根的 n_touch 与表 2 的实际行数不等（总数相同但分布不同）" % diff)
        else:
            ok("逐根的 n_touch 与表 2 命中数逐一相等")

    # ---------------------------------------------------------------- 4 事件三态
    hr("4. 事件层三态：每根必须落在「有事件 / 有接触无事件 / neutral」之一")
    if sizes["kbar_event"] and n_bars:
        n_ev = one(con, "select count(*) from kbar_evolution where symbol=? and "
                        "interval_ms=? and event is not null", sym, iv)
        n_neu = one(con, "select count(*) from kbar_evolution where symbol=? and "
                         "interval_ms=? and state='neutral'", sym, iv)
        n_touch_only = one(con, """
            select count(*) from kbar_evolution v where v.symbol=? and v.interval_ms=?
              and v.event is null and v.state is null
              and exists (select 1 from kbar_reaction r where r.symbol=v.symbol
                          and r.interval_ms=v.interval_ms and r.ts=v.ts)
        """, sym, iv)
        n_gap = n_bars - n_ev - n_neu - n_touch_only
        print("  有事件          %6d  %5.1f%%" % (n_ev, 100.0 * n_ev / n_bars))
        print("  有接触无事件    %6d  %5.1f%%" % (n_touch_only, 100.0 * n_touch_only / n_bars))
        print("  neutral         %6d  %5.1f%%" % (n_neu, 100.0 * n_neu / n_bars))
        if n_gap:
            bad("有 %d 根不属于上面任何一态（三态没覆盖全）" % n_gap)
        else:
            ok("三态覆盖全，无漏网")
        # neutral 不许碰到线
        d = one(con, """
            select count(*) from kbar_evolution v where v.symbol=? and v.interval_ms=?
              and v.state='neutral' and exists (select 1 from kbar_reaction r
                where r.symbol=v.symbol and r.interval_ms=v.interval_ms and r.ts=v.ts)
        """, sym, iv)
        if d:
            bad("有 %d 根被标成 neutral 但表 2 说它碰到过线" % d)
        else:
            ok("neutral 的根确实一条线都没碰到")

    # ---------------------------------------------------------------- 5 悬空引用
    hr("5. 悬空引用：事件/on_line 指向的东西是否真的存在")
    if sizes["kbar_event"]:
        d = one(con, """
            select count(*) from kbar_event e left join kbar_evolution v
              on v.symbol=e.symbol and v.interval_ms=e.interval_ms and v.ts=e.ts
            where v.ts is null
        """)
        if d:
            bad("有 %d 条事件找不到所属的 K 线（表 1）" % d)
        else:
            ok("每条事件都有对应的 K 线")
        d = one(con, """
            select count(*) from kbar_evolution v where v.on_line is not null
              and not exists (select 1 from kbar_event e where e.symbol=v.symbol
                and e.interval_ms=v.interval_ms and e.ts=v.ts and e.line_key=v.on_line)
        """)
        if d:
            bad("有 %d 根的表 1 on_line 在表 2.5 里找不到对应事件" % d)
        else:
            ok("表 1 的 on_line 都能在表 2.5 找到")
        # 表1.on_line 在表 2 找不到是**允许**的（跳空穿越），但要报出来
        d = one(con, """
            select count(*) from kbar_evolution v where v.on_line is not null
              and not exists (select 1 from kbar_reaction r where r.symbol=v.symbol
                and r.interval_ms=v.interval_ms and r.ts=v.ts and r.line_key=v.on_line)
        """)
        n_ol = one(con, "select count(*) from kbar_evolution where symbol=? and "
                        "interval_ms=? and on_line is not null", sym, iv)
        if d:
            print("  [注]  表1.on_line 在表 2 无明细的根 = %d / %d (%.2f%%)"
                  % (d, n_ol, 100.0 * d / n_ol))
            print("        这是**允许**的（跳空穿越时本根没碰到线）。下游取"
                  " side/pen/recover 必须 LEFT JOIN，不能 INNER JOIN")

    # ---------------------------------------------------------------- 6 meta 一致
    hr("6. kbar_meta 的自述与代码是否一致")


    def meta(key):
        r = con.execute("select value from kbar_meta where key=?", (key,)).fetchone()
        return json.loads(r[0]) if r else None

    # ⚠ 曾经是 {900000: "15m", 3600000: "1h"}.get(iv, str(iv)) —— 写死的映射。
    #    凡是表里没有的周期（比如 5m）都会退成 str(iv)，拼出
    #    `reaction_params:BTCUSDT:300000` 这种不存在的键，于是自检报
    #    「没有 reaction_params —— 第 3 步没跑过」，而 meta 其实好好写着
    #    `reaction_params:BTCUSDT:5m`。结论是对的（报红），原因是错的，
    #    而且这个自检永远验不了任何新周期。一律走 period_label 这个唯一权威。
    lab = period_label(iv)
    rp = meta("reaction_params:%s:%s" % (sym, lab))
    ep = meta("event_params:%s:%s" % (sym, lab))
    if not rp:
        bad("没有 reaction_params —— 第 3 步没跑过或没写 meta")
    else:
        rule = rp.get("line_value_rule") or ""
        if "pick_prev" in rule or "上一根" in rule:
            ok("reaction_params.line_value_rule 说的是「上一根那一行」（与代码一致）")
        else:
            bad("reaction_params.line_value_rule 不是 pick_prev 的说法，"
                "库在自述一个已废弃的取法 → 重跑第 3 步刷新 meta。当前值：%s" % rule)
    if not ep:
        bad("没有 event_params —— 第 4 步没跑过或没写 meta")
    else:
        if ep.get("event_reaction_contract"):
            ok("event_params 声明了「事件不保证有反应明细」的契约")
        else:
            bad("event_params 没声明事件↔反应的契约 → 重跑第 4 步")
        if ep.get("revert_window_bars") is not None:
            print("  [注]  反包窗口 = %s 根（口径来源与是否适用 15m 见 D-16）"
                  % ep["revert_window_bars"])

    # ---------------------------------------------------------------- 7 指纹新鲜度
    hr("7. 输入指纹：第 4 步算完之后，它的输入有没有变过")
    fp = meta("event_input_fingerprint:%s:%s" % (sym, lab))
    if not fp:
        warn("没有 event_input_fingerprint → 无法判断第 4 步是不是在旧输入上算的")
    else:
        now = {
            "line_rows": one(con, "select count(*) from kbar_line where symbol=? "
                                  "and interval_ms=?", sym, iv),
            "evolution_bars": one(con, "select count(*) from kbar_evolution where "
                                       "symbol=? and interval_ms=?", sym, iv),
            "reaction_rows": one(con, "select count(*) from kbar_reaction where "
                                      "symbol=? and interval_ms=?", sym, iv),
            "reaction_bars": one(con, "select count(distinct ts) from kbar_reaction "
                                      "where symbol=? and interval_ms=?", sym, iv),
            "event_rows": one(con, "select count(*) from kbar_event where symbol=? "
                                   "and interval_ms=?", sym, iv),
        }
        drift = {k: (fp.get(k), now[k]) for k in now if fp.get(k) != now[k]}
        if drift:
            for k, (was, is_) in drift.items():
                bad("输入变了：%s 记录为 %s，现在是 %s" % (k, was, is_)
                    + " → 重跑 tools/backfill_kbar_events.py")
        else:
            ok("五项输入指纹全部未变（事件层是在当前输入上算出来的）")

    # ---------------------------------------------------------------- 7.5 事后层
    hr("7.5 表 3（事后层）：隔离、完整性、算术")
    if sizes["kbar_forward"]:
        # 粒度：每根 K 线一行
        if sizes["kbar_forward"] != n_bars:
            bad("表 3 有 %d 行，表 1 有 %d 根 —— 每根必须恰好一行"
                % (sizes["kbar_forward"], n_bars))
        else:
            ok("表 3 每根 K 线恰好一行")

        # held 只能落在有事件的根上
        d = one(con, """
            select count(*) from kbar_forward f join kbar_evolution v
              on v.symbol=f.symbol and v.interval_ms=f.interval_ms and v.ts=f.ts
            where f.held is not null and v.event is null
        """)
        if d:
            bad("有 %d 根无事件却填了 held（等于说它没守住一个不存在的线）" % d)
        else:
            ok("held 只落在有事件的根上")

        # ret 的尾部必须留空（结构上算不出来）
        for k in (1, 3, 5):
            n_have = one(con, "select count(ret%d) from kbar_forward where symbol=? "
                              "and interval_ms=?" % k, sym, iv)
            print("    ret%-2d 有值 %6d / %6d" % (k, n_have, sizes["kbar_forward"]))
            if n_have > sizes["kbar_forward"] - k:
                bad("ret%d 有值的行数比「全部根 - %d」还多 —— 尾部不该有值" % (k, k))

        # held 的三态必须齐（1/0/空三者解释得通）
        dist = {}
        for h, c in con.execute("select held, count(*) from kbar_forward "
                                "where symbol=? and interval_ms=? group by held",
                                (sym, iv)):
            dist["null" if h is None else int(h)] = int(c)
        print("    held 分布：%s" % dist)
        n_ev_bars = one(con, "select count(*) from kbar_evolution where symbol=? "
                             "and interval_ms=? and event is not null", sym, iv)
        got = dist.get(1, 0) + dist.get(0, 0) + dist.get("null", 0)
        if got != sizes["kbar_forward"]:
            bad("held 三态合计 %d ≠ 表 3 行数 %d" % (got, sizes["kbar_forward"]))
        elif dist.get(1, 0) + dist.get(0, 0) > n_ev_bars:
            bad("held 有值的条数（%d）比有事件的根（%d）还多"
                % (dist.get(1, 0) + dist.get(0, 0), n_ev_bars))

        # 隔离：当下判定路径不许 import 事后层
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        kdir = os.path.join(root, "ai_trader", "kbar")
        pats = (r"^\s*from\s+\.+\s*forward\b",
                r"^\s*from\s+\.+\s+import\s+forward\b",
                r"^\s*from\s+ai_trader\.kbar\.forward\b",
                r"^\s*from\s+ai_trader\.kbar\s+import\s+[^\n]*\bforward\b",
                r"^\s*import\s+ai_trader\.kbar\.forward\b")
        off = []
        for fn in ("lines.py", "facts.py", "reaction.py", "event.py", "shape.py"):
            p2 = os.path.join(kdir, fn)
            if not os.path.exists(p2):
                continue
            src = open(p2, encoding="utf-8").read()
            if any(re.search(pt, src, re.M) for pt in pats):
                off.append(fn)
        if off:
            bad("当下判定路径 import 了事后层（回测会偷未来）：%s" % off)
        else:
            ok("当下判定路径没 import 事后层（红线未破）")

        # meta 自述
        fp2 = meta("forward_params:%s:%s" % (sym, lab))
        if not fp2:
            bad("没有 forward_params → 第 5 步没跑过或没写 meta")
        else:
            if fp2.get("isolation"):
                ok("forward_params 声明了事后层的隔离约束")
            else:
                bad("forward_params 没声明隔离约束 → 重跑第 5 步")
            if "D-12" in (fp2.get("ret_fee_note") or ""):
                print("  [注]  扣费口径仍是占位（D-12）：ret_fee=%s" % fp2.get("ret_fee"))
            if "D-20" in (fp2.get("held_ref_price_rule") or ""):
                print("  [注]  参照价口径仍是占位（D-20）：用固定线价")

    # ---------------------------------------------------------------- 7.6 读数层
    hr("7.6 读数层（shape.py + 表 5）：判断落了库，但必须可核")
    kdir2 = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    kdir2 = os.path.join(kdir2, "ai_trader", "kbar")
    pats2 = (r"^\s*from\s+\.+\s*forward\b",
             r"^\s*from\s+\.+\s+import\s+forward\b",
             r"^\s*from\s+ai_trader\.kbar\.forward\b",
             r"^\s*from\s+ai_trader\.kbar\s+import\s+[^\n]*\bforward\b",
             r"^\s*import\s+ai_trader\.kbar\.forward\b")
    shape_py = os.path.join(kdir2, "shape.py")
    if not os.path.exists(shape_py):
        bad("没有 ai_trader/kbar/shape.py（读数层没落地）")
    else:
        ssrc = open(shape_py, encoding="utf-8").read()
        # 用户 2026-11-08 改口：「所有演化结论字段或者描述都要入 db 库」。
        # 原先这里反过来查「有没有 shape 表」，现在查「表在不在、对不对得上」。
        try:
            import importlib
            # 本文件原先只读库里/源码里的东西，不 import 项目模块，所以没设过
            # sys.path。这里要现算 n_bad_label 之类的口径（以及拿当前指纹），
            # 才需要真正 import 一次 shape。
            if kdir2 not in [os.path.dirname(os.path.dirname(kdir2))]:
                root2 = os.path.dirname(os.path.dirname(kdir2))
                if root2 not in sys.path:
                    sys.path.insert(0, root2)
            shp = importlib.import_module("ai_trader.kbar.shape")
        except Exception as exc:                       # noqa: BLE001
            shp = None
            bad("读不了 ai_trader.kbar.shape：%s" % exc)
        tabs = [r[0] for r in con.execute(
            "select name from sqlite_master where type='table'")]
        if "kbar_shape" not in tabs:
            bad("没有表 5 kbar_shape —— 判定没落库，先跑 "
                "tools/backfill_kbar_shape.py")
        elif shp is not None:
            cur_hash = shp.params_hash()
            n_shp, = con.execute(
                "select count(*) from kbar_shape where symbol=? and interval_ms=?",
                (sym, iv)).fetchone()
            # 表 1 的行数现查：`n_ev` 这个局部名在本文件里被复用过好几次，
            # 借它来比会得到一个看似合理、其实不是表 1 的数字。
            n_bars1, = con.execute(
                "select count(*) from kbar_evolution where symbol=? and interval_ms=? "
                "and o is not null", (sym, iv)).fetchone()
            if n_shp != n_bars1:
                bad("表 5 有 %d 行，表 1 有 %d 根 —— 不是一根一行"
                    % (n_shp, n_bars1))
            else:
                ok("表 5 与表 1 等长（%d 行，一根一行）" % n_shp)
            hashes = [h for (h,) in con.execute(
                "select distinct params_hash from kbar_shape "
                "where symbol=? and interval_ms=?", (sym, iv))]
            if hashes == [cur_hash]:
                ok("表 5 只有一套口径指纹，且等于当前 shape.py（%s）" % cur_hash)
            else:
                bad("表 5 的指纹 %s ≠ 当前 %s —— 改过阈值没重跑，"
                    "表里混着两套口径，必须 --rebuild" % (hashes, cur_hash))
            # 各结论词的实际行数，下面几处检查都要用，先算出来
            seen = dict(con.execute(
                "select label, count(*) from kbar_shape where symbol=? and interval_ms=? "
                "group by label", (sym, iv)))
            # label 必须能在 states 里找到，**或者**是「没有形态」这一族
            # （无 / 无·真空 / 无·贴线）—— 它们不是形态，所以 states 必须是空。
            # 这一族的词表以 shape.LABEL_NO_ALL 为准，不许在这里另抄一份。
            no_family = list(shp.LABEL_NO_ALL)
            ph_q = ",".join("?" * len(no_family))
            no_not_in = ("label not in (%s) and "
                         "instr(','||states||',', ','||label||',') = 0"
                         % ph_q)
            n_bad_label, = con.execute(
                "select count(*) from kbar_shape where symbol=? and interval_ms=? "
                "and " + no_not_in, (sym, iv) + tuple(no_family)).fetchone()
            if n_bad_label:
                bad("有 %d 行的 label 不在自己的 states 里（自相矛盾）" % n_bad_label)
            else:
                ok("每行 label 都在自己的 states 里（或属于「没有形态」这一族）")
            n_bad_none, = con.execute(
                "select count(*) from kbar_shape where symbol=? and interval_ms=? "
                "and label in (%s) and states <> ''" % ph_q,
                (sym, iv) + tuple(no_family)).fetchone()
            if n_bad_none:
                bad("有 %d 行 label 属于「没有形态」这一族但 states 非空 —— "
                    "这一族不是形态，不该出现在 states 里" % n_bad_none)
            else:
                ok("「没有形态」这一族（%s）的 states 都是空串"
                   % "/".join(no_family))
            # 「无」拆词（D-23.4）之后不该再有裸的「无」。
            # 这一条就是**混合词表的行级检测器**：v1 的行写「无」，v2 写
            # 无·真空/无·贴线，所以只要出现裸「无」就说明有行用了旧口径，
            # 或者构建时没把 n_touch 传进 judge。params_hash 抓不到这个
            # （它只认阈值），所以必须在这里钉住。
            n_bare_none, = con.execute(
                "select count(*) from kbar_shape where symbol=? and interval_ms=? "
                "and label = ?", (sym, iv, shp.STATE_NONE)).fetchone()
            if n_bare_none:
                bad("有 %d 行 label 还是裸「无」（D-23.4 已拆成 %s/%s）—— "
                    "要么是旧口径的行没重跑，要么构建时没把 n_touch 传给 judge"
                    % (n_bare_none, shp.LABEL_NO_EMPTY, shp.LABEL_NO_NEAR))
            else:
                ok("没有裸「无」残留（词表已是 %s）" % shp.JUDGE_RULES_VERSION)
            # 拆出来的两个词都必须真有实例，否则等于拆了个空
            for w in (shp.LABEL_NO_EMPTY, shp.LABEL_NO_NEAR):
                if seen.get(w, 0) <= 0:
                    bad("拆词后的「%s」一次都没出现 —— 拆了个空" % w)
            if seen.get(shp.LABEL_NO_EMPTY, 0) and seen.get(shp.LABEL_NO_NEAR, 0):
                ok("拆出来的两个词都有实例（%s %d / %s %d）"
                   % (shp.LABEL_NO_EMPTY, seen[shp.LABEL_NO_EMPTY],
                      shp.LABEL_NO_NEAR, seen[shp.LABEL_NO_NEAR]))
            # n_states 必须等于 states 的条数
            n_bad_cnt, = con.execute(
                "select count(*) from kbar_shape where symbol=? and interval_ms=? "
                "and n_states <> (length(states) - length(replace(states,',','')) "
                "+ (case when states='' then 0 else 1 end))", (sym, iv)).fetchone()
            if n_bad_cnt:
                bad("有 %d 行 n_states 与 states 条数不符" % n_bad_cnt)
            else:
                ok("n_states 与 states 条数逐行相等")
            # 证据不许为空：落了库却没人读得懂，等于没落
            n_no_ev, = con.execute(
                "select count(*) from kbar_shape where symbol=? and interval_ms=? "
                "and (evidence is null or trim(evidence) = '')", (sym, iv)).fetchone()
            if n_no_ev:
                bad("有 %d 行 evidence 是空的 —— 只存状态词不存理由，复核的人没法验"
                    % n_no_ev)
            else:
                ok("每行都带 evidence（结论可核对）")
            # 结论覆盖面：六个状态词 + 无，一个都不许是「定义有但从不出现」
            never = [w for w in list(shp.STATE_ORDER) if w not in seen]
            if never:
                bad("这些状态词在库里一次都没出现过（判据可能是死条件）：%s" % never)
            else:
                ok("%d 个状态词都有实例（%s）"
                   % (len(shp.STATE_ORDER),
                      ", ".join("%s=%d" % (k, seen[k])
                                for k in shp.STATE_ORDER)))
        # 七个阈值必须都在（v2 多了 swing / panic_close_pos）
        need = ("WICK_PCT_MIN", "WICK_PTS_MIN", "PANIC_VOL_REL_MIN",
                "PANIC_TAKER_BUY_MAX", "PANIC_CLOSE_POS_MAX",
                "STABLE_MAX_PIN_RANGE_MULT", "SCENARIO_TOL_PTS",
                "SWING_LOOKBACK_BARS")
        miss = [n for n in need if n not in ssrc]
        if miss:
            bad("读数层缺阈值常量：%s" % miss)
        else:
            ok("%d 个阈值常量都在" % len(need))
        # 容差只许出现在常量定义与 tol_pts 上（不许扩散到别的判据）
        tol_lines = []
        in_doc = False
        for ln in ssrc.split("\n"):
            if ln.count('"""') == 1:
                in_doc = not in_doc
                continue
            if in_doc or ln.strip().startswith("#"):
                continue
            if "SCENARIO_TOL_PTS" in ln:
                tol_lines.append(ln.strip())
        stray = [x for x in tol_lines
                 if not (x.startswith("SCENARIO_TOL_PTS")
                         or x.startswith("tol_pts")
                         or x.startswith("if tol_pts")
                         or x.startswith('"scenario_tol_pts":'))]
        if stray:
            bad("容差被接到别处了（用户要求只在一个场景生效）：%s" % stray)
        else:
            ok("容差只用在 tol_pts 一处（另有一处是上报口径，不参与判断）")
        # 它不许 import forward
        if any(re.search(pt, ssrc, re.M) for pt in pats2):
            bad("读数层 import 了事后层（当下读数会偷未来）")
        else:
            ok("读数层没 import 事后层")

    # ---------------------------------------------------------------- 7.7 佐证层
    hr("7.7 佐证层（verify.py + 表 6）：拿后面的 K 线验当时的判断")
    # 这一层和表 3 一样是「事后」的，所以隔离规则也照抄表 3 那一套：
    # 单独模块、单独表、判定路径不许 import。
    try:
        import importlib
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if repo_root not in sys.path:
            sys.path.insert(0, repo_root)
        V = importlib.import_module("ai_trader.kbar.verify")
    except Exception as exc:
        bad("import ai_trader.kbar.verify 失败：%s" % exc)
        V = None

    if V is not None:
        verify_py = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "..", "ai_trader", "kbar", "verify.py")
        vsrc = open(verify_py, encoding="utf-8").read()
        ok("佐证层模块在（verify_hash=%s，N=%s，容差=%.0f 点）"
           % (V.verify_hash(), ",".join(str(h) for h in V.VERIFY_HORIZONS),
              V.VERIFY_TOL_PTS))
        # 红线的方向：佐证层可以读判定，判定绝不许读佐证
        if 'ssrc' in dir() and re.search(
                r"from\s+\.\s*import\s+verify|import\s+verify\b", ssrc):
            bad("读数层 import 了佐证层 —— 判定会偷未来，历史结论全部作废")
        else:
            ok("读数层没 import 佐证层（红线：表 5 是输入，表 6 是旁注）")
        if "from . import shape as S" not in vsrc:
            warn("佐证层没有 import shape —— 它读的结论可能和表 5 不是同一套")
        # 佐证层自己的容差必须与 after_pin 的 20 分开（用户要求只在一个场景生效）
        if shp is not None and V.VERIFY_TOL_PTS == shp.SCENARIO_TOL_PTS:
            bad("佐证层用了 after_pin 的容差 %.0f —— 用户要求那个容差只在一个场景生效"
                % shp.SCENARIO_TOL_PTS)
        else:
            ok("佐证层独立容差 %.0f 点（没扩散 after_pin 的那个）" % V.VERIFY_TOL_PTS)

        # 表 6 必须建了且与表 5 等长 × N 个数
        n_vfy = one(con, "select count(*) from kbar_verify where symbol=? "
                         "and interval_ms=?", sym, iv)
        n_shp = one(con, "select count(*) from kbar_shape where symbol=? "
                         "and interval_ms=?", sym, iv)
        expect = n_shp * len(V.VERIFY_HORIZONS)
        if n_vfy == expect:
            ok("表 6 有 %d 行 = 表 5 的 %d 根 × %d 个 N" %
               (n_vfy, n_shp, len(V.VERIFY_HORIZONS)))
        elif n_vfy == 0:
            warn("表 6 是空的 —— 跑 tools/backfill_kbar_verify.py")
        else:
            bad("表 6 行数对不上：%d ≠ %d 根 × %d 个 N = %d"
                % (n_vfy, n_shp, len(V.VERIFY_HORIZONS), expect))

        hashes = [h for (h,) in con.execute(
            "select distinct verify_hash from kbar_verify where symbol=? "
            "and interval_ms=?", (sym, iv))]
        if len(hashes) == 1 and hashes[0] == V.verify_hash():
            ok("表 6 单一本层口径，且与当前代码一致（%s）" % hashes[0])
        elif len(hashes) == 0:
            pass
        elif len(hashes) > 1:
            bad("表 6 混着 %d 套本层口径（%s）—— 必须重跑" %
                (len(hashes), ",".join(hashes)))
        else:
            bad("表 6 口径指纹 %s ≠ 当前 %s —— 改过 N/容差/规则没重跑"
                % (hashes[0], V.verify_hash()))

        # 表 6 的 params_hash 必须与表 5 那根一致（佐证的是同一个判断）
        stale = one(con, "select count(*) from kbar_verify v join kbar_shape s "
                         "on s.symbol=v.symbol and s.interval_ms=v.interval_ms "
                         "and s.ts=v.ts where v.symbol=? and v.interval_ms=? "
                         "and v.params_hash <> s.params_hash", sym, iv)
        if stale:
            bad("表 6 有 %d 行的阈值指纹与表 5 不符 —— 表 5 重算了，表 6 没跟上"
                % stale)
        else:
            ok("表 6 每行的阈值指纹都与表 5 对应那根一致")

        # 裁决值必须全部是那四个词之一
        allowed = set(V.VERDICT_ORDER)
        got = set(v for (v,) in con.execute(
            "select distinct verdict from kbar_verify where symbol=? "
            "and interval_ms=?", (sym, iv)))
        stray = got - allowed
        if stray:
            bad("表 6 出现不认识的裁决：%s" % sorted(stray))
        elif got:
            ok("裁决只有四种：%s" % "、".join(V.VERDICT_ORDER))

        # 表 6 里每行记的容差必须等于当前代码的容差。
        # 只查代码常量是不够的：表里写着 20、代码写着 0，那这张表就是按旧口径
        # 算出来的，行数对得上、指纹对得上，但每个裁决都是错的。
        bad_tol = one(con, "select count(*) from kbar_verify where symbol=? "
                           "and interval_ms=? and abs(tol_pts - ?) > 1e-9",
                      sym, iv, V.VERIFY_TOL_PTS)
        if bad_tol:
            bad("表 6 有 %d 行的容差不是 %.0f 点 —— 表是按别的容差算的，"
                "必须重跑 tools/backfill_kbar_verify.py"
                % (bad_tol, V.VERIFY_TOL_PTS))
        else:
            ok("表 6 每行记的容差都是 %.0f 点（与代码一致）" % V.VERIFY_TOL_PTS)

        # 分布：必须真的四种都出现过（否则某一支是死代码）
        for h in V.VERIFY_HORIZONS:
            slot = dict(con.execute(
                "select verdict, count(*) from kbar_verify where symbol=? "
                "and interval_ms=? and horizon=? group by verdict",
                (sym, iv, h)))
            tot = sum(slot.values())
            if not tot:
                continue
            print("  N=%d：%s" % (h, "  ".join(
                "%s %d" % (v, slot.get(v, 0)) for v in V.VERDICT_ORDER)))
            if slot.get(V.VERDICT_HELD, 0) == 0 or slot.get(V.VERDICT_BROKE, 0) == 0:
                bad("N=%d 里成立或推翻有一边是 0 —— 预言规则可能没接线" % h)
            # 尾部的「无法判定」必须正好等于 N（最后几根凑不满）
            unk = slot.get(V.VERDICT_UNKNOWN, 0)
            if unk != h:
                warn("N=%d 的「无法判定」是 %d 行，按理应正好 %d 行（表尾）"
                     % (h, unk, h))

        # 佐证行不许出现「拿本根当未来」
        same_ts = one(con, "select count(*) from kbar_verify v join kbar_evolution e "
                           "on e.symbol=v.symbol and e.interval_ms=v.interval_ms "
                           "and e.ts=v.ts where v.symbol=? and v.interval_ms=? "
                           "and v.n_bars > 0 and v.verdict in ('佐证成立','被推翻') "
                           "and v.actual_price is null", sym, iv)
        if same_ts:
            bad("表 6 有 %d 行判了成立/推翻却没记实际价位 —— 无法复核" % same_ts)
        else:
            ok("每个成立/推翻的行都记了实际价位（可复核）")

        # 预言原文不许空
        empty_text = one(con, "select count(*) from kbar_verify where symbol=? "
                              "and interval_ms=? and (pred_text is null or "
                              "pred_text = '')", sym, iv)
        if empty_text:
            bad("表 6 有 %d 行没有预言原文 —— 事后没人能知道当时说的什么"
                % empty_text)
        else:
            ok("每行都有预言原文")

    # ---------------------------------------------------------------- 7.10 表 7
    hr("7.10 表 7（kbar_dims）· 描述层：不参与判定，但不许是空壳")
    try:
        n_dims_rows = one(con, "select count(*) from kbar_dims "
                          "where symbol=? and interval_ms=?", sym, iv)
        n_bars = one(con, "select count(*) from kbar_evolution "
                      "where symbol=? and interval_ms=? and o is not null",
                      sym, iv)
    except sqlite3.OperationalError:
        warn("表 7 (kbar_dims) 还不存在 —— 跑 tools/backfill_kbar_dims.py")
        n_dims_rows = n_bars = 0

    if n_dims_rows:
        print("  表 7 行数 %d / 表 1 有 OHLC 的根 %d" % (n_dims_rows, n_bars))
        if n_dims_rows != n_bars:
            bad("表 7 行数 %d ≠ 表 1 的 %d 根 —— 有根没算到" % (n_dims_rows,
                                                            n_bars))
        else:
            ok("表 7 一根一份快照")

        # 期望的维度数从代码里取，不再写死 —— 写死的话删/加维度时
        # 检查脚本和代码会各说各话，而这正是「台账漂移」的来源。
        _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if _root not in sys.path:
            sys.path.insert(0, _root)
        _nd_expect = None
        try:
            from ai_trader.kbar import dims as _D0
            _nd_expect = len(_D0.DIM_NAMES)
        except Exception as e:      # noqa: BLE001
            warn("没法 import dims 取维度数：%s" % e)

        # 口径必须只有一套
        hashes = [r[0] for r in con.execute(
            "select distinct dims_hash from kbar_dims "
            "where symbol=? and interval_ms=?", (sym, iv))]
        if len(hashes) > 1:
            bad("表 7 混着 %d 套口径（%s）" % (len(hashes), ",".join(hashes)))
        else:
            ok("表 7 口径一致 %s" % (hashes[0] if hashes else "-"))

        # ⚠ 这一条是本节的重点：n_filled 全 0 = 表建了但算法没接线
        #    `one()` 只取第一列，这里要三列，所以直接 fetchone。
        fmin, fmax, favg = con.execute(
            "select min(n_filled), max(n_filled), avg(n_filled) from kbar_dims "
            "where symbol=? and interval_ms=?", (sym, iv)).fetchone()
        print("  有值维度 min %s / max %s / avg %.1f"
              % (fmin, fmax, float(favg or 0)))
        if not fmax:
            bad("表 7 一根都没算出维度（n_filled 全 0）—— 算法没接线")
        elif _nd_expect and int(fmax) < max(1, _nd_expect - 9):
            warn("表 7 单根最多只有 %s 个维度有值（%d 个里）"
                 % (fmax, _nd_expect))

        # n_dims 必须恒等于 dims.DIMS 的长度
        nd = [r[0] for r in con.execute(
            "select distinct n_dims from kbar_dims "
            "where symbol=? and interval_ms=?", (sym, iv))]
        if _nd_expect is None:
            warn("取不到代码里的维度数，跳过 n_dims 核对（出现值 %s）" % (nd,))
        elif len(nd) != 1 or nd[0] != _nd_expect:
            bad("表 7 的 n_dims 出现 %s —— 应为唯一值 %d（表 7 该重建了）"
                % (nd, _nd_expect))
        else:
            ok("表 7 每根都算 %d 个维度" % _nd_expect)

        # 维度名必须与总账一致（读第一条的 values_json）
        row = con.execute("select values_json from kbar_dims "
                          "where symbol=? and interval_ms=? "
                          "order by ts limit 1", (sym, iv)).fetchone()
        if row:
            try:
                keys = set(json.loads(row[0]))
            except ValueError:
                keys = set()
                bad("表 7 values_json 不是合法 JSON")
            if keys:
                try:
                    from ai_trader.kbar import dims as _D
                    if keys != set(_D.DIM_NAMES):
                        bad("表 7 的维度名与 dims.DIMS 不一致：多 %s 少 %s"
                            % (sorted(keys - set(_D.DIM_NAMES)),
                               sorted(set(_D.DIM_NAMES) - keys)))
                    else:
                        ok("表 7 的 %d 个维度名与代码一致" % len(_D.DIM_NAMES))
                except Exception as e:      # noqa: BLE001
                    warn("没法 import dims 核对维度名：%s" % e)

        # 旁证/矛盾提示：有，且只用「判据没读」的维度
        n_notes = one(con, "select count(*) from kbar_dims "
                      "where symbol=? and interval_ms=? and notes_json != '[]'",
                      sym, iv)
        print("  带提示的根 %d（%.1f%%）"
              % (n_notes, 100.0 * n_notes / n_dims_rows))
        if not n_notes:
            warn("表 7 一条旁证/矛盾提示都没有 —— 规则可能没接线")

    # ---------------------------------------------------------------- 7.11 表 8
    hr("7.11 表 8（kbar_holdout_log）· 留出集预登记：只增不改，缺字段要看得见")
    try:
        cur = con.execute("select * from kbar_holdout_log "
                          "where symbol=? and interval_ms=? order by id",
                          (sym, iv))
        names = [d[0] for d in cur.description]
        hl = [dict(zip(names, row)) for row in cur.fetchall()]
        has_hl = True
    except sqlite3.OperationalError:
        warn("表 8 (kbar_holdout_log) 还不存在 —— 跑 tools/eval_kbar_holdout.py")
        has_hl = False

    if has_hl:
        print("  登记行数 %d" % len(hl))
        if not hl:
            print("  （还没评测过。留出集是干净的 —— 别白跑，想好再跑。）")

        # 1) 每行的 split_hash 必须和 kbar_meta 里冻结的那份一致
        frozen = None
        try:
            row = con.execute("select value from kbar_meta where key=?",
                              ("holdout_split:%s:%d" % (sym, iv),)).fetchone()
            if row:
                frozen = json.loads(row[0]).get("split_hash")
        except (sqlite3.OperationalError, ValueError):
            pass
        if hl and not frozen:
            warn("表 8 有登记行，但 kbar_meta 里没有冻结的切分凭据")
        elif hl:
            used = {r["split_hash"] for r in hl}
            if used != {frozen}:
                bad("表 8 用了 %s 这些切分，与冻结的 %s 不符 —— "
                    "切分被换过，旧结论要作废" % (sorted(used), frozen))
            else:
                ok("表 8 全部登记都用冻结切分 %s" % frozen)

        # 2) dims_hash 必须是当前代码的口径
        try:
            _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            if _root not in sys.path:
                sys.path.insert(0, _root)
            from ai_trader.kbar import dims as _D2
            cur_dims = _D2.dims_hash()
            stale = [r["id"] for r in hl if r["dims_hash"] != cur_dims]
            if stale:
                warn("表 8 有 %d 行用的是旧维度口径（%s），当前是 %s"
                     % (len(stale), sorted({r["dims_hash"] for r in hl
                                            if r["dims_hash"] != cur_dims}),
                        cur_dims))
            else:
                ok("表 8 全部登记的维度口径都是 %s" % cur_dims)
        except Exception as e:          # noqa: BLE001
            warn("没法 import dims 核对口径：%s" % e)

        # 3) ⚠ 本节重点：主口径计数列不许为空。
        #    第一版 DDL 漏了 n_pass_cluster，白名单插入静默把它丢掉，
        #    结果「登记了，但登记的不是主口径的数」。补列后旧行是 NULL，
        #    那是历史化石，只对新行要求非空。
        try:
            null_ids = [r["id"] for r in hl if r["n_pass_cluster"] is None]
            if null_ids:
                print("  行 %s 的 n_pass_cluster 是 NULL —— 列是后补的，"
                      "这些行登记时还没有这一列（化石，不是新 bug）"
                      % null_ids)
            new_bad = [r["id"] for r in hl
                       if r["n_pass_cluster"] is not None
                       and (r["n_pass_cluster"] > r["n_tested"]
                            or r["n_pass_cluster"] < 0)]
            if new_bad:
                bad("表 8 这些行的主口径计数不合理（过线格数 > 验过格数）：%s"
                    % new_bad)
            elif any(r["n_pass_cluster"] is not None for r in hl):
                ok("表 8 主口径计数列有值且自洽")
        except sqlite3.OperationalError:
            bad("表 8 没有 n_pass_cluster 列 —— 主口径的数会丢")

        # 4) n_expected 必须等于 5% × n_tested
        off = [r["id"] for r in hl
               if abs((r["n_expected"] or 0) - 0.05 * (r["n_tested"] or 0)) > 0.06]
        if off:
            bad("表 8 这些行的 n_expected ≠ 5%% × n_tested：%s" % off)
        elif hl:
            ok("表 8 的基准期望值都等于 5% × 验过格数")

        # 5) 多次评测要显式提示，别只看最好看的那次
        if len(hl) > 1:
            print("  ⚠ 这个留出集已被评测 %d 次 —— 结论要放在一起看，"
                  "不许只报最好看的那次" % len(hl))

    # ---------------------------------------------------------------- 8 留空清单
    hr("8. 故意留空的列（不是缺陷，是待确定口径）")
    for t, col, why in (
            ("kbar_evolution", "n_break", "D-10"),
            ("kbar_evolution", "path_shape", "D-1"),
            ("kbar_evolution", "max_tick_share", "D-2"),
            ("kbar_reaction", "dwell_bars", "D-8"),
            ("kbar_evolution", "state(非 neutral 时)", "D-17")):
        real = col.split("(")[0]
        if real == "state(非 neutral 时)":
            continue
        try:
            n = one(con, "select count(%s) from %s where symbol=? and interval_ms=?"
                    % (real, t), sym, iv)
            tot = one(con, "select count(*) from %s where symbol=? and interval_ms=?"
                       % t, sym, iv)
        except sqlite3.OperationalError:
            continue
        print("  %-16s.%-16s %6d / %6d   （%s）" % (t, real, n, tot, why))
    print("  kbar_evolution.state 只有 'neutral' 与 NULL 两种值（D-17）")

    con.close()
    print("\n" + "=" * 76)
    print("结论")
    print("=" * 76)
    if PROBLEMS:
        print("  %d 个问题：" % len(PROBLEMS))
        for p in PROBLEMS:
            print("   - " + p)
        return 1
    if WARNS:
        print("  通过，但有 %d 条提醒：" % len(WARNS))
        for w in WARNS:
            print("   - " + w)
        return 0
    print("  全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
