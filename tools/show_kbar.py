# -*- coding: utf-8 -*-
"""查看某一根 15m K 线的**判定**与全部演化读数，并**独立重算**关键判定。

用途：验证「表 1~表 3 记的数对不对」。它不复用 reaction/event/forward 的判定函数，
而是按定义从原始数据重算一遍，然后把重算结果与库里的值逐项比对。

为什么不能直接打印库里的值：那只能证明「库里写着这个数」，
证明不了「这个数是对的」。这个项目反复出现「测试通过但没接线」，
打印自己写的东西同样证明不了任何事。

**先给判定，再给证据。**
一开始这个工具只印客观值（n_touch=3 / vol_rel=7.35），用户的原话是
「你只是包含客观的值，没有判断」。绝对值不是读数 —— 一根下影 166 点，
读者还得自己判断这算不算插针。所以现在第一段就是「这一根是什么」，
下面才是支撑它的数。

判定来自 `ai_trader.kbar.shape`，**已落表 5 `kbar_shape`**：
它没有对应的表，所以没有「库里的值」可以比对。这一段的正确性靠
`tests/test_kbar_shape.py` 与 `tools/probe_kbar_shape.py` 保证，不是靠 --verify。

用法：
    python tools/show_kbar.py --at "2026-11-07 23:45"          # 北京时间
    python tools/show_kbar.py --at "2026-11-07 15:45" --tz utc
    python tools/show_kbar.py --at "2026-11-07 23:45" --verify  # 只判对错，退出码
    python tools/show_kbar.py --at "2026-11-07 23:45" --chain 8 # 带前后各 8 根

--verify 的退出码：0 = 全部重算与库一致；1 = 有不一致（会逐条打印差在哪）。
"""
from __future__ import annotations

import argparse
import datetime
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ai_trader.kbar import shape as S  # noqa: E402

DEFAULT_KBAR_DB = os.path.join(ROOT, "data", "kbar.db")
INTERVAL_MS = 900_000
DEFAULT_SYMBOL = "BTCUSDT"


# ---------------------------------------------------------------- 时间
def parse_at(s: str, tz_hours: float) -> int:
    """把 'YYYY-MM-DD HH:MM' 按给定时区解释成 UTC 毫秒时间戳。

    为什么要显式传时区：同一串「11-07 23:45」在 UTC 和 UTC+8 下是两根不同的 K 线，
    而两者在库里都可能有数据 —— 悄悄选一个会让整份读数对不上，
    且看起来完全正常（价格都在合理范围）。
    """
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            dt = datetime.datetime.strptime(s, fmt)
            break
        except ValueError:
            continue
    else:
        raise SystemExit("看不懂的时间：%r（要 'YYYY-MM-DD HH:MM'）" % s)
    epoch = datetime.datetime(1970, 1, 1)
    sec = int((dt - epoch).total_seconds() - tz_hours * 3600)
    return sec * 1000


def fmt_utc(ms):
    if ms is None:
        return "-"
    return datetime.datetime.utcfromtimestamp(int(ms) / 1000).strftime("%Y-%m-%d %H:%M")


