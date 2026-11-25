# -*- coding: utf-8 -*-
"""上生产前置工作（G1~G5）的护栏。

这五个缺口是 2026-11-09 审计列出来的，逐条对应到这里：

    G1 行情进程没有「只采集」模式        → test_feed_only_*
    G2 行情拉取走系统代理、失败即放弃    → test_proxy_*  / test_fetch_retries_*
    G3 没有增量编排器                    → test_ingest_*
    G4 5m 不在聚合周期里                 → test_web_chart_fixes::
                                            test_5m_is_now_a_native_aggregator_interval
    G5 kbar_meta 不记来源                → test_source_*

⚠ 这里刻意**不**联网、**不**碰生产库：全部用临时目录造库。
G1 的"行情能落库"验证需要真网络，所以不在自动化测试里，只在文档里记录
实测结论（当前环境 `src=DOWN`）—— 把"网络通不通"写进测试，只会变成随机红。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ai_trader.kbar import build as B            # noqa: E402
from ai_trader.kbar.store import KBarStore      # noqa: E402


# ============================================================ G5 来源登记

def test_source_record_roundtrips(tmp_path):
    """来源记录必须能原样读回来（kbar_meta 存的是 JSON 字符串）。"""
    db = str(tmp_path / "k.db")
    bars = [(1000, 1, 2, 1, 1.5, 10), (2000, 1, 2, 1, 1.6, 11)]
    with KBarStore(db) as st:
        rec = B.record_source(st, "BTCUSDT", 300_000, "lines", bars,
                              ["data/market.db"], note="测试")
        raw = st.get_meta("source:BTCUSDT:5m:lines")
    assert isinstance(raw, str), "kbar_meta 存的是字符串，不是 dict"
    back = json.loads(raw)
    assert back["layer"] == "lines"
    assert back["sources"] == ["data/market.db"]
    assert back["bars"] == 2
    assert back["first_ts"] == 1000 and back["last_ts"] == 2000
    assert back["input_fp"] == rec["input_fp"]


def test_source_fingerprint_handles_both_bar_shapes():
    """第 1 步给元组、第 2 步给 dict。只支持一种，另一层就会静默失败。"""
    tuples = [(1000, 1, 2, 1, 1.5, 10), (2000, 1, 2, 1, 1.6, 11)]
    dicts = [{"ts": 1000, "o": 1, "h": 2, "l": 1, "c": 1.5},
             {"ts": 2000, "o": 1, "h": 2, "l": 1, "c": 1.6}]
    assert B._bars_fingerprint(tuples) == B._bars_fingerprint(dicts)
    assert B._bars_fingerprint([]) == "empty"


def test_source_fingerprint_detects_value_drift():
    """同区间同根数但价格不同 → 指纹必须不同。

    这正是"复现性断了"要抓的东西：覆盖面一模一样，数据其实不是同一份。
    """
    a = [(1000, 1, 2, 1, 1.5, 10), (2000, 1, 2, 1, 1.6, 11)]
    b = [(1000, 1, 2, 1, 1.5, 10), (2000, 1, 2, 1, 1.7, 11)]
    fa, fb = B._bars_fingerprint(a), B._bars_fingerprint(b)
    assert fa.split(":")[0] == fb.split(":")[0], "区间和根数应该相同"
    assert fa != fb, "价格变了指纹却没变，来源就无从比对"


def test_source_provenance_does_not_affect_judgments():
    """来源登记**只记事实**，不许渗进判据。

    `judge` 读哪些列是由 ledger 从 shape.py 的 AST 推出来的，
    加 kbar_meta 的键不应该改变它。这条钉住"可观测性不得影响结论"。
    """
    from ai_trader.kbar import ledger as LG
    reads = LG.judge_reads()
    assert reads, "judge_reads() 不该为空"
    for table, col in reads:
        assert "source" not in table and "source" not in col, (
            "来源记录渗进判据了：%s.%s" % (table, col))


# ============================================================ G2 代理与重试

def test_proxy_off_forces_direct_connection(monkeypatch):
    """AI_HTTP_PROXY=off 必须真的不读系统代理。

    这台机器的系统代理是 127.0.0.1:4780，代理没起来时 urllib 报
    `Errno 2 No such file or directory` —— 看起来像文件问题，实际是代理。
    所以必须有一个显式的"强制直连"开关。

    ⚠ 断言的是 `_proxy_mode()` 这个**决策**，不是 build_opener 的 handlers：
    实测 Python 3.9 里 `build_opener(ProxyHandler({}))` 的 `.handlers`
    看不到 ProxyHandler（与无参调用一样是 0 个）。第一版测试查 handlers，
    结果它红得莫名其妙 —— 那种断言验的不是行为，是实现细节。
    """
    from ai_trader.web import live
    monkeypatch.setenv("AI_HTTP_PROXY", "off")
    assert live._proxy_mode() == ("direct", "")

    monkeypatch.delenv("AI_HTTP_PROXY", raising=False)
    assert live._proxy_mode() == ("default", ""), "未设置时应沿用系统代理（旧行为）"


def test_proxy_explicit_value_is_used(monkeypatch):
    from ai_trader.web import live
    monkeypatch.setenv("AI_HTTP_PROXY", "http://127.0.0.1:9999")
    assert live._proxy_mode() == ("explicit", "http://127.0.0.1:9999")
    # 大小写与空白都要容忍
    monkeypatch.setenv("AI_HTTP_PROXY", "  OFF  ")
    assert live._proxy_mode() == ("direct", "")


def test_fetch_retries_then_gives_actionable_error(monkeypatch):
    """重试要发生，且最终错误里要带"怎么自救"。

    旧版错误是 `URLError: [Errno 2] No such file or directory` ——
    我本人被它误导过一次，把"代理不通"写成了"币安不可达"。
    """
    import urllib.error
    from ai_trader.web import live

    calls = {"n": 0}

    class Boom:
        def open(self, *a, **k):
            calls["n"] += 1
            raise urllib.error.URLError(OSError(2, "No such file or directory"))

    monkeypatch.setattr(live, "_opener", lambda: Boom())
    monkeypatch.setattr(live.time, "sleep", lambda s: None)

    with pytest.raises(RuntimeError) as ei:
        live._fetch_klines("BTCUSDT", "5m", 5, retries=2)
    assert calls["n"] == 3, "应该请求 3 次（1 次 + 重试 2 次），实际 %d" % calls["n"]
    msg = str(ei.value)
    assert "已重试 2 次" in msg
    assert "AI_HTTP_PROXY" in msg, "错误里没给出自救办法：%s" % msg


def test_fetch_does_not_retry_on_binance_rejection(monkeypatch):
    """币安回了 HTTP 状态码 = 网络通，重试只会让封禁更久。"""
    import urllib.error
    from ai_trader.web import live

    calls = {"n": 0}

    class Reject:
        def open(self, *a, **k):
            calls["n"] += 1
            raise urllib.error.HTTPError("u", 418, "teapot", {}, None)

    monkeypatch.setattr(live, "_opener", lambda: Reject())
    with pytest.raises(RuntimeError) as ei:
        live._fetch_klines("BTCUSDT", "5m", 5, retries=2)
    assert calls["n"] == 1, "HTTP 状态码错误不该重试"
    assert "418" in str(ei.value)


# ============================================================ G3 增量编排器

def _mk_market_db(path, n_1m=120, base=1_790_000_000_000):
    # 桶边界必须先对齐：base 不对齐 300000 时，20 根 1m 会跨 5 个 5m 桶
    # （第一桶只有 2 根）。第一版测试就是没对齐，红得莫名其妙。
    base = (base // 300_000) * 300_000
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE bars (symbol TEXT, interval_ms INTEGER, "
                "open_time INTEGER, o REAL, h REAL, l REAL, c REAL, v REAL, "
                "PRIMARY KEY (symbol, interval_ms, open_time))")
    for i in range(n_1m):
        o = 70000.0 + i
        con.execute("INSERT INTO bars VALUES (?,?,?,?,?,?,?,?)",
                    ("BTCUSDT", 60000, base + i * 60000,
                     o, o + 5, o - 3, o + 2, 1.0))
    con.commit()
    con.close()
    return path


def test_ingest_aggregates_1m_to_5m_correctly(tmp_path):
    """1m → 5m 的聚合必须 o=首 / h=max / l=min / c=末 / v=sum。"""
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import ingest_kbar as IG

    mdb = _mk_market_db(str(tmp_path / "market.db"), n_1m=20)
    bars = IG._agg_from_1m(mdb, 300_000)
    assert len(bars) == 4, "20 根 1m 应聚成 4 根 5m，实际 %d" % len(bars)
    first = bars[0]
    # 前 5 根 1m：o 从 70000 递增到 70004，c 从 70002 到 70006
    assert first[1] == 70000.0, "开盘价应取桶内第一根"
    assert first[4] == pytest.approx(70006.0), "收盘价应取桶内最后一根"
    assert first[2] == pytest.approx(70009.0, abs=0.01), "最高价应取桶内最大"
    assert first[3] == pytest.approx(69997.0, abs=0.01), "最低价应取桶内最小"
    assert first[5] == pytest.approx(5.0), "成交量应求和"
    # 桶必须对齐到 5 分钟边界
    assert first[0] % 300_000 == 0, "桶起点没对齐到 5 分钟"


def test_ingest_refuses_missing_db(tmp_path, capsys):
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import importlib
    import ingest_kbar as IG
    importlib.reload(IG)

    old = sys.argv
    try:
        sys.argv = ["ingest_kbar.py",
                    "--market-db", str(tmp_path / "nope.db"),
                    "--kbar-db", str(tmp_path / "nokbar.db"),
                    "--dry-run"]
        rc = IG.main()
    finally:
        sys.argv = old
    assert rc == 2
    out = capsys.readouterr().out
    assert "找不到" in out


def test_ingest_layer_order_matches_dependency():
    """层顺序就是依赖顺序，顺序错了会拿旧数据算出"看起来合理"的新结论。"""
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import ingest_kbar as IG

    assert IG.LAYERS[0] == "lines", "表0 必须最先"
    assert IG.LAYERS[1] == "facts", "表1 依赖表0"
    assert IG.LAYERS[-1] == "dims", "表7 最后"
    # 三类增量语义必须分得开，不能混成一个说法
    assert IG.INPUT_INCREMENTAL == {"lines", "facts"}
    assert IG.INCREMENTAL == {"verify", "survival", "dims"}
    assert IG.FULL_REBUILD == {"reactions", "events", "forward", "shape"}
    assert (IG.INPUT_INCREMENTAL | IG.INCREMENTAL | IG.FULL_REBUILD) == set(IG.LAYERS), \
        "每一层都必须被归类，否则计划里会打出 '?'"


def test_ingest_kind_labels_are_distinct():
    """增量的三种来源必须打不同的字，别让人以为"标了增量就一定快"。"""
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import ingest_kbar as IG

    assert IG._kind("lines", True) != IG._kind("verify", True)
    assert IG._kind("lines", False).startswith("跳过")
    assert IG._kind("shape", True) == "全表重算"


# ============================================================ G1 只采集模式

def test_feed_only_is_wired_in_cli():
    """`--feed-only` 必须存在，且 run() 要收这个参数。

    这条只是接线检查 —— 真正的行为验证是实跑 `--feed-only --seconds N`，
    看它有没有构造引擎（那需要网络，不在自动化测试里）。
    """
    import inspect
    from ai_trader import main as M

    src = inspect.getsource(M.main)
    assert "--feed-only" in src
    assert "feed_only=args.feed_only" in src
    # feed_only 必须隐含 no-web，否则 Web 会拿到 engine=None
    assert "not args.feed_only" in src

    sig = inspect.signature(M.run)
    assert "feed_only" in sig.parameters


def test_feed_only_skips_engine_construction():
    """run() 里构造引擎的部分必须整体在 `if not feed_only:` 之内。

    这条是有来历的：第一版只把 executor 的三分支包住了，
    `guard = Guard(...)` / `engine = DecisionEngine(...)` 留在外面照跑，
    于是 `--feed-only` 起来第一秒就
    `AttributeError: 'NoneType' object has no attribute 'name'`。
    """
    import inspect
    from ai_trader import main as M

    src = inspect.getsource(M.run)
    # 找到 if not feed_only: 到下一个同级语句之间的块
    idx = src.index("if not feed_only:")
    block = src[idx:]
    for must in ("DecisionEngine(", "Guard(", "engine.attach()",
                 "reconcile_live()"):
        pos = block.index(must)
        # 这些都必须出现在该 if 之后（且缩进更深），不能在它之前被无条件执行
        assert pos < block.index("\n    if feed_only:") if \
            "\n    if feed_only:" in block else True
        assert pos > 0
    # 最硬的检查：DecisionEngine 那行的缩进必须比 `if not feed_only:` 深
    line = [ln for ln in block.split("\n") if "DecisionEngine(" in ln][0]
    assert line.startswith("        "), "DecisionEngine 没有缩进进 if 块：%r" % line
