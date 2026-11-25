# -*- coding: utf-8 -*-
"""30m 数据层（tools/ingest_kbar.py 的聚合来源 + 周期闸）的回归测试。

## 这层是干什么的

P6 的 thesis 失效判据要用 **ema20@30m**（大一个级别的线）。但库里原本
**没有 30m 这个周期**：`kbar_evolution` 只有 5m / 15m，`kbar_line` 只有
5m / 15m / 1h。所以要先合成。

## 为什么数据源是 kbar.db 的 15m，而不是 market.db 的 1m

实测（2026-11-17）：

    market.db 的 1m  : 1263 根，跨度 0.9 天
    kbar.db 的 15m   : 17654 行，跨度约 184 天

`build_bar_lines` 的口径是「这根之前必须有 EMA_WARMUP_BARS(=1500) 根，
否则一根都不落」。30m 的 1500 根预热 ≈ 31 天 —— 1m 那条路差 30 天数据，
**差的不是一点**。而 15m 合成 30m 有 8828 根，绰绰有余。

## 这个文件钉的是什么

1. 桶聚合的契约（o 取首根、c 取末根、h/l 取极值、v 求和）——
   这四件事错了不会有任何报错，只会算出一根「看起来合法」的假 bar。
2. **输入必须升序**这个隐含契约。
3. 30m 只能从 15m 的整数倍合成。
4. 30m 超过 lines 层要**硬停**，不能默默往下跑。
5. 预热不足时一根 EMA 都不落，且必须报数。
"""
from __future__ import annotations

import importlib.util
import io
import os
import sqlite3
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, "tools", "ingest_kbar.py")

SYMBOL = "BTCUSDT"
IV_15M = 900_000
IV_30M = 1_800_000
WARMUP = 1500

# 整 30m 边界起步，避免桶残缺干扰断言
T0 = 1_700_000_000_000 - (1_700_000_000_000 % IV_30M)