def fmt_bj(ms):
    if ms is None:
        return "-"
    return (datetime.datetime.utcfromtimestamp(int(ms) / 1000)
            + datetime.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M")


def hr(t):
    print("\n" + "=" * 84)
    print(t)
    print("=" * 84)


# ---------------------------------------------------------------- 独立重算
def recompute(con, ts, sym, iv=INTERVAL_MS):
    """按定义从表 0 / 表 1 重算这一根的读数。不 import 任何判定模块。"""
    out = {"problems": [], "bars": None, "touch": {}, "events": {}, "forward": {}}

    bar = con.execute("select o,h,l,c from kbar_evolution where symbol=? and "
                      "interval_ms=? and ts=?", (sym, iv, ts)).fetchone()
    if bar is None:
        out["problems"].append("表 1 里没有这根 K 线（ts=%d）" % ts)
        return out
    o, h, l, c = [float(x) for x in bar]
    out["bars"] = (o, h, l, c)

    prev = ts - iv
    prevrows = {r[0]: float(r[1]) for r in con.execute(
        "select line_key, line_price from kbar_line where symbol=? and interval_ms=? "
        "and ts=?", (sym, iv, prev))}
    out["prev_rows"] = len(prevrows)
    out["prev_ts"] = prev

    # 1) 碰到：l <= p <= h，p 取上一根那一行（防偷未来）
    mine_touch = {}
    for k, p in prevrows.items():
        if l <= p <= h:
            # 从哪边来 = 本根开盘与线价比
            side = "from_above" if o > p else "from_below"
            pen = (p - l) / p if side == "from_above" else (h - p) / p
            rec = (c - l) / p if side == "from_above" else (h - c) / p
            close_side = "above" if c > p else ("below" if c < p else "at")
            mine_touch[k] = {"p": p, "side": side, "pen": pen, "rec": rec,
                             "close_side": close_side, "dist_pct": (c - p) / p * 100.0}
    out["touch"] = mine_touch

    # 2) 事件：收盘相对线价换边（prev_close 与 c 分别在哪侧）
    prev_close = con.execute("select c from kbar_evolution where symbol=? and "
                             "interval_ms=? and ts=?", (sym, iv, prev)).fetchone()
    if prev_close:
        pc = float(prev_close[0])
        mine_ev = {}
        for k, p in prevrows.items():
            was = "above" if pc > p else ("below" if pc < p else "at")
            now = "above" if c > p else ("below" if c < p else "at")
            if was != now and was != "at" and now != "at":
                mine_ev[k] = {"event": "向上突破" if now == "above" else "向下突破",
                              "was": was, "now": now}
        out["events"] = mine_ev

    # 3) 事后：ret_k = c[t+k*iv]/c[t] - 1
    fw = {}
    for k in (1, 3, 5):
        row = con.execute("select c from kbar_evolution where symbol=? and "
                          "interval_ms=? and ts=?", (sym, iv, ts + k * iv)).fetchone()
        fw["ret%d" % k] = None if not row or row[0] is None else float(row[0]) / c - 1.0
    out["forward"] = fw
    out["c"] = c
    return out


# ---------------------------------------------------------------- 判定（现算）
def judge_bar(con, ts, sym, iv=INTERVAL_MS):
    """现算这一根的形态判定，返回 (Judgement, 本根在 bars 里的下标)。

    「插针后稳住」要回看前几根，所以必须把 `[ts - look*iv, ts]` 这段一起读进来。
    线快照用**上一根**的（防偷未来）：表 0 里 `ts - iv` 那一行，
    没有就是当时那条线不存在 —— 不能退而求其次去用本根或更早的。
    """
    # 窗口必须容得下 swing 针的确认区间：判第 i 根时要看 bars[i-3-8 .. i]。
    # 这里少开一根，`swing_pin_kind` 就看不到够远的历史，结论会和
    # `backfill_kbar_shape.py`（读全序列）不一致 —— 而且不报错，只是悄悄变短。
    look = S.STABLE_LOOKBACK_BARS
    lo = ts - (look + S.SWING_LOOKBACK_BARS) * iv
    rows = list(con.execute(
        "select ts,o,h,l,c,vol_rel,taker_buy_ratio from kbar_evolution "
        "where symbol=? and interval_ms=? and ts>=? and ts<=? order by ts",
        (sym, iv, lo, ts)))
    bars = [{"ts": r[0], "o": r[1], "h": r[2], "l": r[3], "c": r[4],
             "vol_rel": r[5], "taker_buy_ratio": r[6]} for r in rows]
    i = len(bars) - 1
    if i < 0 or bars[i]["ts"] != ts:
        return None, -1
    prev_lines = {r[0]: float(r[1]) for r in con.execute(
        "select line_key, line_price from kbar_line where symbol=? and interval_ms=? "
        "and ts=?", (sym, iv, ts - iv))}
    return S.judge(bars, i, prev_lines=prev_lines), i


def has_shape_table(con):
    """表 5 建了没有。区分「整层还没落库」与「这一根没落库」—— 前者跳过，
    后者是真问题：掺在一起会把「还没跑回填」误报成「数据坏了」。"""
    return con.execute(
        "select count(*) from sqlite_master where type='table' and name='kbar_shape'"
    ).fetchone()[0] > 0


def stored_judgement(con, ts, sym, iv=INTERVAL_MS):
    """读表 5（落库的判定）。没有这一行就返回 None，**不拿现算冒充库里的**。"""
    if not has_shape_table(con):
        return None
    row = con.execute(
        "select label, states, n_states, side, pin_kind, ref_pin_ts, ref_pin_kind, "
        "       rng, body_pct, up_wick_pct, dn_wick_pct, close_pos, vol_rel, "
        "       taker_buy_ratio, n_recovered, recovered, evidence, params_hash "
        "from kbar_shape where symbol=? and interval_ms=? and ts=?",
        (sym, iv, ts)).fetchone()
    if row is None:
        return None
    keys = ("label", "states", "n_states", "side", "pin_kind", "ref_pin_ts",
            "ref_pin_kind", "rng", "body_pct", "up_wick_pct", "dn_wick_pct",
            "close_pos", "vol_rel", "taker_buy_ratio", "n_recovered",
            "recovered", "evidence", "params_hash")
    d = dict(zip(keys, row))
    d["state_list"] = [x for x in (d["states"] or "").split(",") if x]
    d["evidence_lines"] = [x for x in (d["evidence"] or "").split("\n") if x]
    return d


def has_verify_table(con):
    """表 6（佐证层）建了没有。同 `has_shape_table` 的理由：
    「整层还没落库」与「这一根没落库」必须分开报，不许混成一句。"""
    return con.execute(
        "select count(*) from sqlite_master where type='table' and name='kbar_verify'"
    ).fetchone()[0] > 0


def stored_verdicts(con, ts, sym, iv=INTERVAL_MS):
    """读表 6（用后面的 K 线给出的裁决）。没建表返回 None，没这一根返回 []。"""
    if not has_verify_table(con):
        return None
    cur = con.execute(
        "select horizon, label, states, pred_kind, pred_text, verdict, "
        "       ref_price, actual_price, diff_pts, n_bars, tol_pts, "
        "       params_hash, verify_hash "
        "from kbar_verify where symbol=? and interval_ms=? and ts=? "
        "order by horizon", (sym, iv, ts))
    keys = ("horizon", "label", "states", "pred_kind", "pred_text", "verdict",
            "ref_price", "actual_price", "diff_pts", "n_bars", "tol_pts",
            "params_hash", "verify_hash")
    return [dict(zip(keys, r)) for r in cur]


# ---------------------------------------------------------------- 主流程
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="查看某一根 15m K 线的演化读数并独立校验")
    ap.add_argument("--at", required=True, help="'YYYY-MM-DD HH:MM'")
    ap.add_argument("--tz", default="bj", choices=["bj", "utc"],
                    help="--at 按哪个时区解释（默认 bj = UTC+8）")
    ap.add_argument("--kbar-db", default=DEFAULT_KBAR_DB)
    ap.add_argument("--symbol", default=DEFAULT_SYMBOL)
    ap.add_argument("--interval", type=int, default=INTERVAL_MS)
    ap.add_argument("--chain", type=int, default=0, help="另打印前后各 N 根")
    ap.add_argument("--verify", action="store_true", help="只判对错，不打印明细")
    a = ap.parse_args(argv)

    sym = a.symbol
    tz_h = 8.0 if a.tz == "bj" else 0.0
    ts = parse_at(a.at, tz_h)

    if not os.path.exists(a.kbar_db):
        print("没有库：%s" % a.kbar_db)
        return 2
    con = sqlite3.connect("file:%s?mode=ro" % a.kbar_db.replace("\\", "/"), uri=True)
    iv = a.interval

    have = con.execute("select count(*) from kbar_evolution where symbol=? and "
                       "interval_ms=? and ts=?", (sym, iv, ts)).fetchone()[0]
    if not have:
        hr("库里没有这一根")
        print("  你给的时间   %s（按 %s 解释）" % (a.at, "UTC+8 北京" if tz_h else "UTC"))
        print("  换算成 UTC   %s   ts=%d" % (fmt_utc(ts), ts))
        lo, hi = con.execute("select min(ts), max(ts) from kbar_evolution where "
                             "symbol=? and interval_ms=?", (sym, iv)).fetchone()
        print("  库里覆盖     %s .. %s (UTC)  %s .. %s (北京)"
              % (fmt_utc(lo), fmt_utc(hi), fmt_bj(lo), fmt_bj(hi)))
        near = con.execute("select ts from kbar_evolution where symbol=? and "
                           "interval_ms=? order by abs(ts-?) limit 1",
                           (sym, iv, ts)).fetchone()
        if near:
            print("  最近的一根   %s (UTC)  %s (北京)  差 %+d 根"
                  % (fmt_utc(near[0]), fmt_bj(near[0]), (ts - near[0]) // iv))
        print("\n  提示：这不是 bug —— K 线是等间隔的，给定的分钟数必须是 15 的倍数"
              "（:00 :15 :30 :45）。")
        con.close()
        return 1

    r = recompute(con, ts, sym, iv)
    problems = list(r["problems"])

    # 库里读到的值
    cur = con.execute("select * from kbar_evolution where symbol=? and interval_ms=? "
                      "and ts=?", (sym, iv, ts))
    keys = [d[0] for d in cur.description]
    bar = dict(zip(keys, cur.fetchone()))
    db_touch = {x[0]: {"p": float(x[1]), "dist_pct": float(x[2]), "side": x[3],
                       "pen": float(x[4]), "rec": float(x[5]), "close_side": x[6],
                       "nth": x[7]}
                for x in con.execute(
                    "select line_key, line_price, dist_pct, side, pen_depth_pct, "
                    "recover_pct, close_side, nth_touch from kbar_reaction "
                    "where symbol=? and interval_ms=? and ts=?", (sym, iv, ts))}
    db_ev = {x[0]: {"p": float(x[1]), "event": x[2], "prev_event": x[3],
                    "close_side": x[5]}
             for x in con.execute(
                 "select line_key, line_price, event, prev_event, since_event_idx, "
                 "close_side from kbar_event where symbol=? and interval_ms=? and ts=?",
                 (sym, iv, ts))}
    fcur = con.execute("select * from kbar_forward where symbol=? and interval_ms=? "
                       "and ts=?", (sym, iv, ts))
    fkeys = [d[0] for d in fcur.description]
    frow = fcur.fetchone()
    if frow is None:
        # 表 3 缺这一根 = 第 5 步没跑，或这一根是后加的。两种都要说出来，
        # 不能当成「ret 都是空」蒙过去（那 --verify 会静默放行）。
        fw = {k: None for k in fkeys}
        problems.append("表 3 里没有这一根（第 5 步没跑，或这一根是后加的）")
    else:
        fw = dict(zip(fkeys, frow))

    # ---------------- 校验 ----------------
    # 表 2 的集合必须等于重算的触碰集合
    if set(db_touch) != set(r["touch"]):
        problems.append("表 2 的触碰集合与重算不符：库里多 %s / 少 %s"
                        % (sorted(set(db_touch) - set(r["touch"])),
                           sorted(set(r["touch"]) - set(db_touch))))
    for k, m in r["touch"].items():
        d = db_touch.get(k)
        if not d:
            continue
        if abs(d["p"] - m["p"]) > 1e-6:
            problems.append("%s 线价不符：表2=%s 重算=%s" % (k, d["p"], m["p"]))
        if abs(d["pen"] - m["pen"]) > 1e-9:
            problems.append("%s 穿透不符：表2=%s 重算=%s" % (k, d["pen"], m["pen"]))
        if abs(d["rec"] - m["rec"]) > 1e-9:
            problems.append("%s 收回不符：表2=%s 重算=%s" % (k, d["rec"], m["rec"]))
        if d["close_side"] != m["close_side"]:
            problems.append("%s 收盘侧不符：表2=%s 重算=%s"
                            % (k, d["close_side"], m["close_side"]))
        if d["side"] != m["side"]:
            problems.append("%s 来自哪侧不符：表2=%s 重算=%s"
                            % (k, d["side"], m["side"]))
        if abs(d["dist_pct"] - m["dist_pct"]) > 1e-6:
            problems.append("%s 距离不符：表2=%s 重算=%s"
                            % (k, d["dist_pct"], m["dist_pct"]))

    # 表 1 的 n_touch 必须 == 表 2 行数
    if int(bar["n_touch"] or 0) != len(db_touch):
        problems.append("表 1 n_touch=%s 但表 2 有 %d 行"
                        % (bar["n_touch"], len(db_touch)))

    # 表 2.5 的「向上/向下突破」子集必须都在重算的换边集合里
    for k, e in db_ev.items():
        if e["event"] in ("向上突破", "向下突破"):
            m = r["events"].get(k)
            if not m:
                problems.append("表 2.5 说 %s 在 %s 上%s，但重算收盘没换边"
                                % (k, fmt_utc(ts), e["event"]))
            elif m["event"] != e["event"]:
                problems.append("%s 事件方向不符：表2.5=%s 重算=%s"
                                % (k, e["event"], m["event"]))

    # 表 1 的 on_line 必须在表 2.5 里
    if bar["event"] and bar["on_line"] not in db_ev:
        problems.append("表 1 on_line=%s 不在表 2.5 里" % bar["on_line"])

    # 表 3 的 ret 必须等于重算
    for k in (1, 3, 5):
        col = "ret%d" % k
        want = r["forward"][col]
        got = fw.get(col)
        if want is None and got is None:
            continue
        if want is None or got is None:
            problems.append("%s：表3=%s 重算=%s（一边有值一边空）" % (col, got, want))
        elif abs(float(got) - want) > 1e-12:
            problems.append("%s 不符：表3=%.9f 重算=%.9f" % (col, got, want))

    # 表 3 的 held 只能落在有事件的根上
    if fw.get("held") is not None and not bar["event"]:
        problems.append("表 3 held=%s 但这根没有事件" % fw.get("held"))

    if a.verify:
        print("ts=%d  %s (UTC)  %s (北京)" % (ts, fmt_utc(ts), fmt_bj(ts)))
        jd, _ji = judge_bar(con, ts, sym, iv)
        stj = stored_judgement(con, ts, sym, iv)
        if stj is None:
            if not has_shape_table(con):
                print("判定 未校验（本库还没有表 5 —— 先跑 "
                      "tools/backfill_kbar_shape.py）")
            else:
                problems.append("表 5 没有这一根 —— 先跑 "
                                "tools/backfill_kbar_shape.py")
        elif jd is None:
            problems.append("表 5 有判定（%s）但现算算不出来" % stj["label"])
        else:
            if stj["label"] != jd.label:
                problems.append("判定不符：表5=%s 现算=%s" % (stj["label"], jd.label))
            if stj["state_list"] != list(jd.states):
                problems.append("states 不符：表5=%s 现算=%s"
                                % (stj["state_list"], list(jd.states)))
            if stj["side"] != jd.side:
                problems.append("side 不符：表5=%s 现算=%s" % (stj["side"], jd.side))
            if stj["pin_kind"] != jd.pin_kind:
                problems.append("pin_kind 不符：表5=%s 现算=%s"
                                % (stj["pin_kind"], jd.pin_kind))
            if stj["ref_pin_kind"] != jd.ref_pin_kind:
                problems.append("ref_pin_kind 不符：表5=%s 现算=%s"
                                % (stj["ref_pin_kind"], jd.ref_pin_kind))
            if stj["ref_pin_ts"] != jd.pin_ts:
                problems.append("参照针不符：表5=%s 现算=%s"
                                % (stj["ref_pin_ts"], jd.pin_ts))
            if stj["evidence_lines"] != list(jd.evidence):
                problems.append("evidence 不符（表5 %d 行 / 现算 %d 行）"
                                % (len(stj["evidence_lines"]), len(jd.evidence)))
            if stj["params_hash"] != S.params_hash():
                problems.append("口径指纹不符：表5=%s 当前=%s —— 改过阈值没重跑"
                                % (stj["params_hash"], S.params_hash()))
        if stj is not None:
            print("判定 %s（表 5 落库，已与现算逐字段复核）" % stj["label"])

        # 表 6 也必须复核：佐证行是拿后面 K 线算的，最容易出「口径变了没重跑」
        import importlib
        try:
            V = importlib.import_module("ai_trader.kbar.verify")
        except Exception as exc:            # pragma: no cover - 环境问题
            V = None
            problems.append("import verify 失败：%s" % exc)
        verdicts = stored_verdicts(con, ts, sym, iv)
        if verdicts is None:
            print("佐证 未校验（本库还没有表 6）")
        elif not verdicts:
            problems.append("表 6 没有这一根 —— 先跑 "
                            "tools/backfill_kbar_verify.py")
        elif V is not None:
            cur_hash = V.verify_hash()
            for v in verdicts:
                if v["verify_hash"] != cur_hash:
                    problems.append(
                        "表 6 的 N=%d 用旧口径算的（%s ≠ 当前 %s）—— 重跑回填"
                        % (v["horizon"], v["verify_hash"], cur_hash))
                if v["params_hash"] != S.params_hash():
                    problems.append(
                        "表 6 的 N=%d 对应的表 5 指纹是旧的（%s ≠ %s）"
                        % (v["horizon"], v["params_hash"], S.params_hash()))
                if v["tol_pts"] != V.VERIFY_TOL_PTS:
                    problems.append(
                        "表 6 的 N=%d 容差是 %s，当前是 %s"
                        % (v["horizon"], v["tol_pts"], V.VERIFY_TOL_PTS))
            if not any(p.startswith("表 6") for p in problems):
                print("佐证 %d 个 N 的裁决与口径指纹全部一致"
                      % len(verdicts))

        if problems:
            print("不一致 %d 处：" % len(problems))
            for p in problems:
                print("  - " + p)
            con.close()
            return 1
        print("重算与库全部一致（触碰 %d 条 / 事件 %d 条 / 事后 ret / n_touch）"
              % (len(db_touch), len(db_ev)))
        con.close()
        return 0

    # ---------------- 打印 ----------------
    o, h, l, c = r["bars"]
    hr("K 线  ts=%d   UTC %s   北京 %s" % (ts, fmt_utc(ts), fmt_bj(ts)))

    # 【第一段就是判定】用户的原话：「你只是包含客观的值，没有判断」。
    # 所以先给「这一根是什么」，客观值退到下面当证据。
    jd, _ji = judge_bar(con, ts, sym, iv)
    stj = stored_judgement(con, ts, sym, iv)
    print("\n【判定 · 表 5 kbar_shape 落库】ai_trader/kbar/shape.py 算的，已入 db")
    if stj is None:
        if not has_shape_table(con):
            print("  本库还没有表 5 —— 判定层还没落库。跑 "
                  "tools/backfill_kbar_shape.py 回填")
        else:
            print("  表 5 里没有这一根 —— 跑 tools/backfill_kbar_shape.py 补齐")
    if stj is not None:
        print("  这一根 = 【%s】" % stj["label"])
        if stj["n_states"] > 1:
            print("  同时成立：%s" % "、".join(stj["state_list"]))
        print("  自己是针   %s / %s"
              % (stj["side"] or "-", stj["pin_kind"] or "-"))
        if stj["ref_pin_ts"] is not None:
            print("  稳的是     %s (UTC)  %s (北京) 的针（形态 %s）"
                  % (fmt_utc(stj["ref_pin_ts"]), fmt_bj(stj["ref_pin_ts"]),
                     stj["ref_pin_kind"] or "-"))
        for e in stj["evidence_lines"]:
            print("    · %s" % e)
        print("  落库结构   全幅=%.1f 实体=%.0f%% 下影=%.0f%% 上影=%.0f%% 收位=%.0f%%"
              % (stj["rng"], 100 * (stj["body_pct"] or 0),
                 100 * (stj["dn_wick_pct"] or 0), 100 * (stj["up_wick_pct"] or 0),
                 100 * (stj["close_pos"] or 0)))
        print("  落库收回   %d 条%s" % (stj["n_recovered"],
                                   ("  " + stj["recovered"]) if stj["recovered"] else ""))
        print("  口径指纹   %s（当前 shape.py 是 %s）%s"
              % (stj["params_hash"], S.params_hash(),
                 "" if stj["params_hash"] == S.params_hash()
                 else "  ← 不一致，这张表是旧口径算的，快 --rebuild"))

    # 现算复核：库里的值必须和现算一致，否则表 5 是坏数据
    print("\n【判定 · 现算复核】同一根重算一遍，和表 5 比")
    if jd is None:
        print("  算不出来（表 1 缺这一根或数据不连续）")
    elif stj is None:
        print("  现算 = 【%s】（表里没有，无从比较）" % jd.label)
    else:
        same = (jd.label == stj["label"]
                and list(jd.states) == stj["state_list"]
                and jd.side == stj["side"] and jd.pin_kind == stj["pin_kind"]
                and jd.pin_ts == stj["ref_pin_ts"]
                and jd.ref_pin_kind == stj["ref_pin_kind"]
                and list(jd.evidence) == stj["evidence_lines"])
        print("  现算 = 【%s】   与表 5 %s" % (jd.label, "完全一致 OK" if same else "不一致 ← 错！"))
        if not same:
            print("     表5 states=%s side=%s pin=%s ref=%s"
                  % (stj["state_list"], stj["side"], stj["pin_kind"],
                     stj["ref_pin_kind"]))
            print("     现算states=%s side=%s pin=%s ref=%s"
                  % (list(jd.states), jd.side, jd.pin_kind, jd.ref_pin_kind))

    # 【佐证层】用户 2026-11-08：「判断对不对，可以用后面几根 K 线来判」。
    # 这是全项目唯一允许看未来的地方，所以必须**明确标出来**——
    # 不标，读者会以为上面的判定用到了这些数据（那就全废了）。
    print("\n【佐证 · 表 6 kbar_verify】拿**后面**的 K 线判当时那句话对不对")
    verdicts = stored_verdicts(con, ts, sym, iv)
    if verdicts is None:
        print("  本库还没有表 6 —— 佐证层还没落库。跑 "
              "tools/backfill_kbar_verify.py 回填")
    elif not verdicts:
        print("  表 6 里没有这一根 —— 跑 tools/backfill_kbar_verify.py 补齐")
    else:
        for v in verdicts:
            print("  后面 %d 根 → 【%s】" % (v["horizon"], v["verdict"]))
            print("      当时的话：%s" % v["pred_text"])
            if v["actual_price"] is not None and v["ref_price"] is not None:
                print("      实际：后面最低/最高 %.1f，针对的价位 %.1f，差 %+.1f 点"
                      "（容差 %.0f）"
                      % (v["actual_price"], v["ref_price"], v["diff_pts"],
                         v["tol_pts"]))
            elif v["actual_price"] is not None:
                print("      实际：最大偏离 %.1f 点（这句话的尺是「1 倍本根全幅」）"
                      % v["actual_price"])
            if v["verdict"] == "无法判定":
                print("      ← 表尾凑不满 %d 根，**没验**，不是「成立」"
                      % v["horizon"])
        hs = set(v["verify_hash"] for v in verdicts)
        print("  本层口径   容差 %.0f 点（不是 after_pin 那个 20）  指纹 %s"
              % (verdicts[0]["tol_pts"], ",".join(sorted(hs))))
        print("  ⚠ 命中率不能单独看 —— 必须和同口径基准并排，见 "
              "tools/probe_kbar_verify.py")

    print("\n  OHLC  O=%.1f  H=%.1f  L=%.1f  C=%.1f   （直接取自表 1）" % (o, h, l, c))
    print("  上一根 %s (UTC) —— 判「碰到」只许用这一根的线价" % fmt_utc(r["prev_ts"]))
    print("  上一根上有 %d 条线" % r["prev_rows"])

    print("\n【表 1 · 事实层】")
    print("  区间       range_ts_h=%s  fam=%s seg=%s seg_idx=%s direction=%s"
          % (fmt_utc(bar["range_ts_h"]) if bar["range_ts_h"] else "-",
             bar["fam"], bar["seg"], bar["seg_idx"], bar["direction"]))
    print("  空间       zone=%s" % bar["zone"])
    print("  结构       dir=%s  body=%.4f  up_wick=%.4f  dn_wick=%.4f  close_pos=%.4f"
          % (bar["dir"], bar["body_pct"], bar["up_wick_pct"], bar["dn_wick_pct"],
             bar["close_pos"]))
    print("  量能       vol=%s  vol_rel=%s  vol_slot_rel=%s  taker_buy=%s  conc=%s"
          % (bar["vol"], bar["vol_rel"], bar["vol_slot_rel"], bar["taker_buy_ratio"],
             bar["conc"]))
    print("  内路径     t_low_min=%s  t_high_min=%s  path_shape=%s  max_tick_share=%s"
          % (bar["t_low_min"], bar["t_high_min"], bar["path_shape"],
             bar["max_tick_share"]))
    print("  观察线     n_lines=%s  n_touch=%s  n_break=%s"
          % (bar["n_lines"], bar["n_touch"], bar["n_break"]))
    print("  事件       event=%s  on_line=%s  prev_event=%s  since_idx=%s  state=%s"
          % (bar["event"], bar["on_line"], bar["prev_event"],
             bar["since_event_idx"], bar["state"]))

    print("\n【表 2 · 反应层】这一根碰到了 %d 条线" % len(db_touch))
    if not db_touch:
        print("  （一条都没碰到：没有线的线价落在 [L=%.1f, H=%.1f] 之间）" % (l, h))
    else:
        print("  %-18s %-10s %-10s %-11s %-10s %-10s %-7s %s"
              % ("线", "线价", "距离%", "从哪侧来", "穿透", "收回", "收盘侧", "第几次"))
        for k in sorted(db_touch, key=lambda x: abs(db_touch[x]["dist_pct"])):
            d = db_touch[k]
            print("  %-18s %-10.2f %+9.4f%% %-11s %-10.6f %-10.6f %-7s %s"
                  % (k, d["p"], d["dist_pct"], d["side"], d["pen"], d["rec"],
                     d["close_side"], d["nth"]))

    print("\n【表 2.5 · 事件层（权威）】%d 条" % len(db_ev))
    if not db_ev:
        print("  （无事件：收盘相对任何一条线都没换边）")
    else:
        for k in sorted(db_ev):
            e = db_ev[k]
            print("  %-18s 线价=%-10.2f %-14s close_side=%-6s"
                  % (k, e["p"], e["event"], e["close_side"]))

    print("\n【表 3 · 事后层】")
    print("  ret1=%s  ret3=%s  ret5=%s"
          % tuple(("%+.6f" % fw["ret%d" % k]) if fw["ret%d" % k] is not None else "None"
                  for k in (1, 3, 5)))
    print("  held=%s  held_line=%s  held_ref_price=%s  held_side=%s"
          % (fw["held"], fw["held_line"], fw["held_ref_price"], fw["held_side"]))
    if fw["held"] is None:
        print("  （held 空：这根没有事件 → 「守住」没有主语；或后面不足 3 根）")

    print("\n【独立重算 vs 库】")
    print("  重算触碰 %d 条 / 库里 %d 条" % (len(r["touch"]), len(db_touch)))
    print("  重算换边 %d 条 / 库里事件 %d 条" % (len(r["events"]), len(db_ev)))
    print("  重算 ret1=%s  ret3=%s  ret5=%s"
          % tuple(("%+.6f" % r["forward"]["ret%d" % k])
                  if r["forward"]["ret%d" % k] is not None else "None"
                  for k in (1, 3, 5)))
    if problems:
        print("\n  >>> 发现 %d 处不一致：" % len(problems))
        for p in problems:
            print("      - " + p)
    else:
        print("\n  >>> 一致")

    if a.chain:
        n = a.chain
        print("\n【前后各 %d 根】" % n)
        print("  %-16s %-16s %-10s %-10s %-7s %-14s %-5s %-16s %s"
              % ("UTC", "北京", "O", "C", "n_touch", "event", "held", "on_line", "state"))
        for row in con.execute(
                "select v.ts, v.o, v.c, v.n_touch, v.event, f.held, v.on_line, v.state "
                "from kbar_evolution v left join kbar_forward f "
                "  on f.symbol=v.symbol and f.interval_ms=v.interval_ms and f.ts=v.ts "
                "where v.symbol=? and v.interval_ms=? and v.ts>=? and v.ts<=? "
                "order by v.ts", (sym, iv, ts - n * iv, ts + n * iv)):
            mark = "  <<<" if row[0] == ts else ""
            print("  %-16s %-16s %-10.1f %-10.1f %-7s %-14s %-5s %-16s %s%s"
                  % (fmt_utc(row[0]), fmt_bj(row[0]), row[1], row[2], row[3],
                     row[4] or "-", "-" if row[5] is None else row[5],
                     row[6] or "-", row[7] or "-", mark))

    con.close()
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
