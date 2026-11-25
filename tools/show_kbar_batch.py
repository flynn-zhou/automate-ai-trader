# -*- coding: utf-8 -*-
"""把若干根 K 线的**演化结论字段**逐根列开，供人逐条核对。

用户 2026-11-08：
    「把我指定的那 6 个小时的 k 线演化结果按照每次的结论字段列开给我，
      我来检查下你的演化结果结论如何」

和 `show_kbar.py` 的分工：
    `show_kbar.py`   一根、全量、带独立校验（排查用）
    `show_kbar_batch` 多根、只列结论字段（验收用）

**当时 / 事后 分开两条线，绝不混在一张表里**（用户同日的明确要求）：

    表 5 `kbar_shape`   当时的判定 —— 只用 `[0..t]` 的数据
    表 6 `kbar_verify`  事后的判定 —— 用 `t+1..t+N` 回头验那句话

本工具**只读**，且不 import shape / verify 的任何判定函数：它打印的是库里
落的值，不重算。要「库里的值对不对」用 `show_kbar.py --verify`。

用法：
    python tools/show_kbar_batch.py --at "2026-11-04 10:00" --at "2026-11-04 10:15" ...
    python tools/show_kbar_batch.py --preset anchor6      # 用户点名的 6 根
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sqlite3
import sys

DEFAULT_KBAR_DB = os.path.join("data", "kbar.db")
DEFAULT_SYMBOL = "BTCUSDT"
INTERVAL_MS = 900_000

# 用户 2026-11-08 点名要看的 6 根（北京时间）
PRESET_ANCHOR6 = [
    "2026-11-04 10:00",
    "2026-11-04 10:15",
    "2026-11-04 10:30",
    "2026-11-04 10:45",
    "2026-11-04 11:15",
    "2026-11-04 11:30",
]

STATE_ORDER = ("恐慌盘", "插针收回", "插针", "插针后稳住", "针后远离")


def parse_at(s: str, tz_hours: float) -> int:
    dt = datetime.datetime.strptime(s.strip(), "%Y-%m-%d %H:%M")
    epoch = datetime.datetime(1970, 1, 1)
    return int((dt - epoch).total_seconds() * 1000) - int(tz_hours * 3600 * 1000)


def fmt_utc(ms):
    return datetime.datetime.utcfromtimestamp(ms / 1000.0).strftime(
        "%Y-%m-%d %H:%M")


def fmt_bj(ms):
    return datetime.datetime.utcfromtimestamp(
        (ms + 8 * 3600 * 1000) / 1000.0).strftime("%m-%d %H:%M")


def hr(t):
    print("\n" + "=" * 92)
    print(t)
    print("=" * 92)


def one(con, sql, *a):
    r = con.execute(sql, a).fetchone()
    return r[0] if r else None


def load_bar(con, ts, sym, iv):
    row = con.execute(
        "select o,h,l,c,range_ts_h,fam,seg,seg_idx,direction,zone,dir,"
        "       body_pct,up_wick_pct,dn_wick_pct,close_pos,vol,vol_rel,"
        "       vol_slot_rel,taker_buy_ratio,conc,t_low_min,t_high_min,"
        "       n_lines,n_touch,event,prev_event,on_line,since_event_idx,state "
        "from kbar_evolution where symbol=? and interval_ms=? and ts=?",
        (sym, iv, ts)).fetchone()
    if row is None:
        return None
    keys = ("o", "h", "l", "c", "range_ts_h", "fam", "seg", "seg_idx",
            "direction", "zone", "dir", "body_pct", "up_wick_pct", "dn_wick_pct",
            "close_pos", "vol", "vol_rel", "vol_slot_rel", "taker_buy_ratio",
            "conc", "t_low_min", "t_high_min", "n_lines", "n_touch", "event",
            "prev_event", "on_line", "since_event_idx", "state")
    return dict(zip(keys, row))


def load_shape(con, ts, sym, iv):
    row = con.execute(
        "select label,states,n_states,side,pin_kind,ref_pin_ts,ref_pin_kind,"
        "       ref_pin_side,rng,body_pct,up_wick_pct,dn_wick_pct,close_pos,"
        "       vol_rel,taker_buy_ratio,n_recovered,recovered,evidence,"
        "       params_hash from kbar_shape "
        "where symbol=? and interval_ms=? and ts=?", (sym, iv, ts)).fetchone()
    if row is None:
        return None
    keys = ("label", "states", "n_states", "side", "pin_kind", "ref_pin_ts",
            "ref_pin_kind", "ref_pin_side", "rng", "body_pct", "up_wick_pct",
            "dn_wick_pct", "close_pos", "vol_rel", "taker_buy_ratio",
            "n_recovered", "recovered", "evidence", "params_hash")
    d = dict(zip(keys, row))
    d["state_list"] = [x for x in (d["states"] or "").split(",") if x]
    d["evidence_lines"] = [x for x in (d["evidence"] or "").split("\n") if x]
    return d


def load_verify(con, ts, sym, iv):
    cur = con.execute(
        "select horizon,label,states,pred_kind,pred_text,verdict,ref_price,"
        "       actual_price,diff_pts,n_bars,tol_pts,params_hash,verify_hash "
        "from kbar_verify where symbol=? and interval_ms=? and ts=? "
        "order by horizon", (sym, iv, ts))
    keys = ("horizon", "label", "states", "pred_kind", "pred_text", "verdict",
            "ref_price", "actual_price", "diff_pts", "n_bars", "tol_pts",
            "params_hash", "verify_hash")
    return [dict(zip(keys, r)) for r in cur]


def has_table(con, name):
    return one(con, "select count(*) from sqlite_master where type='table' "
                    "and name=?", name) > 0


# 数值维度的打印精度。价格给 1 位，比例给 4 位有效小数，
# 其余（成交笔数、周期毫秒、时间戳）原样。人读的输出不该是 0.2089640842980408。
_DIM_PRICE = {"o", "h", "l", "c", "line_price"}
_DIM_PCT = {"body_pct", "up_wick_pct", "dn_wick_pct", "close_pos",
            "taker_buy_ratio", "conc", "vol_rel", "vol_slot_rel",
            "pen_depth_pct", "recover_pct", "dist_pct"}


def _fmt_dim(name, val):
    if isinstance(val, float):
        if name in _DIM_PRICE:
            return "%.1f" % val
        if name in _DIM_PCT:
            return "%.4f" % val
        return "%.2f" % val
    return str(val)


def load_dims(con, ts, sym, iv):
    if not has_table(con, "kbar_dims"):
        return None
    cur = con.execute(
        "select label,n_dims,n_filled,values_json,buckets_json,notes_json,"
        "       dims_hash from kbar_dims "
        "where symbol=? and interval_ms=? and ts=?", (sym, iv, ts))
    r = cur.fetchone()
    if not r:
        return None
    return {"label": r[0], "n_dims": r[1], "n_filled": r[2],
            "values": json.loads(r[3]), "buckets": json.loads(r[4]),
            "notes": json.loads(r[5]), "dims_hash": r[6]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="把若干根 K 线的演化结论字段逐根列开（只读，不重算）")
    ap.add_argument("--at", action="append", default=[],
                    help="'YYYY-MM-DD HH:MM'，可重复")
    ap.add_argument("--preset", default=None, choices=["anchor6"],
                    help="anchor6 = 用户点名的 6 根")
    ap.add_argument("--tz", default="bj", choices=["bj", "utc"])
    ap.add_argument("--kbar-db", default=DEFAULT_KBAR_DB)
    ap.add_argument("--symbol", default=DEFAULT_SYMBOL)
    ap.add_argument("--interval", type=int, default=INTERVAL_MS)
    a = ap.parse_args(argv)

    ats = list(a.at)
    if a.preset:
        ats = list(PRESET_ANCHOR6) + ats
    if not ats:
        ats = list(PRESET_ANCHOR6)
    tz = 8.0 if a.tz == "bj" else 0.0

    if not os.path.exists(a.kbar_db):
        raise SystemExit("找不到 %s" % a.kbar_db)
    con = sqlite3.connect("file:%s?mode=ro" % a.kbar_db, uri=True)
    try:
        has5 = has_table(con, "kbar_shape")
        has6 = has_table(con, "kbar_verify")
        total = one(con, "select count(*) from kbar_evolution where symbol=? "
                         "and interval_ms=? and o is not null",
                    a.symbol, a.interval)
        hr("0. 库与口径")
        print("  库        %s（只读打开）" % a.kbar_db)
        print("  品种/周期 %s @ %d ms" % (a.symbol, a.interval))
        print("  可查的根  %s 根（表 1，有 OHLC 的）" % "{:,}".format(total or 0))
        print("  表 5 当时判定  %s" % ("在" if has5 else "**没有**"))
        print("  表 6 事后判定  %s" % ("在" if has6 else "**没有**"))
        print("  时区      %s" % ("北京时间（UTC+8）" if tz else "UTC"))

        rows = []
        missing = []
        for s in ats:
            ts = parse_at(s, tz)
            bar = load_bar(con, ts, a.symbol, a.interval)
            if bar is None:
                missing.append(s)
                continue
            rows.append({
                "at": s, "ts": ts, "bar": bar,
                "shape": load_shape(con, ts, a.symbol, a.interval) if has5 else None,
                "verify": load_verify(con, ts, a.symbol, a.interval) if has6 else None,
                # 表 7：39 个维度的环境快照。缺表就是 None，不假装有。
                "dims": load_dims(con, ts, a.symbol, a.interval),
            })
        if missing:
            print("\n  ⚠ 库覆盖不到：%s" % "、".join(missing))
            lo = one(con, "select min(ts) from kbar_evolution where symbol=? "
                          "and interval_ms=? and o is not null",
                     a.symbol, a.interval)
            hi = one(con, "select max(ts) from kbar_evolution where symbol=? "
                          "and interval_ms=? and o is not null",
                     a.symbol, a.interval)
            if lo is None:
                print("    这个品种/周期在库里一根都没有")
            else:
                print("    库覆盖 %s .. %s（北京）"
                      % (fmt_bj(lo), fmt_bj(hi)))

        # ---------------- 一、汇总表 ----------------
        hr("1. 当时判定汇总（表 5 `kbar_shape`，只用 [0..t] 的数据）")
        hdr = ("北京时间", "UTC", "label（结论）", "同时成立的状态",
               "自己是针", "稳的是哪根", "参照方向")
        print("  %-12s %-13s %-10s %-22s %-12s %-16s %-6s" % hdr)
        for r in rows:
            sh = r["shape"]
            if sh is None:
                print("  %-12s %-13s %s" % (fmt_bj(r["ts"]), fmt_utc(r["ts"]),
                                            "（表 5 里没有这一根）"))
                continue
            selfpin = "%s/%s" % (sh["side"] or "-", sh["pin_kind"] or "-")
            ref = ("%s %s" % (fmt_bj(sh["ref_pin_ts"]), sh["ref_pin_kind"] or "")
                   if sh["ref_pin_ts"] is not None else "-")
            print("  %-12s %-13s %-10s %-22s %-12s %-16s %-6s"
                  % (fmt_bj(r["ts"]), fmt_utc(r["ts"]), sh["label"],
                     "、".join(sh["state_list"]) or "（空）", selfpin, ref,
                     sh["ref_pin_side"] or "-"))

        # ---------------- 二、逐根字段 ----------------
        for r in rows:
            sh, bar = r["shape"], r["bar"]
            hr("2. %s（北京） / %s（UTC） ts=%d"
               % (fmt_bj(r["ts"]), fmt_utc(r["ts"]), r["ts"]))
            if sh is None:
                print("  表 5 里没有这一根 —— 先跑 tools/backfill_kbar_shape.py")
                continue
            print("  【结论】%s" % sh["label"])
            print("  【同时成立】%s（共 %d 个）"
                  % ("、".join(sh["state_list"]) or "（空，即「无」）",
                     sh["n_states"]))
            print("  【本根自己是针】方向=%s  形态=%s"
                  % (sh["side"] or "-", sh["pin_kind"] or "-"))
            print("  【稳住/远离参照的针】%s"
                  % ("%s（北京） / %s（UTC）  形态=%s  方向=%s"
                     % (fmt_bj(sh["ref_pin_ts"]), fmt_utc(sh["ref_pin_ts"]),
                        sh["ref_pin_kind"] or "-", sh["ref_pin_side"] or "-")
                     if sh["ref_pin_ts"] is not None else "无（本根不参照针）"))
            print("  【结构】全幅=%.1f 点  实体=%.0f%%  下影=%.0f%%  上影=%.0f%%  收位=%.0f%%"
                  % (sh["rng"], 100 * (sh["body_pct"] or 0),
                     100 * (sh["dn_wick_pct"] or 0),
                     100 * (sh["up_wick_pct"] or 0),
                     100 * (sh["close_pos"] or 0)))
            vr = sh["vol_rel"]
            print("  【量能】vol_rel=%s  taker_buy=%s"
                  % ("%.2f×" % vr if vr is not None else "缺",
                     "%.2f" % sh["taker_buy_ratio"]
                     if sh["taker_buy_ratio"] is not None else "缺"))
            print("  【收回】%d 条%s" % (sh["n_recovered"],
                                       ("  " + sh["recovered"])
                                       if sh["recovered"] else ""))
            print("  【证据链】")
            for i, e in enumerate(sh["evidence_lines"], 1):
                print("    %d) %s" % (i, e))
            print("  【口径指纹】%s" % sh["params_hash"])
            print("  【表 1 上下文】zone=%s  fam=%s seg=%s idx=%s direction=%s"
                  % (bar["zone"], bar["fam"], bar["seg"], bar["seg_idx"],
                     bar["direction"]))
            print("  【表 1 事件】event=%s  on_line=%s  prev=%s  since=%s"
                  % (bar["event"] or "-", bar["on_line"] or "-",
                     bar["prev_event"] or "-", bar["since_event_idx"]))
            print("  【表 1 观察线】n_lines=%s  n_touch=%s"
                  % (bar["n_lines"], bar["n_touch"]))
            print("  【OHLC】O=%.1f H=%.1f L=%.1f C=%.1f"
                  % (bar["o"], bar["h"], bar["l"], bar["c"]))
            # ---- 表 7：39 个维度的环境快照（描述层，不参与判定）----
            dm = r["dims"]
            if dm is None:
                print("  【环境快照】表 7 里没有这一根 —— 跑 "
                      "tools/backfill_kbar_dims.py")
            else:
                print("  【环境快照】39 个维度里算出 %d 个有值（dims_hash=%s）"
                      % (dm["n_filled"], dm["dims_hash"]))
                # 按用户给的六大类分组打印，便于对读《维度总账》§三
                _GROUPS = (
                    ("价", ("o", "h", "l", "c", "dir", "body_pct",
                            "up_wick_pct", "dn_wick_pct", "close_pos")),
                    ("量", ("vol", "vol_rel", "vol_slot_rel",
                            "taker_buy_ratio", "conc")),
                    ("时", ("idx", "seg_idx", "n_touch", "since_event_idx",
                            "src_ts", "line_ready_ms")),
                    ("空", ("range_ts_h", "fam", "seg", "direction", "zone",
                            "t_low_min", "t_high_min", "n_lines",
                            "line_period_ms", "dist_pct")),
                    ("观察线的反应", ("line_price", "on_line", "line_key",
                                 "line_kind", "side", "pen_depth_pct",
                                 "recover_pct", "close_side")),
                    ("情绪", ("event", "prev_event", "state")),
                )
                for gname, gkeys in _GROUPS:
                    parts = []
                    for k in gkeys:
                        val = dm["values"].get(k)
                        if val is None:
                            continue
                        bk = dm["buckets"].get(k) or ""
                        parts.append("%s=%s%s"
                                     % (k, _fmt_dim(k, val),
                                        ("（%s）" % bk) if bk else ""))
                    if parts:
                        print("    %-8s %s" % (gname, "  ".join(parts)))
                    else:
                        print("    %-8s （这一根全缺）" % gname)
                if dm["notes"]:
                    print("  【旁证/矛盾】⚠ 只是提示，**不改上面的结论**")
                    for n in dm["notes"]:
                        print("    [%s] %s" % (n["kind"], n["text"]))
                else:
                    print("  【旁证/矛盾】（无提示）")

        # ---------------- 三、事后判定（单独一条线） ----------------
        hr("3. 事后判定（表 6 `kbar_verify`，用 t+1..t+N 回头验）")
        print("  ⚠ 这一段**不是**当时的判定，是事后拿后面的 K 线验它。两条线不混。")
        # 两种预言的尺不同，**不能并在一张表里**：
        #   价格型（插针/恐慌/稳住/远离）→ 参照是一个价位，差 = 实际 − 参照（点）
        #   全幅型（无）                  → 参照是「1 倍本根全幅」，没有价位，
        #                                   实际是最大偏离（点）。把点数印在
        #                                   「价位」列里，读的人会以为那是个价格。
        price_rows = [(r, v) for r in rows for v in (r["verify"] or [])
                      if v["ref_price"] is not None]
        ruler_rows = [(r, v) for r in rows for v in (r["verify"] or [])
                      if v["ref_price"] is None]

        print("\n  3.1 价格型预言（参照是一个价位）")
        print("  %-12s %-10s %-3s %-10s %-13s %-13s %-9s %s"
              % ("北京时间", "当时结论", "N", "裁决", "参照价位", "实际极值",
                 "差(点)", "后面实际怎么走"))
        if not price_rows:
            print("    （这 6 根没有价格型预言）")
        for r, v in price_rows:
            # ⚠ 不能拿「裁决==成立」去猜事实方向：**两类预言相反**。
            #   守住型（…守住）：没破才叫成立 → 差>0 是没破
            #   会破型（…会破）：破了才叫成立 → 差<0 才是破了
            # 按 pred_kind 判，不按裁决判 —— 否则「恐慌盘说会破、真的破了、
            # 裁决成立」会被写成「后面更靠近参照」，恰好反了。
            # 注意 pred_kind 落库是**中文**（「针低守住」），不是英文常量名。
            broke = v["diff_pts"] < 0
            side_word = "低点" if "低" in v["pred_kind"] else "高点"
            verb = "跌破" if "低" in v["pred_kind"] else "突破"
            if v["pred_kind"].endswith("守住"):
                fact = ("后面%s了%s（预言说不会）" % (verb, side_word) if broke
                        else "后面没%s%s（预言说不会）" % (verb, side_word))
            else:
                fact = ("后面%s了%s（预言说会）" % (verb, side_word) if broke
                        else "后面没%s%s（预言说会）" % (verb, side_word))
            print("  %-12s %-10s %-3d %-10s %-13.1f %-13.1f %+9.1f %s"
                  % (fmt_bj(r["ts"]), v["label"], v["horizon"], v["verdict"],
                     v["ref_price"], v["actual_price"], v["diff_pts"], fact))

        print("\n  3.2 全幅型预言（「无」专用：尺是 1 倍本根全幅，没有价位）")
        print("  %-12s %-10s %-3s %-10s %-12s %-13s %-11s %s"
              % ("北京时间", "当时结论", "N", "裁决", "尺(本根全幅)",
                 "实际最大偏离", "余量(点)", "含义"))
        if not ruler_rows:
            print("    （这 6 根没有全幅型预言）")
        for r, v in ruler_rows:
            # 库里的 diff_pts = 实际偏离 − 尺：负数代表「还在尺内」
            ruler = v["actual_price"] - v["diff_pts"]
            inside = v["diff_pts"] < 0
            print("  %-12s %-10s %-3d %-10s %-12.1f %-13.1f %+11.1f %s"
                  % (fmt_bj(r["ts"]), v["label"], v["horizon"], v["verdict"],
                     ruler, v["actual_price"], v["diff_pts"],
                     "没走出尺" if inside else "走出了尺"))

        for r, v in (price_rows + ruler_rows):
            if v["verdict"] == "无法判定":
                print("\n  ⚠ %s N=%d 是「无法判定」—— 表尾凑不满 %d 根，"
                      "**没验**，不是「成立」" % (fmt_bj(r["ts"]),
                                                 v["horizon"], v["horizon"]))

        hr("4. 当时的原话（表 6 的 pred_text）")
        for r in rows:
            for v in (r["verify"] or []):
                print("  %s  N=%d  【%s】" % (fmt_bj(r["ts"]), v["horizon"],
                                             v["verdict"]))
                print("      %s" % v["pred_text"])

        hr("5. 读法提醒")
        print("  · 表 5 与表 6 是两条线：5 说「这一根是什么」，6 说「那句话对不对」。")
        print("  · 「无法判定」= 表尾凑不满 N 根，**没验**，不是「成立」。")
        print("  · 命中率不许单独看，必须与同口径基准并排：tools/probe_kbar_verify.py")
        print("  · 本工具**不重算**，打印的就是库里落的值。要校验库值对不对，")
        print("    跑 tools/show_kbar.py --at <某根> --verify。")
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
