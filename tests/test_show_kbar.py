# -*- coding: utf-8 -*-
"""`tools/show_kbar.py` 的测试。

这个工具是「人工验证功能对不对」的入口，所以它自己必须可信：
  - 时区必须显式，不许猜
  - 校验必须能真的抓到不一致（否则它只是一张好看的报表）
"""
from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, "tools", "show_kbar.py")
IV = 900_000
# 必须先对齐到 15m 网格：1600000000000 是 12:26:40，不是任何一根 K 线的开盘时刻。
BASE = 1599999300000  # 2020-09-13 12:15:00 UTC
SYM = "BTCUSDT"


def _load():
    spec = importlib.util.spec_from_file_location("show_kbar_tool", TOOL)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["show_kbar_tool"] = mod
    spec.loader.exec_module(mod)
    return mod


T = _load()


# ---------------------------------------------------------------- 时区
def test_parse_at_reads_as_beijing_by_default_semantics():
    """同一串时间，按 UTC 与按北京解释必须差 8 小时（不是差一分钟）。"""
    utc = T.parse_at("2026-11-07 23:45", 0)
    bj = T.parse_at("2026-11-07 23:45", 8)
    assert utc - bj == 8 * 3600 * 1000
    assert T.fmt_bj(utc) == "2026-11-08 07:45"
    assert T.fmt_utc(bj) == "2026-11-07 15:45"


def test_parse_at_rejects_text_it_cannot_read():
    with pytest.raises(SystemExit):
        T.parse_at("11-07 23:45", 8)
    with pytest.raises(SystemExit):
        T.parse_at("2026/11/07 23:45", 8)


# ---------------------------------------------------------------- 夹具
def _make_db(path):
    con = sqlite3.connect(path)
    con.executescript("""
    CREATE TABLE kbar_line (symbol TEXT, interval_ms INT, ts INT, line_key TEXT,
        line_price REAL, bar_close REAL, dist_pct REAL);
    CREATE TABLE kbar_evolution (symbol TEXT, interval_ms INT, ts INT, o REAL, h REAL,
        l REAL, c REAL, range_ts_h INT, fam INT, seg INT, seg_idx INT, direction TEXT,
        zone TEXT, dir INT, body_pct REAL, up_wick_pct REAL, dn_wick_pct REAL,
        close_pos REAL, vol REAL, vol_rel REAL, vol_slot_rel REAL, taker_buy_ratio REAL,
        conc REAL, t_low_min INT, t_high_min INT, path_shape TEXT,
        max_tick_share REAL, n_lines INT, n_touch INT, n_break INT, event TEXT,
        on_line TEXT, prev_event TEXT, since_event_idx INT, state TEXT);
    CREATE TABLE kbar_reaction (symbol TEXT, interval_ms INT, ts INT, line_key TEXT,
        line_price REAL, dist_pct REAL, side TEXT, pen_depth_pct REAL,
        recover_pct REAL, close_side TEXT, nth_touch INT);
    CREATE TABLE kbar_event (symbol TEXT, interval_ms INT, ts INT, line_key TEXT,
        line_price REAL, event TEXT, prev_event TEXT, since_event_idx INT,
        close_side TEXT);
    CREATE TABLE kbar_forward (symbol TEXT, interval_ms INT, ts INT, ret1 REAL,
        ret3 REAL, ret5 REAL, held INT, held_line TEXT, held_ref_price REAL,
        held_side TEXT);
    """)
    return con


def _shape_table(con):
    """建表 5。列只放够比对的那几个 —— 这些测试的对象是「比对逻辑」，不是 DDL。"""
    con.executescript("""
    CREATE TABLE kbar_shape (symbol TEXT, interval_ms INT, ts INT, label TEXT,
        states TEXT, n_states INT, side TEXT, pin_kind TEXT, ref_pin_ts INT,
        ref_pin_kind TEXT, rng REAL, body_pct REAL, up_wick_pct REAL,
        dn_wick_pct REAL, close_pos REAL, vol_rel REAL, taker_buy_ratio REAL,
        n_recovered INT, recovered TEXT, evidence TEXT, params_hash TEXT);
    """)


