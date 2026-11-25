# -*- coding: utf-8 -*-
"""回填工具：`tools/backfill_5m_bars.py`。

重点不是「能不能拉」，而是这几件事，每一件都对应一次「看起来成功」的事故：

1. fetcher 可注入（本地无外网，必须能测）
2. 幂等：重复跑不多行，已有行不被覆盖
3. 翻页：超过单页上限要接着翻，不能只拿第一页就收工
4. 未收盘的 K 线不许落库（它的 h/l/c 还会变，而且增量不会回头重算）
5. 自检：补完必须验证「洞还在不在」，不能在还有洞时说成功
6. --fill-gaps 必须补到最新，不能只补洞（否则新鲜度闸会变绿）
"""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import backfill_5m_bars as BF          # noqa: E402

IV = 300_000
SYMBOL = "BTCUSDT"
# 取一个整 5m 的时刻做起点
T0 = 1_700_000_000_000 - (1_700_000_000_000 % IV)


def _make_market_db(path, span=None, interval_ms=IV):
    """建表；span=(起始 ts, 根数) 给了就预置一段连续数据。

    ★ schema 必须抄**生产**的那一份：`updated_at INTEGER NOT NULL` 且没有默认值。
      这个细节曾经让工具骗人 —— 漏写 updated_at 触 NOT NULL 违反，
      而 `INSERT OR IGNORE` 把它静默吞掉，于是「一根都没进库」被报告成
      「已存在跳过」。测试若用简化 schema，这个 bug 永远测不出来。
    """
    con = BF._open_rw(str(path))
    if span:
        start, n = span
        con.executemany(
            "INSERT INTO bars(symbol,interval_ms,open_time,o,h,l,c,v,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            [(SYMBOL, interval_ms, start + i * interval_ms,
              100.0 + i, 101.0 + i, 99.0 + i, 100.5 + i, 10.0, 1)
             for i in range(n)])
    con.commit()
    con.close()


