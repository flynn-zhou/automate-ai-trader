# -*- coding: utf-8 -*-
"""命令行编排层（tools/ingest_kbar.py）的回归测试。

这个文件存在的**唯一**原因：`ingest_kbar.main()` 里有一个变量遮蔽 bug ——

    L240: steps.append((name, _kind(name, bool(bars))))   # 调用模块级函数
    L253: for name, _kind in steps:                       # 同名赋值！

Python 在**编译期**就因为这个赋值把 `_kind` 判成 `main()` 的局部名，
于是 L240 引用它时还没绑定 → `UnboundLocalError`。
跟走不走得到 L253 无关，**任何参数都必崩**，`--dry-run` 也一样。

它为什么能活 6 天：13 个 `test_kbar_*.py` 全部直接调
`B.build_and_persist_*`，**没有一条**经过这层编排。于是工具从
2026-11-10 09:40 那次编辑起就完全失效，`kbar.db` 静默停摆 6.5 天，
而所有分析都在旧数据上照常跑、照常出结论。

这里补的就是「真的把命令行跑一遍」这一层：subprocess 起进程，
断言退出码、断言 plan 打得出来、断言不写库。
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, "tools", "ingest_kbar.py")

SYMBOL = "BTCUSDT"
IV_15M = 900_000
IV_5M = 300_000

# 起点取一个整 1h、且能被 15m/5m 同时整除的时刻，避免桶边界干扰
T0 = 1_700_000_000_000 - (1_700_000_000_000 % 3_600_000)


def _make_market_db(path, interval_ms=IV_15M, n=150):
    """造一个最小 market.db：只要 bars 表够 load_market_bars 读。"""
    con = sqlite3.connect(path)
    try:
        con.execute(
            "CREATE TABLE IF NOT EXISTS bars ("
            " symbol TEXT NOT NULL, interval_ms INTEGER NOT NULL,"
            " open_time INTEGER NOT NULL, o REAL, h REAL, l REAL, c REAL, v REAL,"
            " PRIMARY KEY (symbol, interval_ms, open_time))")
        rows = []
        for i in range(n):
            ts = T0 + i * interval_ms
            base = 80_000.0 + i
            rows.append((SYMBOL, interval_ms, ts,
                         base, base + 50.0, base - 50.0, base + 10.0, 100.0 + i))
        con.executemany("INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?,?,?,?)", rows)
        con.commit()
    finally:
        con.close()


def _make_kbar_db(path):
    """造一个空 kbar.db：KBarStore 建好全部表，_last_ts 返回 0。"""
    sys.path.insert(0, ROOT)
    from ai_trader.kbar.store import KBarStore
    with KBarStore(path) as st:                     # noqa: F841 —— 建表即目的
        pass


def _make_market_db_1m_only(path, n_15m=150):
    """只给 1m bar、**没有**原生 15m —— 逼 _load_bars 走 --from-1m 的聚合路径。

    这条路返回的是纯 list（不是 L.Bar），正是 _norm_bar 唯一真正干活的场景。
    不测它，「归一化」这行代码就等于没被验证过。
    """
    con = sqlite3.connect(path)
    try:
        con.execute(
            "CREATE TABLE IF NOT EXISTS bars ("
            " symbol TEXT NOT NULL, interval_ms INTEGER NOT NULL,"
            " open_time INTEGER NOT NULL, o REAL, h REAL, l REAL, c REAL, v REAL,"
            " PRIMARY KEY (symbol, interval_ms, open_time))")
        rows = []
        for i in range(n_15m * 15):                 # 15 根 1m = 1 根 15m
            ts = T0 + i * 60_000
            base = 80_000.0 + i * 0.4
            rows.append((SYMBOL, 60_000, ts,
                         base, base + 30.0, base - 30.0, base + 5.0, 10.0))
        con.executemany("INSERT INTO bars VALUES (?,?,?,?,?,?,?,?)", rows)
        con.commit()
    finally:
        con.close()


@pytest.fixture()
def dbs(tmp_path):
    market = tmp_path / "market.db"
    kbar = tmp_path / "kbar.db"
    _make_market_db(str(market))
    _make_kbar_db(str(kbar))
    return str(market), str(kbar), str(tmp_path / "no_such_range.db")


def _run(dbs, *extra, interval_ms=IV_15M):
    market, kbar, rng = dbs
    cmd = [sys.executable, TOOL,
           "--market-db", market,
           "--kbar-db", kbar,
           "--range-db", rng,
           "--symbol", SYMBOL,
           "--interval-ms", str(interval_ms)] + list(extra)
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=180)


# --------------------------------------------------------------------------
# 核心回归：有新增 bar 时 main() 必须能走到「打印计划」
# --------------------------------------------------------------------------

def test_dry_run_with_new_bars_exits_zero(dbs):
    """这条就是抓 UnboundLocalError 的那一条。

    L240 在 steps 构造时调用模块级 _kind；只要 main() 里还有同名局部赋值，
    这里就必崩（退出码 1 + stderr 带 UnboundLocalError）。
    """
    r = _run(dbs, "--dry-run")
    assert r.returncode == 0, (
        "ingest_kbar --dry-run 崩了：\nSTDOUT:\n%s\nSTDERR:\n%s"
        % (r.stdout, r.stderr))
    assert "UnboundLocalError" not in r.stderr
    assert "UnboundLocalError" not in r.stdout


def test_dry_run_prints_the_plan_and_the_layer_kinds(dbs):
    """计划必须把「每层是增量还是全量」打出来 —— 这是运维唯一能看懂的信号。"""
    r = _run(dbs, "--dry-run")
    assert r.returncode == 0
    out = r.stdout
    assert "计划" in out
    for layer in ("lines", "facts", "reactions", "events", "forward",
                  "shape", "verify", "survival", "dims"):
        assert layer in out, "计划里缺了 %s 这一层" % layer
    # 增量/全量的标注必须真的体现在输出里，否则运维会以为全量也很快
    assert "全表重算" in out
    assert "只喂" in out


def test_dry_run_says_how_many_new_bars(dbs):
    r = _run(dbs, "--dry-run")
    assert r.returncode == 0
    assert "新增 bar" in r.stdout
    assert "150" in r.stdout


def test_dry_run_does_not_write_to_the_kbar_db(dbs):
    """--dry-run 的契约是「没写库」。空库跑完必须还是空库。"""
    _, kbar, _ = dbs
    r = _run(dbs, "--dry-run")
    assert r.returncode == 0
    con = sqlite3.connect(kbar)
    try:
        for t in ("kbar_line", "kbar_evolution", "kbar_shape", "kbar_event"):
            n = con.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
            assert n == 0, "--dry-run 却往 %s 写了 %d 行" % (t, n)
    finally:
        con.close()


def test_dry_run_respects_until(dbs):
    """--until 必须真的截断层列表（否则「只跑到 facts」会变成全跑）。"""
    r = _run(dbs, "--dry-run", "--until", "facts")
    assert r.returncode == 0
    out = r.stdout
    assert "lines" in out and "facts" in out
    assert "reactions" not in out
    assert "shape" not in out


def test_5m_interval_is_accepted(dbs):
    """5m 与 15m 走同一条编排路径，别只测一种。"""
    market, kbar, rng = dbs
    _make_market_db(market, interval_ms=IV_5M, n=200)
    r = _run(dbs, "--dry-run", interval_ms=IV_5M)
    assert r.returncode == 0, r.stderr
    assert "5m" in r.stdout


# --------------------------------------------------------------------------
# 输入缺失时必须给人话，不能甩 sqlite 栈
# --------------------------------------------------------------------------

def test_missing_market_db_says_so_instead_of_traceback(tmp_path, dbs):
    _, kbar, rng = dbs
    cmd = [sys.executable, TOOL,
           "--market-db", str(tmp_path / "nope.db"),
           "--kbar-db", kbar,
           "--range-db", rng,
           "--symbol", SYMBOL,
           "--interval-ms", str(IV_15M),
           "--dry-run"]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=120)
    assert r.returncode == 2
    assert "market.db" in r.stdout
    assert "Traceback" not in r.stderr


def test_missing_kbar_db_tells_you_to_backfill_first(tmp_path, dbs):
    market, _, rng = dbs
    cmd = [sys.executable, TOOL,
           "--market-db", market,
           "--kbar-db", str(tmp_path / "nope.db"),
           "--range-db", rng,
           "--symbol", SYMBOL,
           "--interval-ms", str(IV_15M),
           "--dry-run"]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=120)
    assert r.returncode == 2
    assert "kbar.db" in r.stdout
    assert "Traceback" not in r.stderr


# --------------------------------------------------------------------------
# 没有新 bar：不能崩，也不能假装算了
# --------------------------------------------------------------------------

def test_no_new_bars_still_exits_zero_and_warns(dbs):
    """kbar 已经算到最新时会走「没有新 bar」分支。

    这一支同样在 steps 构造之后，之前被 UnboundLocalError 挡在前面，
    所以从来没人验证过它能不能正常收尾。
    """
    market, kbar, rng = dbs
    # 先真跑一次，把数据写进去
    r1 = _run(dbs, interval_ms=IV_15M)
    assert r1.returncode == 0, "首次回填失败：\n%s\n%s" % (r1.stdout, r1.stderr)
    # 再跑一次：此时库里已到最新
    r2 = _run(dbs, "--dry-run")
    assert r2.returncode == 0, r2.stderr
    assert "没有新 bar" in r2.stdout
    # 没有新 bar 也不该崩 —— 这才是这条测试真正守的东西
    assert "UnboundLocalError" not in r2.stderr


def test_from_1m_aggregation_runs_every_layer(tmp_path):
    """只有 1m bar 时走 --from-1m：输入是纯 list，必须被归一化后喂给所有层。

    这条守的是 _norm_bar。没有它，`lines` 拿到纯 list、
    `facts` 对纯 list 调 ._asdict() —— 两层各自以不同方式崩。
    """
    market = str(tmp_path / "market_1m.db")
    kbar = str(tmp_path / "kbar.db")
    rng = str(tmp_path / "no_such_range.db")
    _make_market_db_1m_only(market)
    _make_kbar_db(kbar)

    cmd = [sys.executable, TOOL,
           "--market-db", market,
           "--kbar-db", kbar,
           "--range-db", rng,
           "--symbol", SYMBOL,
           "--interval-ms", str(IV_15M),
           "--from-1m"]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=300)
    assert r.returncode == 0, (
        "--from-1m 路径崩了：\nSTDOUT:\n%s\nSTDERR:\n%s" % (r.stdout, r.stderr))
    assert "1m 聚合为 15m" in r.stdout
    # 真的落库了，不只是跑完
    con = sqlite3.connect(kbar)
    try:
        n = con.execute("SELECT COUNT(*) FROM kbar_evolution").fetchone()[0]
        assert n > 0, "跑完了但 kbar_evolution 是空的（默认参数下这条必红）"
    finally:
        con.close()


def test_lines_layer_gets_warmup_history(tmp_path):
    """增量补数据时，lines 层必须拿到预热历史，否则 EMA 全线缺失。

    实测过的现场（2026-11-16 补 622 根 15m 之后）：
        donchian_hi@15m   473/473   ← 预热 150 根，够
        ema20@15m           0/0     ← 预热 1500 根，只喂了 622 根，全缺
        range_key@1h      622/622   ← 外部 range_rows 给的，不受影响

    结果是「区间突破看得见、EMA20 全空」这种半残数据安静地流进下游。
    这条测试钉住：只补少量 bar 时，EMA 仍然算得出来。
    """
    sys.path.insert(0, ROOT)
    from ai_trader.kbar.lines import EMA_WARMUP_BARS

    market = str(tmp_path / "m.db")
    kbar = str(tmp_path / "k.db")
    rng = str(tmp_path / "no_range.db")
    _make_kbar_db(kbar)

    base_n = EMA_WARMUP_BARS + 60           # 让第一轮就把 EMA 喂出来
    _make_market_db(market, n=base_n)
    r1 = _run((market, kbar, rng), "--until", "lines")
    assert r1.returncode == 0, r1.stdout + r1.stderr

    con = sqlite3.connect(kbar)
    since = con.execute("SELECT MAX(ts) FROM kbar_line").fetchone()[0]
    con.close()
    assert since, "第一轮没写出任何线"

    # 再追加 60 根新 bar
    new_n = 60
    _make_market_db(market, n=base_n + new_n)
    r2 = _run((market, kbar, rng), "--until", "lines")
    assert r2.returncode == 0, r2.stdout + r2.stderr
    assert "预热窗口" in r2.stdout, "没有走预热分支"

    con = sqlite3.connect(kbar)
    try:
        got = con.execute(
            "SELECT COUNT(*) FROM kbar_line WHERE line_key='ema20@15m'"
            " AND ts > ?", (since,)).fetchone()[0]
    finally:
        con.close()
    assert got == new_n, (
        "新增 %d 根里有 EMA20 的只有 %d 根 —— 预热历史没喂进 lines 层"
        % (new_n, got))


def test_force_rewinds_the_input_window_for_repair(dbs):
    """--force 必须真的把输入窗口往前推。

    否则「修完预热 bug 也补不回缺口」：库已算到最新时 new_bars=0，
    预热窗口正好等于 warmup 根，EMA 需要「之前有 warmup 根」，一根都出不来。
    """
    r = _run(dbs, "--dry-run", "--force")
    assert r.returncode == 0, r.stderr
    # kbar 是空库（since=0），这一轮不会推窗口 —— 先把数据算进去
    r1 = _run(dbs, "--until", "lines")
    assert r1.returncode == 0, r1.stdout + r1.stderr
    r2 = _run(dbs, "--dry-run", "--force")
    assert r2.returncode == 0, r2.stderr
    assert "--force：输入窗口往前推" in r2.stdout


def test_facts_layer_gets_warmup_so_vol_rel_is_not_null(tmp_path):
    """facts 层也要预热，否则最新 bar 的 vol_rel 是 NULL。

    这是和 EMA 完全同一类的 bug，出现得很晚才发现：
    `vol_rel = vol / median(前 VOL_REL_N=20 根)`，只喂新 bar 时前 20 根算不出来，
    而 `bar_facts` 只是把它记进 `stats["skipped"]["vol_rel:warmup"]` 就跳过
    —— **不报错、不出声**，那一列就是 NULL。

    生产实测的痕迹：11-10 与 11-11 各有 20 根 vol_rel 永久为 NULL（正好等于
    VOL_REL_N），11-16/11-17 的增量又在最后几根上留下了新的 NULL。
    """
    sys.path.insert(0, ROOT)
    from ai_trader.kbar import facts as F

    market = str(tmp_path / "m.db")
    kbar = str(tmp_path / "k.db")
    rng = str(tmp_path / "no_range.db")
    _make_kbar_db(kbar)

    base_n = 120
    _make_market_db(market, n=base_n)
    r1 = _run((market, kbar, rng), "--until", "facts")
    assert r1.returncode == 0, r1.stdout + r1.stderr

    con = sqlite3.connect(kbar)
    since = con.execute("SELECT MAX(ts) FROM kbar_evolution").fetchone()[0]
    con.close()

    # 再追加几根新的：数量远小于 VOL_REL_N，正好是"只喂新 bar 就全算不出"的场景
    new_n = 5
    _make_market_db(market, n=base_n + new_n)
    r2 = _run((market, kbar, rng), "--until", "facts")
    assert r2.returncode == 0, r2.stdout + r2.stderr
    assert "facts 预热" in r2.stdout, "没有走 facts 预热分支"

    con = sqlite3.connect(kbar)
    try:
        got, tot = con.execute(
            "SELECT SUM(vol_rel IS NOT NULL), COUNT(*) FROM kbar_evolution"
            " WHERE ts > ?", (since,)).fetchone()
    finally:
        con.close()
    assert tot == new_n, "预期新增 %d 行，实际 %d" % (new_n, tot)
    assert got == new_n, (
        "新增 %d 根里只有 %d 根算出了 vol_rel（VOL_REL_N=%d）—— "
        "facts 层的预热历史没喂进去" % (new_n, got, F.VOL_REL_N))


def test_source_is_not_shadowed_by_a_loop_variable():
    """静态兜底：main() 里不许出现对模块级函数名的赋值。

    动态测试已经能抓到症状，但这条能在「还没跑到」时就说明白病因，
    而且不依赖 subprocess 是否能起来。
    """
    import ast
    path = os.path.join(ROOT, "tools", "ingest_kbar.py")
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)

    module_level = {n.name for n in tree.body
                    if isinstance(n, ast.FunctionDef)}
    assert "_kind" in module_level, "模块级 _kind 不见了，测试前提失效"

    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        assigned = set()
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store):
                assigned.add(sub.id)
            elif isinstance(sub, ast.arg):
                assigned.add(sub.arg)
        clash = assigned & module_level
        assert not clash, (
            "函数 %s() 内部赋值了模块级函数名 %s —— 这会让整个函数体里对该名字的"
            "引用都变成「局部名先用后赋值」，UnboundLocalError 必崩。"
            % (node.name, ", ".join(sorted(clash))))


# --------------------------------------------------------------------------
# 输入源为空 ≠ 没有新 bar：前者必须失败
# --------------------------------------------------------------------------

def _make_market_db_having(path, interval_ms=IV_15M, n=150,
                           only_interval_ms=None):
    """造 market.db。only_interval_ms 给了就只写那一种周期（其余周期为空）。"""
    con = sqlite3.connect(path)
    try:
        con.execute(
            "CREATE TABLE IF NOT EXISTS bars ("
            " symbol TEXT NOT NULL, interval_ms INTEGER NOT NULL,"
            " open_time INTEGER NOT NULL, o REAL, h REAL, l REAL, c REAL, v REAL,"
            " PRIMARY KEY (symbol, interval_ms, open_time))")
        rows = []
        iv = only_interval_ms or interval_ms
        for i in range(n):
            ts = T0 + i * iv
            base = 80_000.0 + i
            rows.append((SYMBOL, iv, ts,
                         base, base + 50.0, base - 50.0, base + 10.0, 100.0 + i))
        con.executemany("INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?,?,?,?)", rows)
        con.commit()
    finally:
        con.close()


def test_empty_source_fails_loudly_instead_of_reporting_success(tmp_path):
    """★ 本文件最重要的一条，对应真实事故。

    2026-11-09 那次 5m 构建：market.db 里没有 5m 行（5m 到 11-09 才进
    config.INTERVALS_MS 成为原生周期），`_load_bars` 拿到空输入后只往 note
    里加一句话就继续跑，main() 退出码 0。于是 5m 演化层停在 11-01，
    而每小时的任务全部「成功」—— 停了 7 天没有任何告警。
    kbar_meta 里的铁证：last_build:BTCUSDT:5m 的 bars_in=34561 / last_ts=11-01，
    写入时间却是 11-09。

    正确行为：「没东西可跑」必须是非 0 退出，并且告诉人怎么补。
    """
    market = str(tmp_path / "market.db")
    kbar = str(tmp_path / "kbar.db")
    _make_market_db_having(market, n=150, only_interval_ms=IV_15M)  # 只有 15m
    _make_kbar_db(kbar)
    dbs = (market, kbar, str(tmp_path / "no_such_range.db"))

    r = _run(dbs, "--dry-run", interval_ms=IV_5M)
    assert r.returncode == 2, (
        "市场库没有 5m 行时必须以 2 退出，实际 %d\n%s\n%s"
        % (r.returncode, r.stdout, r.stderr))
    assert "输入源是空的" in r.stdout
    assert "--from-1m" in r.stdout, "要说清楚怎么补，而不是只说失败"
    assert "Traceback" not in r.stderr


def test_exit_code_2_is_not_confused_with_no_new_bars(dbs):
    """反证：**有**输入源、只是没有新 bar 时，仍然必须是成功。

    这两件事以前输出一模一样（一行提示 + 退出码 0）。
    现在必须能区分开，否则修「空输入」会把「已经最新」也判成故障，
    每小时告警一次，最后所有人都把告警关掉。
    """
    market, kbar, rng = dbs
    r1 = _run(dbs, "--until", "lines")            # 首次：真回填
    assert r1.returncode == 0, r1.stdout + r1.stderr
    r2 = _run(dbs, "--until", "lines")            # 第二次：没有新 bar
    assert r2.returncode == 0, (
        "没有新 bar 是正常状态，不该失败：\n%s" % r2.stdout)
    assert "没有新 bar" in r2.stdout


# --------------------------------------------------------------------------
# 接缝/段内断口：不允许跨洞算 EMA
# --------------------------------------------------------------------------

def test_gap_between_library_tail_and_source_is_refused(tmp_path):
    """库里算到 T，而来源从 T+3 根之后才开始 —— 中间有洞，必须拒绝。

    这正是「11-09 之后原生 5m 突然出现」会造成的形状：不是接着长，
    而是从 11-01 直接跳到 11-09，中间 7 天没有行。
    """
    market = str(tmp_path / "market.db")
    kbar = str(tmp_path / "kbar.db")
    _make_kbar_db(kbar)
    _make_market_db(market, n=150)
    dbs = (market, kbar, str(tmp_path / "no_such_range.db"))

    # 先正常回填前 50 根
    con = sqlite3.connect(market)
    con.execute("DELETE FROM bars WHERE open_time >= ?", (T0 + 50 * IV_15M,))
    con.commit()
    con.close()
    r1 = _run(dbs, "--until", "lines")
    assert r1.returncode == 0, r1.stdout + r1.stderr

    # 再把 50..149 补回去，但**故意跳过 50..59**（造 10 根洞）
    con = sqlite3.connect(market)
    rows = []
    for i in range(60, 150):
        ts = T0 + i * IV_15M
        base = 80_000.0 + i
        rows.append((SYMBOL, IV_15M, ts,
                     base, base + 50.0, base - 50.0, base + 10.0, 100.0 + i))
    con.executemany("INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()

    r2 = _run(dbs, "--until", "lines")
    assert r2.returncode == 2, (
        "接缝有洞时必须拒绝，实际 %d\n%s" % (r2.returncode, r2.stdout))
    assert "接缝断口" in r2.stdout
    assert "缺 10 根" in r2.stdout, r2.stdout
    # 必须给出可执行的下一步（15m 没有回填工具，提示是「补满 market.db」；
    # 5m 才会指向 tools/backfill_5m_bars.py）
    assert "补满" in r2.stdout or "backfill" in r2.stdout


def test_allow_gaps_is_an_explicit_escape_hatch(tmp_path):
    """带洞数据上硬算必须显式开口，不能是默认行为。"""
    market = str(tmp_path / "market.db")
    kbar = str(tmp_path / "kbar.db")
    _make_kbar_db(kbar)
    _make_market_db(market, n=150)
    dbs = (market, kbar, str(tmp_path / "no_such_range.db"))

    con = sqlite3.connect(market)
    con.execute("DELETE FROM bars WHERE open_time >= ?", (T0 + 50 * IV_15M,))
    con.commit()
    con.close()
    assert _run(dbs, "--until", "lines").returncode == 0

    con = sqlite3.connect(market)
    rows = []
    for i in range(60, 150):
        ts = T0 + i * IV_15M
        base = 80_000.0 + i
        rows.append((SYMBOL, IV_15M, ts,
                     base, base + 50.0, base - 50.0, base + 10.0, 100.0 + i))
    con.executemany("INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()

    r = _run(dbs, "--until", "lines", "--allow-gaps", "--dry-run")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "--allow-gaps" in r.stdout
    assert "递推值是错的" in r.stdout, "开了口子也得把风险说清楚"


def test_contiguous_input_is_not_blocked(dbs):
    """反证：数据连续时不许因为这两道闸而失败。"""
    r = _run(dbs, "--until", "lines")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "断口" not in r.stdout
