# -*- coding: utf-8 -*-
"""K 线演化的**只读**接口（回看模式）。

为什么单独一个模块，不复用 `ai_trader.kbar.store.KBarStore`
------------------------------------------------------------
`KBarStore.__init__` 会 `os.makedirs` → `PRAGMA journal_mode=WAL` →
`executescript(SCHEMA)` → `_migrate()` → `commit`。**它是写库的。**
给核心存储层加一个 readonly 开关，要动的是被 1436 条测试钉着的东西，
收益不抵风险。

所以 Web 侧自己开 `sqlite3.connect("file:...?mode=ro", uri=True)`：

  1. 保证 Web 进程**永远不可能**写 kbar.db —— 组里那条「只读生产库」的红线
     在这里往前推一步：连自己的库也只读；
  2. 请求路径上没有 DDL、没有 WAL 切换，快。

代价是几条 SELECT 与 store.py 重复。但这些都是「按 (symbol, interval_ms, ts)
取行」，**没有业务判断**，不会分叉出两套口径 —— 真正会分叉的是阈值和判据，
那些全在 shape.py / dims.py 里，本模块一行都没有。

⚠ 前视红线（这个模块最容易犯的错）
----------------------------------
表 6（kbar_verify）和表 6b（kbar_verify_survival）**是看未来的**：
「当时那句话后来对不对」「预言守住了几根」。

回看视图里可以展示，但必须和「当时能看到的」在**数据结构上**分开：
`then` = 当时可见（结论词、状态、维度、旁证、碰到的线）；
`post` = 事后才知道（预言判决、存活结局、ret1/3/5）。

前端也必须分开摆并标注「事后」。混在一起，这个页面本身就成了前视偏差的入口
—— 看图的人会记成「当时我就知道」。
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from flask import Blueprint, current_app, jsonify, request

from .auth import login_required
from ..kbar import ledger as LG

logger = logging.getLogger(__name__)

kbar_bp = Blueprint("kbar", __name__, url_prefix="/admin/api/kbar")

# 周期名 → 毫秒。**只放这里验证过的两个周期**：kbar.db 里确实有数据的那两个。
# 加周期不是改这一行就行 —— 表 0~7 都要先按那个周期跑出来。
KBAR_INTERVALS: Dict[str, int] = {"5m": 300_000, "15m": 900_000}

# 「无」三个词的配色在前端；这里只给顺序，不重复定义颜色。
DEFAULT_LIMIT = 800
MAX_LIMIT = 4000

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))


def _db_path() -> str:
    """kbar.db 的路径。允许 app.config 覆盖（测试就靠这个指到临时库）。"""
    p = current_app.config.get("KBAR_DB")
    if p:
        return str(p)
    return os.path.join(_ROOT, "data", "kbar.db")


def _ro() -> sqlite3.Connection:
    """只读连接。库不存在时由调用方转成 503，不在这里抛。"""
    p = _db_path()
    if not os.path.exists(p):
        raise FileNotFoundError(p)
    return sqlite3.connect("file:%s?mode=ro" % p.replace("\\", "/"), uri=True)


def _rows(con: sqlite3.Connection, sql: str, args: Tuple) -> List[Dict]:
    cur = con.execute(sql, args)
    keys = [d[0] for d in cur.description]
    return [dict(zip(keys, r)) for r in cur]


def _freshness() -> Optional[Dict]:
    """读 tools/kbar_freshness.py 写的健康快照。

    为什么页面要显示这个：2026-11-10 → 11-16，kbar.db 静默停摆 6.5 天，
    页面照常渲染，只是画的是旧数据 —— **沉默的失效比报错危险得多**。
    把「落后 N 小时」摆到页面上，是让这种停摆当场可见的最后一道。

    读不到就返回 None（不是报错）：这一层没跑过 timer 时不该让整页挂掉。
    """
    p = current_app.config.get("KBAR_HEALTH") or os.path.join(
        _ROOT, "data", "kbar_health.json")
    try:
        with open(str(p), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _sym() -> str:
    return request.args.get("symbol") or current_app.config.get(
        "KBAR_SYMBOL") or "BTCUSDT"


def _iv() -> Optional[int]:
    name = (request.args.get("interval") or "15m").strip()
    if name in KBAR_INTERVALS:
        return KBAR_INTERVALS[name]
    if name.isdigit() and int(name) in KBAR_INTERVALS.values():
        return int(name)
    return None


def _bad_interval(name: str):
    return jsonify({"ok": False, "error": "bad interval: %r" % name,
                    "allowed": sorted(KBAR_INTERVALS)}), 400


def _dumps(v) -> Any:
    """SQLite 里的 JSON 文本 → 对象。坏 JSON 不吞掉，返回 None 让前端看得见空。"""
    if not v:
        return None
    try:
        return json.loads(v)
    except ValueError:
        return None


# ---------------------------------------------------------------- 端点
@kbar_bp.get("/meta")
@login_required
def meta():
    """这个周期在库里有什么：覆盖范围、行数、口径指纹。

    先给这个端点，是因为图上任何一个空块都要能回答「是没数据，还是没算」。
    """
    sym = _sym()
    iv = _iv()
    if iv is None:
        return _bad_interval(request.args.get("interval", ""))
    name = next(k for k, v in KBAR_INTERVALS.items() if v == iv)
    fresh = _freshness()
    try:
        con = _ro()
    except FileNotFoundError as e:
        return jsonify({"ok": False, "error": "kbar.db 不存在: %s" % e,
                        "freshness": fresh}), 503
    try:
        row = con.execute(
            "SELECT COUNT(*), MIN(ts), MAX(ts) FROM kbar_evolution "
            "WHERE symbol=? AND interval_ms=? AND o IS NOT NULL",
            (sym, iv)).fetchone()
        n_shape = con.execute(
            "SELECT COUNT(*) FROM kbar_shape WHERE symbol=? AND interval_ms=?",
            (sym, iv)).fetchone()[0]
        hashes = [r[0] for r in con.execute(
            "SELECT DISTINCT dims_hash FROM kbar_dims "
            "WHERE symbol=? AND interval_ms=?", (sym, iv))]
        params = [r[0] for r in con.execute(
            "SELECT DISTINCT params_hash FROM kbar_shape "
            "WHERE symbol=? AND interval_ms=?", (sym, iv))]
        labels = [{"label": r[0], "n": r[1]} for r in con.execute(
            "SELECT label, COUNT(*) FROM kbar_shape "
            "WHERE symbol=? AND interval_ms=? GROUP BY label "
            "ORDER BY COUNT(*) DESC", (sym, iv))]
    finally:
        con.close()
    if not row[0]:
        return jsonify({"ok": True, "symbol": sym, "interval": name,
                        "interval_ms": iv, "empty": True,
                        "freshness": fresh,
                        "hint": "该周期暂无数据（先跑回填）"})
    return jsonify({
        "ok": True, "symbol": sym, "interval": name, "interval_ms": iv,
        "empty": False,
        "freshness": fresh,
        "bars": row[0], "first_ts": row[1], "last_ts": row[2],
        "shape_rows": n_shape,
        "dims_hash": hashes[0] if len(hashes) == 1 else hashes,
        "params_hash": params[0] if len(params) == 1 else params,
        "labels": labels,
        "n_dims": len(LG.dim_catalog()),
        "judge_dims": sorted(LG.judge_dim_names()),
    })


@kbar_bp.get("/evo")
@login_required
def evo():
    """画图三件套一次给齐：K 线、结论词（副图色带）、观察线反应（可选叠加）。

    为什么合成一个端点：这三者必须落在同一个 ts 上才有意义。
    分三次请求，前端就得自己对齐时间轴 —— 对齐错了会静默画歪。
    """
    sym = _sym()
    iv = _iv()
    if iv is None:
        return _bad_interval(request.args.get("interval", ""))
    name = next(k for k, v in KBAR_INTERVALS.items() if v == iv)
    try:
        limit = int(request.args.get("limit", DEFAULT_LIMIT))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "limit 必须是整数"}), 400
    limit = max(50, min(limit, MAX_LIMIT))
    with_rx = request.args.get("with_reactions", "1") not in ("0", "false", "")

    try:
        con = _ro()
    except FileNotFoundError as e:
        return jsonify({"ok": False, "error": "kbar.db 不存在: %s" % e}), 503
    try:
        # 先取窗口内最后一根，再按 ts 倒序取 limit 根 —— 回看模式的"最新"
        # 是**库里的最后一根**，不是墙钟。图上必须能看出数据到哪天为止。
        last = con.execute(
            "SELECT MAX(ts) FROM kbar_evolution "
            "WHERE symbol=? AND interval_ms=? AND o IS NOT NULL",
            (sym, iv)).fetchone()[0]
        if last is None:
            return jsonify({"ok": True, "symbol": sym, "interval": name,
                            "interval_ms": iv, "empty": True,
                            "bars": [], "label": [], "states": [],
                            "n_touch": [], "status": [], "rx": [],
                            "hint": "该周期暂无数据（先跑回填）"})
        evo = _rows(con, "SELECT ts,o,h,l,c,vol,vol_rel,taker_buy_ratio,"
                         "zone,direction,n_lines,n_touch,event,state "
                         "FROM kbar_evolution WHERE symbol=? AND interval_ms=? "
                         "AND o IS NOT NULL AND ts<=? ORDER BY ts DESC LIMIT ?",
                    (sym, iv, last, limit))
        evo.reverse()
        ts_list = [int(r["ts"]) for r in evo]

        # 结论词：按 ts 取，缺了就是 None（不补默认词 —— 补了就是把"没算"
        # 说成"无"，而"无"本身是一个结论）
        shape = {int(r["ts"]): r for r in _rows(
            con, "SELECT ts,label,states,n_states,side,pin_kind,evidence,"
                 "n_recovered FROM kbar_shape WHERE symbol=? AND interval_ms=? "
                 "AND ts>=? ORDER BY ts", (sym, iv, ts_list[0]))}
        # 存活结局：**事后**信息，单独一列，前端要标灰
        surv = {int(r["ts"]): r for r in _rows(
            con, "SELECT ts,status,must_hold,break_n,n_seen,max_n "
                 "FROM kbar_verify_survival WHERE symbol=? AND interval_ms=? "
                 "AND ts>=? ORDER BY ts", (sym, iv, ts_list[0]))}

        rx: List[List] = []
        if with_rx:
            for r in _rows(
                    con, "SELECT ts,line_key,line_price,side,close_side "
                         "FROM kbar_reaction WHERE symbol=? AND interval_ms=? "
                         "AND ts>=? ORDER BY ts,line_key",
                    (sym, iv, ts_list[0])):
                rx.append([int(r["ts"]), r["line_key"],
                           float(r["line_price"]), r["side"],
                           r["close_side"]])

        dist = [{"label": r[0], "n": r[1]} for r in con.execute(
            "SELECT label, COUNT(*) FROM kbar_shape WHERE symbol=? "
            "AND interval_ms=? GROUP BY label ORDER BY COUNT(*) DESC",
            (sym, iv))]
    finally:
        con.close()

    out_bars, out_label, out_states, out_ntouch, out_status = [], [], [], [], []
    for r in evo:
        ts = int(r["ts"])
        s = shape.get(ts)
        out_bars.append([ts, r["o"], r["c"], r["l"], r["h"]])
        out_label.append(s["label"] if s else None)
        out_states.append(s["states"] if s else None)
        out_ntouch.append(r["n_touch"])
        g = surv.get(ts)
        out_status.append(g["status"] if g else None)

    return jsonify({
        "ok": True, "symbol": sym, "interval": name, "interval_ms": iv,
        "empty": False,
        "last_ts": last,
        "window": {"n": len(evo), "first_ts": ts_list[0], "last_ts": last},
        # 与 _chart_js 同序：ts,o,c,l,h —— 前端两套图共用一份解析代码
        "bars": out_bars,
        "label": out_label,
        "states": out_states,
        "n_touch": out_ntouch,
        "status": out_status,
        "rx": rx,
        "label_dist": dist,
    })


@kbar_bp.get("/snapshot")
@login_required
def snapshot():
    """某一根 K 线的完整演化快照 —— **then（当时可见）与 post（事后）分开给**。

    这一个端点是整个回看视图里最容易埋前视偏差的地方，所以结构上就劈成两半：
      then.label / states / dims / notes / reactions  → 当时就能算出来
      post.preds / survival / forward                → 事后才知道
    前端必须分成两块、post 标「事后」。谁把 post 的东西摆进 then，
    这个页面就从"复盘"变成"偷看答案"。
    """
    sym = _sym()
    iv = _iv()
    if iv is None:
        return _bad_interval(request.args.get("interval", ""))
    name = next(k for k, v in KBAR_INTERVALS.items() if v == iv)
    try:
        ts = int(request.args.get("ts", ""))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "ts 必须是毫秒整数"}), 400

    try:
        con = _ro()
    except FileNotFoundError as e:
        return jsonify({"ok": False, "error": "kbar.db 不存在: %s" % e}), 503
    try:
        bar = con.execute(
            "SELECT ts,o,h,l,c,vol,vol_rel,vol_slot_rel,taker_buy_ratio,"
            "range_ts_h,fam,seg,seg_idx,direction,zone,dir,body_pct,"
            "up_wick_pct,dn_wick_pct,close_pos,n_lines,n_touch,n_break,"
            "event,prev_event,on_line,since_event_idx,state "
            "FROM kbar_evolution WHERE symbol=? AND interval_ms=? AND ts=?",
            (sym, iv, ts)).fetchone()
        if bar is None:
            return jsonify({"ok": False, "error": "该 ts 没有这一根",
                            "ts": ts, "interval": name}), 404
        bcols = ["ts", "o", "h", "l", "c", "vol", "vol_rel", "vol_slot_rel",
                 "taker_buy_ratio", "range_ts_h", "fam", "seg", "seg_idx",
                 "direction", "zone", "dir", "body_pct", "up_wick_pct",
                 "dn_wick_pct", "close_pos", "n_lines", "n_touch", "n_break",
                 "event", "prev_event", "on_line", "since_event_idx", "state"]
        bar_d = dict(zip(bcols, bar))

        sh = con.execute(
            "SELECT label,states,n_states,side,pin_kind,ref_pin_ts,"
            "ref_pin_kind,ref_pin_side,rng,body_pct,up_wick_pct,dn_wick_pct,"
            "close_pos,vol_rel,taker_buy_ratio,n_recovered,recovered,evidence,"
            "params_hash FROM kbar_shape WHERE symbol=? AND interval_ms=? "
            "AND ts=?", (sym, iv, ts)).fetchone()
        shape = None
        if sh:
            shape = dict(zip(
                ["label", "states", "n_states", "side", "pin_kind",
                 "ref_pin_ts", "ref_pin_kind", "ref_pin_side", "rng",
                 "body_pct", "up_wick_pct", "dn_wick_pct", "close_pos",
                 "vol_rel", "taker_buy_ratio", "n_recovered", "recovered",
                 "evidence", "params_hash"], sh))

        dm = con.execute(
            "SELECT n_dims,n_filled,values_json,buckets_json,notes_json,"
            "dims_hash FROM kbar_dims WHERE symbol=? AND interval_ms=? AND ts=?",
            (sym, iv, ts)).fetchone()
        dims = None
        if dm:
            dims = {"n_dims": dm[0], "n_filled": dm[1],
                    "values": _dumps(dm[2]), "buckets": _dumps(dm[3]),
                    "notes": _dumps(dm[4]) or [], "dims_hash": dm[5]}

        rxs = _rows(con, "SELECT line_key,line_kind,line_period_ms,line_price,"
                         "dist_pct,side,pen_depth_pct,recover_pct,close_side,"
                         "nth_touch FROM kbar_reaction WHERE symbol=? AND "
                         "interval_ms=? AND ts=? ORDER BY line_key",
                    (sym, iv, ts))
        evs = _rows(con, "SELECT line_key,event,prev_event,close_side,"
                         "pen_depth_pct FROM kbar_event WHERE symbol=? AND "
                         "interval_ms=? AND ts=? ORDER BY line_key",
                    (sym, iv, ts))

        # ---- 以下全是 post：看未来的，前端必须单列 ----
        preds = _rows(con, "SELECT horizon,pred_kind,pred_text,verdict,"
                           "ref_price,actual_price,diff_pts,n_bars,tol_pts "
                           "FROM kbar_verify WHERE symbol=? AND interval_ms=? "
                           "AND ts=? ORDER BY horizon", (sym, iv, ts))
        sv = con.execute(
            "SELECT label,pred_kind,must_hold,status,break_n,n_seen,max_n,"
            "ref_price,actual_at_break,tol_pts FROM kbar_verify_survival "
            "WHERE symbol=? AND interval_ms=? AND ts=?",
            (sym, iv, ts)).fetchone()
        survival = None
        if sv:
            survival = dict(zip(["label", "pred_kind", "must_hold", "status",
                                 "break_n", "n_seen", "max_n", "ref_price",
                                 "actual_at_break", "tol_pts"], sv))
        fw = con.execute(
            "SELECT ret1,ret3,ret5,held,held_line,held_ref_price,held_side "
            "FROM kbar_forward WHERE symbol=? AND interval_ms=? AND ts=?",
            (sym, iv, ts)).fetchone()
        forward = None
        if fw:
            forward = dict(zip(["ret1", "ret3", "ret5", "held", "held_line",
                                "held_ref_price", "held_side"], fw))
    finally:
        con.close()

    return jsonify({
        "ok": True, "symbol": sym, "interval": name, "interval_ms": iv,
        "ts": ts,
        "bar": bar_d,
        "then": {
            "shape": shape,
            "dims": dims,
            "reactions": rxs,
            "events": evs,
        },
        "post": {
            "preds": preds,
            "survival": survival,
            "forward": forward,
        },
        # 39 个维度的静态说明 + 哪 8 个是判据（从 shape.py AST 求，
        # 前端不许自己写这份字面量 —— 见 ai_trader/kbar/ledger.py）
        "dim_catalog": LG.dim_catalog(),
    })