def _shape_fill(con, n, label=None, states=None, evidence=None, params_hash=None):
    """按 shape 现算的结果写一行；label/states/evidence/fingerprint 可以故意改错。

    「故意改错」是这个夹具的用途：不给出错的可能，就没法证明 --verify 抓得住错。
    """
    from ai_trader.kbar import shape as S
    ts = BASE + n * IV
    jd, _ = T.judge_bar(con, ts, SYM, IV)
    assert jd is not None, "夹具造的这一根算不出判定，用例前提不成立"
    g = S.geometry(*[_bar_ohlc(con, n)[k] for k in ("o", "h", "l", "c")])
    con.execute(
        "insert into kbar_shape values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (SYM, IV, ts,
         label if label is not None else jd.label,
         (",".join(jd.states) if states is None else states),
         len(jd.states) if states is None else
         (0 if states == "" else len(states.split(","))),
         jd.side, jd.pin_kind, jd.pin_ts, jd.ref_pin_kind,
         g.rng, g.body / g.rng, g.up_pct, g.dn_pct, g.close_pos,
         None, None, 0, None,
         ("\n".join(jd.evidence) if evidence is None else evidence),
         params_hash if params_hash is not None else S.params_hash()))


def _reaction_and_forward(con, n=1, key="ema20@15m", lp=100.5):
    """补一条表 2 与一条表 3 —— --verify 会核对它们，缺了就报别的问题。"""
    pen = (101.0 - lp) / lp
    rec = (101.0 - 100.0) / lp
    dist = (100.0 - lp) / lp * 100.0
    con.execute("insert into kbar_reaction values (?,?,?,?,?,?,?,?,?,?,?)",
                (SYM, IV, BASE + n * IV, key, lp, dist, "from_below",
                 pen, rec, "below", 1))
    con.execute("insert into kbar_forward values (?,?,?,?,?,?,?,?,?,?)",
                (SYM, IV, BASE + n * IV, None, None, None, None, None, None, None))


def _bar_ohlc(con, n):
    r = con.execute("select o,h,l,c from kbar_evolution where ts=?",
                    (BASE + n * IV,)).fetchone()
    return {"o": r[0], "h": r[1], "l": r[2], "c": r[3]}


def _bar(con, n, o, h, l, c, **kw):
    con.execute(
        "insert into kbar_evolution values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
        "?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (SYM, IV, BASE + n * IV, o, h, l, c, kw.get("range_ts_h"), kw.get("fam"),
         kw.get("seg"), kw.get("seg_idx"), kw.get("direction"), kw.get("zone"),
         kw.get("dir"), kw.get("body_pct"), kw.get("up_wick_pct"),
         kw.get("dn_wick_pct"), kw.get("close_pos"), kw.get("vol"),
         kw.get("vol_rel"), kw.get("vol_slot_rel"), kw.get("taker_buy_ratio"),
         kw.get("conc"), kw.get("t_low_min"), kw.get("t_high_min"),
         kw.get("path_shape"), kw.get("max_tick_share"),
         kw.get("n_lines", 2), kw.get("n_touch", 0), kw.get("n_break"),
         kw.get("event"), kw.get("on_line"), kw.get("prev_event"),
         kw.get("since_event_idx"), kw.get("state")))


def _line(con, n, key, price, c):
    con.execute("insert into kbar_line values (?,?,?,?,?,?,?)",
                (SYM, IV, BASE + n * IV, key, price, c, (c - price) / price * 100.0))