class FakeFetcher:
    """按请求区间生成连续 5m K 线，并记录每次调用的参数。

    `page_cap` 模拟交易所单页上限，用来验证翻页。
    `close_time_lag_ms` 让某些根的 close_time 落在「未来」，模拟未收盘。
    """

    def __init__(self, page_cap=3, forming_at=None, fail_after=None,
                 skip_ts=()):
        self.page_cap = page_cap
        self.forming_at = set(forming_at or ())
        self.fail_after = fail_after
        self.skip_ts = set(skip_ts)
        self.calls = []

    def fetch(self, symbol, interval, start_ms, end_ms, limit=1500):
        self.calls.append((start_ms, end_ms, limit))
        if self.fail_after is not None and len(self.calls) > self.fail_after:
            raise BF.FetchError("假装网络挂了")
        cap = min(int(limit), int(self.page_cap))
        out = []
        ts = start_ms
        while ts <= end_ms and len(out) < cap:
            if ts not in self.skip_ts:
                base = 100.0 + ((ts // IV) % 1000)
                close_time = ts + IV - 1
                if ts in self.forming_at:
                    close_time = 9 * 10 ** 15          # 远超 now，必被丢
                out.append([ts, base, base + 1.0, base - 1.0, base + 0.5,
                            10.0, close_time])
            ts += IV
        return out


@pytest.fixture()
def market(tmp_path):
    p = str(tmp_path / "market.db")
    _make_market_db(p)
    return p


T_NOW = T0 + 1000 * IV         # 固定的“现在”，避免测试依赖真实时钟


# ---------------------------------------------------------------- 基本补洞

def test_fills_a_hole_between_two_existing_rows(market):
    # 已有 0..9 和 15..20，中间 10..14 是洞
    _make_market_db(market, span=(T0, 10))
    con = BF._open_rw(market)
    con.executemany(
        "INSERT INTO bars(symbol,interval_ms,open_time,o,h,l,c,v,updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        [(SYMBOL, IV, T0 + i * IV, 1.0, 2.0, 0.5, 1.5, 1.0, 1)
         for i in range(15, 21)])
    con.commit()
    con.close()

    holes = BF.find_holes(market, SYMBOL, IV)
    assert holes == [(T0 + 9 * IV, T0 + 15 * IV, 5)], holes

    # now_ms 贴着已有尾巴：这样只测「补洞」，不把「补到最新」混进来
    st = BF.run(FakeFetcher(page_cap=100), market, SYMBOL,
                interval="5m", interval_ms=IV, fill_gaps=True,
                now_ms=T0 + 20 * IV, out=lambda *a: None)
    assert st["ok"], st
    assert st["written"] == 5
    assert st["holes_after"] == []
    # 洞两侧已有的行一根都不许动
    assert st["skipped_existing"] >= 4


def test_existing_rows_are_not_overwritten_by_default(market):
    """默认只补空：已有行的 OHLC 必须原样保留。"""
    _make_market_db(market, span=(T0, 20))
    con = BF._open_rw(market)
    con.execute(
        "UPDATE bars SET o=777.0 WHERE open_time=?", (T0 + 3 * IV,))
    con.commit()
    con.close()

    # FakeFetcher 会给出 base+0 的 o，若被覆盖就不再是 777
    st = BF.run(FakeFetcher(page_cap=100), market, SYMBOL, interval="5m",
                interval_ms=IV, from_ms=T0, to_ms=T0 + 19 * IV,
                now_ms=T_NOW, out=lambda *a: None)
    assert st["skipped_existing"] == 20
    assert st["written"] == 0
    con = sqlite3.connect(market)
    got = con.execute("SELECT o FROM bars WHERE open_time=?",
                      (T0 + 3 * IV,)).fetchone()[0]
    con.close()
    assert got == pytest.approx(777.0), "已有行被回填工具覆盖了"


def test_overwrite_flag_really_overwrites(market):
    _make_market_db(market, span=(T0, 5))
    con = BF._open_rw(market)
    con.execute("UPDATE bars SET o=777.0 WHERE open_time=?", (T0 + 3 * IV,))
    con.commit()
    con.close()

    st = BF.run(FakeFetcher(page_cap=100), market, SYMBOL, interval="5m",
                interval_ms=IV, from_ms=T0, to_ms=T0 + 4 * IV,
                overwrite=True, now_ms=T_NOW, out=lambda *a: None)
    assert st["written"] >= 1
    con = sqlite3.connect(market)
    got = con.execute("SELECT o FROM bars WHERE open_time=?",
                      (T0 + 3 * IV,)).fetchone()[0]
    con.close()
    assert got != pytest.approx(777.0)


def test_is_idempotent(market):
    _make_market_db(market, span=(T0, 10))
    f1 = FakeFetcher(page_cap=100)
    st1 = BF.run(f1, market, SYMBOL, interval="5m", interval_ms=IV,
                 from_ms=T0, to_ms=T0 + 9 * IV, now_ms=T_NOW,
                 out=lambda *a: None)
    assert st1["written"] == 0, "已经全有，不该写新行"

    # 再来一次补洞+补尾，行数不该翻倍
    con = sqlite3.connect(market)
    before = con.execute("SELECT COUNT(*) FROM bars").fetchone()[0]
    con.close()
    BF.run(FakeFetcher(page_cap=100), market, SYMBOL, interval="5m",
           interval_ms=IV, fill_gaps=True, now_ms=T_NOW, out=lambda *a: None)
    con = sqlite3.connect(market)
    after = con.execute("SELECT COUNT(*) FROM bars").fetchone()[0]
    dup = con.execute(
        "SELECT COUNT(*) FROM (SELECT open_time FROM bars "
        "GROUP BY symbol, interval_ms, open_time HAVING COUNT(*) > 1)"
    ).fetchone()[0]
    con.close()
    assert dup == 0, "出现重复行：幂等性坏了"
    assert after >= before


# ---------------------------------------------------------------- 翻页

def test_pages_through_a_long_range(market):
    """单页只给 3 根时，必须一路翻到区间末尾，不能拿一页就收工。"""
    f = FakeFetcher(page_cap=3)
    st = BF.run(f, market, SYMBOL, interval="5m", interval_ms=IV,
                from_ms=T0, to_ms=T0 + 29 * IV, now_ms=T_NOW,
                out=lambda *a: None)
    assert st["ok"], st
    assert st["written"] == 30, "翻页不完整：只拿到 %d 根" % st["written"]
    assert len(f.calls) >= 10, "页数太少，说明没有真翻页"


def test_pagination_terminates_when_the_exchange_stops_advancing(market):
    """交易所不前进时必须跳出，不能死循环。"""

    class Stuck:
        def __init__(self):
            self.n = 0

        def fetch(self, symbol, interval, start_ms, end_ms, limit=1500):
            self.n += 1
            # 永远只回同一根，且不前进
            return [[T0, 1.0, 2.0, 0.5, 1.5, 1.0, T0 + IV - 1]]

    s = Stuck()
    st = BF.run(s, market, SYMBOL, interval="5m", interval_ms=IV,
                from_ms=T0, to_ms=T0 + 500 * IV, now_ms=T_NOW,
                out=lambda *a: None)
    assert s.n < 10, "没有前进还在一直翻页（死循环风险）"
    assert st["pages"] < 10


# ---------------------------------------------------------------- 未收盘

def test_forming_bar_is_not_written(market):
    """未收盘那根的 close_time 在未来，必须丢掉。

    落了它，库里就有一根 h/l/c 还会变的假数据，而增量只处理
    `ts > since`，永远轮不到它重算。
    """
    sorting = T0 + 5 * IV
    f = FakeFetcher(page_cap=100, forming_at=[sorting])
    st = BF.run(f, market, SYMBOL, interval="5m", interval_ms=IV,
                from_ms=T0, to_ms=T0 + 9 * IV, now_ms=T_NOW,
                out=lambda *a: None)
    assert st["skipped_forming"] == 1
    con = sqlite3.connect(market)
    n = con.execute("SELECT COUNT(*) FROM bars WHERE open_time=?",
                    (sorting,)).fetchone()[0]
    con.close()
    assert n == 0, "未收盘的 K 线被写进库了"


def test_parse_klines_drops_forming_by_close_time():
    now = T0 + 10 * IV
    raw = [
        [T0, 1.0, 2.0, 0.5, 1.5, 1.0, T0 + IV - 1],               # 已收盘
        [T0 + IV, 1.0, 2.0, 0.5, 1.5, 1.0, now + 10 * IV],        # 未来 → 丢
    ]
    rows, forming = BF._parse_klines(raw, IV, now_ms=now)
    assert len(rows) == 1
    assert forming == 1


# ---------------------------------------------------------------- 自检

def test_reports_failure_when_a_hole_survives(market):
    """fetcher 故意漏掉一根 → 补完还有洞 → 必须 ok=False。

    这个工具自己绝不能变成「看起来成功」的那一环。
    """
    _make_market_db(market, span=(T0, 20))
    con = BF._open_rw(market)
    con.execute("DELETE FROM bars WHERE open_time=?", (T0 + 10 * IV,))
    con.commit()
    con.close()
    assert BF.find_holes(market, SYMBOL, IV)

    f = FakeFetcher(page_cap=100, skip_ts=[T0 + 10 * IV])
    st = BF.run(f, market, SYMBOL, interval="5m", interval_ms=IV,
                fill_gaps=True, now_ms=T_NOW, out=lambda *a: None)
    assert st["ok"] is False, "还有洞却报成功"
    assert st["holes_after"], st


def test_network_failure_is_reported_not_raised(market):
    f = FakeFetcher(page_cap=100, fail_after=0)
    st = BF.run(f, market, SYMBOL, interval="5m", interval_ms=IV,
                from_ms=T0, to_ms=T0 + 9 * IV, now_ms=T_NOW,
                out=lambda *a: None)
    assert st["ok"] is False
    assert "网络挂了" in st["error"]


def test_pages_already_fetched_are_kept_when_the_network_dies_midway(market):
    """★ 中途断线时，前面拉到的页必须已经落库。

    实测踩过：36 页的任务攒在内存里最后一次性写，第 N 页 URLError 之后
    整轮 `written=0`，前面十几页白拉。逐页落库之后这个工具可重入 ——
    再跑一次接着补，幂等 insert 不会多一行。
    """
    # 单页 3 根，拉到第 3 次调用时断网 → 前两页（6 根）应当已经进库
    f = FakeFetcher(page_cap=3, fail_after=2)
    st = BF.run(f, market, SYMBOL, interval="5m", interval_ms=IV,
                from_ms=T0, to_ms=T0 + 29 * IV, now_ms=T_NOW,
                out=lambda *a: None)
    assert st["ok"] is False
    assert st["written"] == 6, "断线前拉到的 6 根没有落库：written=%d" % st["written"]
    assert len(BF.existing_5m_ts(market, SYMBOL, IV)) == 6
    assert st["stopped_at"] is not None

    # 重跑一次（这次不断网）→ 应当补齐到 30 根，且不产生重复
    st2 = BF.run(FakeFetcher(page_cap=3), market, SYMBOL, interval="5m",
                 interval_ms=IV, from_ms=T0, to_ms=T0 + 29 * IV,
                 now_ms=T_NOW, out=lambda *a: None)
    assert st2["ok"], st2
    con = sqlite3.connect(market)
    n = con.execute("SELECT COUNT(*) FROM bars WHERE interval_ms=?",
                    (IV,)).fetchone()[0]
    dup = con.execute(
        "SELECT COUNT(*) FROM (SELECT open_time FROM bars "
        "GROUP BY symbol, interval_ms, open_time HAVING COUNT(*) > 1)"
    ).fetchone()[0]
    con.close()
    assert n == 30, n
    assert dup == 0


def test_missing_market_db_is_reported(tmp_path):
    st = BF.run(FakeFetcher(), str(tmp_path / "nope.db"), SYMBOL,
                interval="5m", interval_ms=IV, fill_gaps=True,
                now_ms=T_NOW, out=lambda *a: None)
    assert st["ok"] is False
    assert "找不到 market.db" in st["error"]


# ---------------------------------------------------------------- 补到最新

def test_fill_gaps_extends_all_the_way_to_now(market):
    """★ 只补洞不补尾 = 把「停了 7 天」修成「停在今天」，更糟。

    新鲜度闸看的是 MAX(ts)，尾巴不补它就会变绿，
    于是「数据不新鲜」这件事被一个「修好了」的动作掩盖掉。
    """
    _make_market_db(market, span=(T0, 10))
    now = T0 + 100 * IV
    st = BF.run(FakeFetcher(page_cap=200), market, SYMBOL, interval="5m",
                interval_ms=IV, fill_gaps=True, now_ms=now,
                out=lambda *a: None)
    assert st["ok"], st
    ts = sorted(BF.existing_5m_ts(market, SYMBOL, IV))
    assert ts[-1] >= now - IV, "没有补到最新：最后一根是 %d，now=%d" % (ts[-1], now)
    assert len(ts) >= 99


def test_fill_gaps_on_an_empty_table_says_what_to_do(market):
    st = BF.run(FakeFetcher(), market, SYMBOL, interval="5m",
                interval_ms=IV, fill_gaps=True, now_ms=T_NOW,
                out=lambda *a: None)
    assert st["ok"] is False
    assert "--from" in st["error"]


# ---------------------------------------------------------------- 命令行

def test_dry_run_does_not_touch_network_or_db(market):
    _make_market_db(market, span=(T0, 10))
    f = FakeFetcher(page_cap=100)
    st = BF.run(f, market, SYMBOL, interval="5m", interval_ms=IV,
                fill_gaps=True, dry_run=True, now_ms=T_NOW,
                out=lambda *a: None)
    assert st["ok"] and st["pages"] == 0
    assert f.calls == [], "dry-run 居然发了网络请求"


def test_cli_dry_run_exits_zero(market, capsys):
    _make_market_db(market, span=(T0, 10))
    rc = BF.main(["--market-db", market, "--symbol", SYMBOL,
                  "--fill-gaps", "--dry-run"])
    assert rc == 0


def test_cli_unknown_market_db_exits_two(tmp_path):
    rc = BF.main(["--market-db", str(tmp_path / "nope.db"), "--fill-gaps"])
    assert rc == 2


# --------------------------------------------------- 写库诚实性（踩过的坑）

def test_write_actually_lands_a_row_on_the_production_schema(market):
    """★ 真实事故的回归测试：写了 0 行却报告「已存在跳过 1 行」。

    生产 `bars` 表的 `updated_at INTEGER NOT NULL` 没有默认值。
    当初漏写这个字段 + `INSERT OR IGNORE`，NOT NULL 违反被静默吞掉，
    于是「一根都没进库」被报告成「已存在跳过」—— 工具自己在骗人。
    """
    rows = [(T0, 100.0, 101.0, 99.0, 100.5, 10.0)]
    st = BF.write_bars(market, SYMBOL, IV, rows)
    assert st["written"] == 1, st
    assert st["skipped_existing"] == 0, st

    con = sqlite3.connect(market)
    got = con.execute(
        "SELECT o, h, l, c, v, updated_at FROM bars WHERE open_time=?",
        (T0,)).fetchone()
    con.close()
    assert got is not None, "报告写了 1 行，库里其实没有"
    assert got[0] == pytest.approx(100.0)
    assert got[5] not in (None, 0), "updated_at 没写"


def test_a_second_write_really_skips_and_says_so(market):
    rows = [(T0, 100.0, 101.0, 99.0, 100.5, 10.0)]
    assert BF.write_bars(market, SYMBOL, IV, rows)["written"] == 1
    st = BF.write_bars(market, SYMBOL, IV, rows)
    assert st["written"] == 0
    assert st["skipped_existing"] == 1


def test_unknown_not_null_column_raises_instead_of_reporting_skipped(market):
    """表里多一个我们不认识的 NOT NULL 列时，必须**抛错**。

    `INSERT OR IGNORE` 的坏处就在这里：它把「写不进去」伪装成「已经有了」。
    换成 `ON CONFLICT DO NOTHING` 之后，只有主键冲突会被忽略。
    """
    con = sqlite3.connect(market)
    con.execute("ALTER TABLE bars ADD COLUMN must_have REAL NOT NULL DEFAULT 0")
    con.execute("UPDATE bars SET must_have=0")   # 默认值能顶住，先证实能写
    con.commit()
    con.close()

    # 把默认值去掉做不了（SQLite 限制），所以直接造一张结构不兼容的表
    other = os.path.join(os.path.dirname(market), "strict.db")
    con = sqlite3.connect(other)
    con.execute(
        "CREATE TABLE bars (symbol TEXT NOT NULL, interval_ms INTEGER NOT NULL,"
        " open_time INTEGER NOT NULL, o REAL, h REAL, l REAL, c REAL, v REAL,"
        " need_this INTEGER NOT NULL,"
        " PRIMARY KEY (symbol, interval_ms, open_time))")
    con.commit()
    con.close()
    # 抛哪种 sqlite3 错误取决于差异在哪儿（缺列是 OperationalError，
    # 违的是 NOT NULL 是 IntegrityError）—— 关键是**必须抛**，
    # 绝不能返回「written=0, skipped_existing=1」那种假话。
    with pytest.raises(sqlite3.Error):
        BF.write_bars(other, SYMBOL, IV,
                      [(T0, 100.0, 101.0, 99.0, 100.5, 10.0)])


# ---------------------------------------------------------------- from-kbar

def test_from_kbar_uses_the_earliest_history_the_downstream_needs(tmp_path):
    """--from-kbar 的起点 = kbar.db 里最早的一根，省得运维手算毫秒。

    下游（amp5、止损下限）要和 15m 一样长才不漏，所以 5m 没有时退到 15m。
    """
    kbar = str(tmp_path / "kbar.db")
    sys.path.insert(0, ROOT)
    from ai_trader.kbar.store import KBarStore
    with KBarStore(kbar) as st:
        pass
    con = sqlite3.connect(kbar)
    con.executemany(
        "INSERT INTO kbar_evolution"
        " (symbol, interval_ms, ts, idx, o, h, l, c, vol)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        [(SYMBOL, 900_000, T0 - 50 * IV, (T0 - 50 * IV) // 900_000,
          1.0, 2.0, 0.5, 1.5, 1.0),
         (SYMBOL, IV, T0, T0 // IV, 1.0, 2.0, 0.5, 1.5, 1.0)])
    con.commit()
    con.close()
    assert BF.kbar_earliest_ts(kbar, SYMBOL, IV) == T0 - 50 * IV


def test_from_kbar_missing_kbar_db_returns_none(tmp_path):
    assert BF.kbar_earliest_ts(str(tmp_path / "nope.db"), SYMBOL, IV) is None


def test_cli_from_kbar_without_history_exits_two(tmp_path):
    kbar = str(tmp_path / "kbar.db")
    sys.path.insert(0, ROOT)
    from ai_trader.kbar.store import KBarStore
    with KBarStore(kbar) as st:
        pass
    rc = BF.main(["--market-db", str(tmp_path / "m.db"), "--fb" if False else
                  "--from-kbar", "--kbar-db", kbar, "--dry-run"])
    assert rc == 2


def test_only_from_means_backfill_up_to_now(market):
    """只给 --from 时，to 归一为「现在」；不归一就只会补一根，不报错。

    ★ 补回来的最后一根必然是「还没收盘」的那根（close_time 在未来），
      被 _parse_klines 丢掉 —— 所以是 30 根而不是 31 根。
      这一条同时钉住了两个行为：归一化 + 未收盘不落库。
    """
    st = BF.run(FakeFetcher(page_cap=200), market, SYMBOL, interval="5m",
                interval_ms=IV, from_ms=T0, to_ms=None,
                now_ms=T0 + 30 * IV, out=lambda *a: None)
    assert st["ok"], st
    assert st["written"] == 30, st["written"]
    assert st["skipped_forming"] == 1, "最后一根未收盘，必须被丢掉"