def _load_tool():
    """按路径加载 tools/ingest_kbar.py。

    直接 import 不行：tools/ 不是包（没有 __init__.py），而且就算能 import，
    这个模块在 import 期会往 sys.path 插 ROOT。用 spec 加载最省事，
    也让测试和「真的跑这个文件」用的是同一份代码。
    """
    spec = importlib.util.spec_from_file_location("_ingest_kbar_under_test", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ING = _load_tool()


# ---------------------------------------------------------------- 桶聚合契约

def test_bucket_rows_takes_first_open_and_last_close():
    """两根 15m 合成的 30m：open 取前一根的 o，close 取后一根的 c。

    这两个方向是聚合里最容易写反的地方（open 取错一根 / close 取错一根），
    写反了结果依然是一根合法的 K 线，没有任何报错。
    """
    rows = [
        (T0,         100.0, 110.0, 90.0, 105.0, 10.0),
        (T0 + IV_15M, 105.0, 130.0, 95.0, 120.0, 20.0),
    ]
    out = ING._bucket_rows(rows, IV_30M)
    assert len(out) == 1
    got = out[0]
    assert got[0] == T0           # 桶键对齐到 30m 边界
    assert got[1] == 100.0        # open = 第一根的 o（不是 105）
    assert got[4] == 120.0        # close = 最后一根的 c（不是 105）


def test_bucket_rows_high_is_max_and_low_is_min():
    rows = [
        (T0,          100.0, 110.0, 90.0,  105.0, 10.0),
        (T0 + IV_15M, 105.0, 130.0, 95.0,  120.0, 20.0),
    ]
    out = ING._bucket_rows(rows, IV_30M)
    assert out[0][2] == 130.0     # h = max（不是末根）
    assert out[0][3] == 90.0      # l = min（不是末根）


def test_bucket_rows_sums_volume():
    rows = [
        (T0,          100.0, 110.0, 90.0, 105.0, 10.0),
        (T0 + IV_15M, 105.0, 130.0, 95.0, 120.0, 25.0),
    ]
    out = ING._bucket_rows(rows, IV_30M)
    assert out[0][5] == 35.0      # v = sum，不是 max 也不是末根


def test_bucket_rows_none_volume_counts_as_zero():
    """缺量（None）不能把整个桶的成交量变成 None。"""
    rows = [
        (T0,          100.0, 110.0, 90.0, 105.0, None),
        (T0 + IV_15M, 105.0, 130.0, 95.0, 120.0, 7.0),
    ]
    out = ING._bucket_rows(rows, IV_30M)
    assert out[0][5] == 7.0


def test_bucket_rows_two_15m_per_30m_bucket():
    """4 根 15m 应该正好合成 2 根 30m（不多不少）。"""
    rows = []
    for i in range(4):
        ts = T0 + i * IV_15M
        rows.append((ts, 100.0 + i, 101.0 + i, 99.0 + i, 100.5 + i, 1.0))
    out = ING._bucket_rows(rows, IV_30M)
    assert len(out) == 2
    assert [r[0] for r in out] == [T0, T0 + IV_30M]
    assert out[0][4] == 101.5     # 第 2 根的 c
    assert out[1][4] == 103.5     # 第 4 根的 c


def test_bucket_rows_order_matters_and_is_ascending_only():
    """★ 输入必须升序 —— 这是个**没有防御**的隐含契约，故意钉出来。

    聚合只做「第一根定 open、最后一根定 close」，不排序也不校验。
    乱序输入不会报错，只会算出一根 open/close 都错的假 K 线。
    所以调用方的 SQL 必须带 ORDER BY（`_agg_from_1m` 与
    `_agg_from_kbar_15m` 都带了）。这条测试的存在意义是：
    以后谁想改成「不排序也行」，会先看到这里已经声明过是调用方的责任。
    """
    asc = [
        (T0,          100.0, 110.0, 90.0, 105.0, 10.0),
        (T0 + IV_15M, 105.0, 130.0, 95.0, 120.0, 20.0),
    ]
    good = ING._bucket_rows(asc, IV_30M)[0]
    bad = ING._bucket_rows(list(reversed(asc)), IV_30M)[0]
    assert good[1] == 100.0 and good[4] == 120.0
    # 反序之后 open/close 都换了，但结果依然是「合法」的 bar —— 这就是坑
    assert bad[1] == 105.0 and bad[4] == 105.0
    assert good[:5] != bad[:5]


# -------------------------------------------------------------- 来源合法性

def _make_kbar_db(path, n15=10, offset_ms=0):
    """造一个只含 kbar_evolution(15m) 的最小 kbar.db。"""
    con = sqlite3.connect(path)
    try:
        con.execute(
            "CREATE TABLE kbar_evolution ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " symbol TEXT NOT NULL, interval_ms INTEGER NOT NULL,"
            " ts INTEGER NOT NULL, o REAL, h REAL, l REAL, c REAL, vol REAL)")
        rows = []
        for i in range(n15):
            ts = T0 + offset_ms + i * IV_15M
            base = 80_000.0 + i
            rows.append((SYMBOL, IV_15M, ts, base, base + 50.0,
                         base - 50.0, base + 10.0, 100.0 + i))
        con.executemany(
            "INSERT INTO kbar_evolution"
            " (symbol, interval_ms, ts, o, h, l, c, vol) VALUES (?,?,?,?,?,?,?,?)",
            rows)
        con.commit()
    finally:
        con.close()


def test_agg_from_kbar_15m_reads_and_buckets(tmp_path):
    db = str(tmp_path / "kbar.db")
    _make_kbar_db(db, n15=4)
    out = ING._agg_from_kbar_15m(db, SYMBOL, IV_30M)
    assert len(out) == 2
    assert out[0][0] == T0
    assert out[0][1] == 80_000.0            # 首根 o
    # 第 2 根的 c：base = 80000 + 1 = 80001，c = base + 10 = 80011
    assert out[0][4] == 80_011.0
    assert out[0][5] == 201.0               # 100 + 101


def test_agg_from_kbar_15m_only_reads_15m_rows(tmp_path):
    """库里若混了别的周期，不能被一起合进去。

    ★ 污染行的时刻必须落在 4 根 15m **之外**。第一版测试把它放在 T0，
    结果它落进已有的 30m 桶里、桶数不变 —— 那条测试对「忘记按周期过滤」
    这个变异体是**完全无效**的（实测：变异体存活）。放在范围外才会多出一个桶。
    同时断言极值没被污染，因为「桶数对了但值串了」也是一种失败。
    """
    db = str(tmp_path / "kbar.db")
    _make_kbar_db(db, n15=4)
    con = sqlite3.connect(db)
    try:
        con.execute(
            "INSERT INTO kbar_evolution"
            " (symbol, interval_ms, ts, o, h, l, c, vol)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (SYMBOL, 300_000, T0 + 10 * IV_15M, 1.0, 999_999.0, 0.5, 4.0, 5.0))
        con.commit()
    finally:
        con.close()
    out = ING._agg_from_kbar_15m(db, SYMBOL, IV_30M)
    assert len(out) == 2, "5m 那行被混进来会多出一个桶"
    assert max(r[2] for r in out) < 999_999.0, "5m 的极值串进了 30m 的桶"


def test_agg_from_kbar_15m_only_reads_requested_symbol(tmp_path):
    db = str(tmp_path / "kbar.db")
    _make_kbar_db(db, n15=2)
    con = sqlite3.connect(db)
    try:
        con.execute(
            "INSERT INTO kbar_evolution"
            " (symbol, interval_ms, ts, o, h, l, c, vol)"
            " VALUES (?,?,?,?,?,?,?,?)",
            ("ETHUSDT", IV_15M, T0, 1.0, 2.0, 3.0, 4.0, 5.0))
        con.commit()
    finally:
        con.close()
    out = ING._agg_from_kbar_15m(db, SYMBOL, IV_30M)
    assert len(out) == 1
    assert out[0][1] == 80_000.0, "别的币种的数据串进来了"


@pytest.mark.parametrize("iv", [300_000, 900_000 - 1, 600_000, 1_000_000])
def test_agg_from_kbar_15m_rejects_non_multiple_of_15m(tmp_path, iv):
    """只能合成 15m 的整数倍。非整数倍要**报错**，不能悄悄按整数除法糊过去。"""
    db = str(tmp_path / "kbar.db")
    _make_kbar_db(db, n15=2)
    with pytest.raises(ValueError):
        ING._agg_from_kbar_15m(db, SYMBOL, iv)


@pytest.mark.parametrize("iv", [IV_30M, 3_600_000, 2_700_000])
def test_agg_from_kbar_15m_accepts_multiples_of_15m(tmp_path, iv):
    db = str(tmp_path / "kbar.db")
    _make_kbar_db(db, n15=12)
    out = ING._agg_from_kbar_15m(db, SYMBOL, iv)
    assert out, "整数倍周期应该能合成出来"
    assert all(r[0] % iv == 0 for r in out), "桶键没对齐到目标周期"


# ------------------------------------------------------------ 命令行周期闸

def test_interval_1800000_is_an_accepted_choice(monkeypatch):
    """30m 必须在 argparse 的 choices 里，否则 30m 根本跑不起来。"""
    monkeypatch.setattr(sys, "argv", ["ingest_kbar.py", "--interval-ms", "1800000"])
    args = ING._argp()
    assert args.interval_ms == IV_30M
    assert hasattr(args, "from_kbar_15m")


def test_from_kbar_15m_flag_parses(monkeypatch):
    monkeypatch.setattr(sys, "argv",
                        ["ingest_kbar.py", "--interval-ms", "1800000",
                         "--from-kbar-15m", "--until", "lines"])
    args = ING._argp()
    assert args.from_kbar_15m is True
    assert args.until == "lines"


def test_30m_beyond_lines_layer_is_refused(monkeypatch, capsys):
    """30m 跑超过 lines 的层必须硬停（返回 2），且说清楚原因。

    为什么要停：facts 层的 `TICKS_PER_BAR` 是按 15m 算死的常量，
    reactions/events 那几层也从未在 30m 上验证过。默默跑下去会产出一批
    「看起来合理」的 30m 结论 —— 本项目最吃过亏的失败方式就是这个
    （见交接文档 §十五：静默停摆 6.5 天）。
    """
    monkeypatch.setattr(sys, "argv",
                        ["ingest_kbar.py", "--interval-ms", "1800000",
                         "--from-kbar-15m"])
    rc = ING.main()
    out = capsys.readouterr().out
    assert rc == 2
    assert "只支持到 lines 层" in out
    assert "--until lines" in out


def test_15m_still_runs_all_layers(monkeypatch):
    """闸门只拦 30m，别把 15m 的老路径一起关掉。"""
    monkeypatch.setattr(sys, "argv", ["ingest_kbar.py", "--interval-ms", "900000"])
    args = ING._argp()
    cutoff = ING.LAYERS.index(args.until) + 1 if args.until else len(ING.LAYERS)
    assert args.interval_ms == IV_15M
    assert cutoff == len(ING.LAYERS), "15m 应该仍然跑全部层"


# ------------------------------------------------------- 预热不足必须报数

def test_warmup_short_emits_no_ema_and_reports_it():
    """30m 序列不足 1500 根时，EMA 一根都不落，且 skipped 里必须报数。

    这是本项目反复吃过的亏：「禁止静默过滤」要求每个没产出的线都说出原因。
    少了一万行却没人知道，比报错难查得多。
    """
    from ai_trader.kbar import lines as L

    bars = [L.Bar(T0 + i * IV_30M, 100.0 + i, 101.0 + i, 99.0 + i,
                  100.5 + i, 1.0) for i in range(100)]
    rows, stats = L.build_bar_lines(SYMBOL, IV_30M, bars)
    ema_rows = [r for r in rows if r["line_key"].startswith("ema")]
    assert ema_rows == [], "预热不足却落了 EMA 行"
    assert stats["warmup_short"] is True
    assert any(k.startswith("ema20:warmup") for k in stats["skipped"]), \
        "预热不足没有报数，违反了「禁止静默过滤」"


def test_enough_30m_bars_emit_ema20():
    """足量 30m bar 时 ema20@30m 要真的落出来，且 key 带 30m 标签。"""
    from ai_trader.kbar import lines as L

    n = WARMUP + 10
    bars = [L.Bar(T0 + i * IV_30M, 100.0 + i, 101.0 + i, 99.0 + i,
                  100.5 + i, 1.0) for i in range(n)]
    rows, stats = L.build_bar_lines(SYMBOL, IV_30M, bars)
    ema = [r for r in rows if r["line_key"] == "ema20@30m"]
    assert len(ema) == n - WARMUP + 1
    assert all(r["interval_ms"] == IV_30M for r in ema)
    assert L.period_label(IV_30M) == "30m"