# ---------------------------------------------------------------- 重算
def test_recompute_finds_the_touch_using_the_previous_bar(tmp_path):
    """碰到判定用「上一根那一行的线价」——本根的线价不算。"""
    p = str(tmp_path / "t.db")
    con = _make_db(p)
    _bar(con, 0, 100, 101, 99, 100)
    _bar(con, 1, 100, 101, 99, 100)
    _line(con, 0, "ema20@15m", 100.5, 100.0)     # 上一根：线价 100.5 落在 [99,101]
    _line(con, 1, "ema20@15m", 200.0, 100.0)     # 本根：线价 200 不在区间里
    con.commit()
    r = T.recompute(con, BASE + IV, SYM, IV)
    con.close()
    assert "ema20@15m" in r["touch"], "用了本根的线价，或者根本没算"
    assert abs(r["touch"]["ema20@15m"]["p"] - 100.5) < 1e-9


def test_recompute_does_not_touch_lines_absent_on_the_previous_bar(tmp_path):
    """上一根没有这条线 → 就是不存在，不许拿本根的行顶上。"""
    p = str(tmp_path / "t.db")
    con = _make_db(p)
    _bar(con, 0, 100, 101, 99, 100)
    _bar(con, 1, 100, 101, 99, 100)
    _line(con, 1, "range_p@1h", 100.5, 100.0)    # 只有本根有
    con.commit()
    r = T.recompute(con, BASE + IV, SYM, IV)
    con.close()
    assert r["touch"] == {}


def test_recompute_event_requires_a_side_change(tmp_path):
    """事件 = 收盘换边。收盘留在同侧就没有事件。"""
    p = str(tmp_path / "t.db")
    con = _make_db(p)
    _bar(con, 0, 100, 101, 99, 99.5)             # 前一根收盘在线下
    _bar(con, 1, 100, 102, 99, 101.0)            # 本根收盘到线上 → 向上突破
    _line(con, 0, "ema20@15m", 100.0, 99.5)
    _line(con, 1, "ema20@15m", 100.0, 101.0)
    con.commit()
    r = T.recompute(con, BASE + IV, SYM, IV)
    con.close()
    assert "ema20@15m" in r["events"]
    assert r["events"]["ema20@15m"]["event"] == "向上突破"


def test_recompute_returns_none_ret_when_not_enough_future_bars(tmp_path):
    p = str(tmp_path / "t.db")
    con = _make_db(p)
    for i in range(2):
        _bar(con, i, 100, 101, 99, 100)
    con.commit()
    r = T.recompute(con, BASE, SYM, IV)
    con.close()
    assert r["forward"]["ret1"] is not None
    assert r["forward"]["ret3"] is None, "后面没有 3 根，ret3 必须是 None"


# ---------------------------------------------------------------- 校验要能真报错
def test_verify_catches_a_wrong_touch_row(tmp_path, capsys):
    """库里写错一条触碰，--verify 必须返回 1 并说清哪里不符。"""
    p = str(tmp_path / "t.db")
    con = _make_db(p)
    _bar(con, 0, 100, 101, 99, 100)
    _bar(con, 1, 100, 101, 99, 100, n_touch=1)
    _line(con, 0, "ema20@15m", 100.5, 100.0)
    _line(con, 1, "ema20@15m", 100.5, 100.0)
    # 库里的反应行故意把穿透写成错的（按定义约 0.005，这里写 0.9）
    con.execute("insert into kbar_reaction values (?,?,?,?,?,?,?,?,?,?,?)",
                (SYM, IV, BASE + IV, "ema20@15m", 100.5, 1.0, "from_above",
                 0.9, 0.5, "above", 1))
    con.execute("insert into kbar_forward values (?,?,?,?,?,?,?,?,?,?)",
                (SYM, IV, BASE + IV, None, None, None, None, None, None, None))
    con.commit()
    con.close()
    rc = T.main(["--at", "2020-09-13 12:30", "--tz", "utc", "--kbar-db", p,
                 "--verify"])
    out = capsys.readouterr().out
    assert rc == 1, "写错的穿透没被抓到"
    assert "不一致" in out


def test_verify_passes_on_a_consistent_bar(tmp_path, capsys):
    p = str(tmp_path / "t.db")
    con = _make_db(p)
    _bar(con, 0, 100, 101, 99, 100)
    _bar(con, 1, 100, 101, 99, 100, n_touch=1)
    _line(con, 0, "ema20@15m", 100.5, 100.0)
    _line(con, 1, "ema20@15m", 100.5, 100.0)
    # 按定义算正确值：o=100 h=101 l=99 c=100，线价 lp=100.5
    #   o(100) < lp(100.5) → 从下方来
    #   穿透 = (h - lp)/lp，收回 = (h - c)/lp，收盘 c(100) < lp → below
    # 注意变量名不能叫 p —— 上面 p 是库文件路径
    lp = 100.5
    side = "from_below"
    pen = (101.0 - lp) / lp
    rec = (101.0 - 100.0) / lp
    dist = (100.0 - lp) / lp * 100.0
    con.execute("insert into kbar_reaction values (?,?,?,?,?,?,?,?,?,?,?)",
                (SYM, IV, BASE + IV, "ema20@15m", lp, dist, side,
                 pen, rec, "below", 1))
    con.execute("insert into kbar_forward values (?,?,?,?,?,?,?,?,?,?)",
                (SYM, IV, BASE + IV, None, None, None, None, None, None, None))
    con.commit()
    con.close()
    rc = T.main(["--at", "2020-09-13 12:30", "--tz", "utc", "--kbar-db", p,
                 "--verify"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "一致" in out


def test_missing_bar_reports_where_the_data_ends_instead_of_crashing(tmp_path,
                                                                    capsys):
    """时间不在库里时要说清库里覆盖到哪、最近一根是哪根。"""
    p = str(tmp_path / "t.db")
    con = _make_db(p)
    _bar(con, 0, 100, 101, 99, 100)
    con.commit()
    con.close()
    rc = T.main(["--at", "2026-11-08 05:15", "--tz", "utc", "--kbar-db", p])
    out = capsys.readouterr().out
    assert rc == 1
    assert "库里覆盖" in out or "没有" in out


# ---------------------------------------------------------------- 表 5（落库的判定）
def test_verify_passes_when_table_5_matches(tmp_path, capsys):
    """表 5 与现算一致时，--verify 必须放行（否则每秒都在报假警）。"""
    p = str(tmp_path / "t.db")
    con = _make_db(p)
    _shape_table(con)
    _bar(con, 0, 100, 101, 99, 100)
    _bar(con, 1, 100, 101, 99, 100, n_touch=1)
    _line(con, 0, "ema20@15m", 100.5, 100.0)
    _line(con, 1, "ema20@15m", 100.5, 100.0)
    _reaction_and_forward(con, 1)
    _shape_fill(con, 1)
    con.commit()
    con.close()
    rc = T.main(["--at", "2020-09-13 12:30", "--tz", "utc", "--kbar-db", p,
                 "--verify"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "表 5 落库" in out


@pytest.mark.parametrize("field,mutate", [
    ("label", dict(label="恐慌盘")),
    ("states", dict(states="插针")),
    ("evidence", dict(evidence="编的一段证据")),
    ("params_hash", dict(params_hash="000000000000")),
])
def test_verify_catches_a_bad_table_5_row(tmp_path, capsys, field, mutate):
    """表 5 里有任何一个字段和现算不一致，--verify 就必须红。

    这不是形式检查：用户要求「结论入 db 库」，那 db 里那行就是**对外唯一的答案**。
    它一旦和代码算的不一样，读库的人和跑代码的人会得到两个结论，
    而两边都不报错 —— 四种改错法都必须被抓住。
    """
    p = str(tmp_path / "t.db")
    con = _make_db(p)
    _shape_table(con)
    _bar(con, 0, 100, 101, 99, 100)
    _bar(con, 1, 100, 101, 99, 100, n_touch=1)
    _line(con, 0, "ema20@15m", 100.5, 100.0)
    _line(con, 1, "ema20@15m", 100.5, 100.0)
    _reaction_and_forward(con, 1)
    _shape_fill(con, 1, **mutate)
    con.commit()
    con.close()
    rc = T.main(["--at", "2020-09-13 12:30", "--tz", "utc", "--kbar-db", p,
                 "--verify"])
    out = capsys.readouterr().out
    assert rc == 1, "改错 %s 却没被抓到：\n%s" % (field, out)
    assert "不符" in out or "不一致" in out, out


def test_verify_does_not_crash_when_table_5_is_absent(tmp_path, capsys):
    """表 5 还没建时要明说跳过，而不是崩（区分「层没落库」与「这根没落库」）。"""
    p = str(tmp_path / "t.db")
    con = _make_db(p)
    _bar(con, 0, 100, 101, 99, 100)
    _bar(con, 1, 100, 101, 99, 100, n_touch=1)
    _line(con, 0, "ema20@15m", 100.5, 100.0)
    _line(con, 1, "ema20@15m", 100.5, 100.0)
    _reaction_and_forward(con, 1)
    con.commit()
    con.close()
    rc = T.main(["--at", "2020-09-13 12:30", "--tz", "utc", "--kbar-db", p,
                 "--verify"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "未校验" in out and "表 5" in out


# ---------------------------------------------------------------- 独立性
def test_tool_does_not_import_the_judgement_modules():
    """校验工具必须独立重算 —— 复用被测代码等于自证。

    注意这里的边界：工具**允许** import `ai_trader.kbar.shape`（读数层）。
    道理是「谁被测谁不许被复用」——
        · 表 1/表 2/表 2.5/表 3 的公式（reaction/event/forward）都被 --verify 校验，
          所以工具不许 import 它们，必须自己按定义重算；
        · 读数层**原先没有表**，所以 --verify 校验不了它；2026-11-08 用户要求
          「结论入 db 库」后表 5 `kbar_shape` 有了，--verify 现在会把库里那行
          和现算逐字段比（label/states/side/pin/evidence/口径指纹）。
          它 import shape 仍然成立：比的是「库里那份」和「代码这份」，
          两份来自不同介质，不是自证。
    """
    src = open(TOOL, encoding="utf-8").read()
    for bad in ("from ai_trader.kbar import reaction",
                "from ai_trader.kbar import event",
                "from ai_trader.kbar import forward",
                "from ai_trader.kbar.reaction",
                "from ai_trader.kbar.event",
                "from ai_trader.kbar.forward",
                "from ai_trader.kbar import build"):
        assert bad not in src, "show_kbar 复用了被测实现：%s" % bad
    # 包级 import 会把 forward / build 一起拉进来，所以也不许
    assert "import ai_trader.kbar\n" not in src
    assert "from ai_trader.kbar import (" not in src
    # 但它确实用了读数层，且读的是 shape 这一个模块
    assert "from ai_trader.kbar import shape as S" in src, \
        "判定那一段应当由 shape.py 提供"


def test_tool_does_not_call_the_recompute_helpers_of_the_judged_modules():
    """光禁 import 不够 —— 有人会写 `__import__` 或把函数复制进来。

    这条查的是「工具里有没有**调用**被校验模块的函数」。
    复制一份实现同样等于自证：改了一边没改另一边，比对就失效。
    只看 `名字(` 这种调用形态 —— `pen_depth_pct` 作为 SQL 列名出现是正常的
    （工具要 select 这一列来跟自己的重算比）。
    """
    src = open(TOOL, encoding="utf-8").read()
    for name in ("build_reactions", "build_events", "build_forward",
                 "pen_depth_pct", "recover_pct", "held_of",
                 "classify_cross", "pick_prev", "assert_no_lookahead"):
        assert name + "(" not in src, \
            "show_kbar 调用了被校验模块的函数：%s()" % name


def test_tool_documents_why_it_does_not_print_only_db_values():
    src = open(TOOL, encoding="utf-8").read()
    assert "独立重算" in src or "重算" in src
    assert "时区" in src, "没写清为什么要显式指定时区"
