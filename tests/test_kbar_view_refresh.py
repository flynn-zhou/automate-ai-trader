# -*- coding: utf-8 -*-
"""把关：K 线视图不许是启动时的一次性快照。

这个坑我原样重犯了一遍
----------------------

`main.py` 给 `pending_lines` 特意返回**可调用对象**，注释写着：

> LineBook 是「截至某收盘时刻已就绪」的静态快照…启动时读一次，
> 跑到第三天用的还是第一天的线 —— `ema20` 失效判定会拿一根三天前的
> 均线去比，而且不会报错。

而我第一版的 `kbar_view_for()` 就是启动时 `load_kbar_view()` 一次，
返回那个静态对象。于是 S5 三道闸在长跑里会一直拿**启动那天**的 K 线事实
去判出场：不会红、不会报错，只会表现为「三道闸从来不触发」，
或者按几天前的 K 线触发。

这条测试的存在就是为了让下一次「图省事返回静态对象」立刻变红。
"""
import os
import sqlite3
import time

import pytest

from ai_trader.trading.kbar_view import (KBarView, RefreshableKBarView,
                                         load_kbar_view)

SYM = "BTCUSDT"
IV = 300_000


def _make_db(path, bars):
    """造一个最小 kbar.db：`bars` 是 [(ts, label, side)]。"""
    con = sqlite3.connect(path)
    con.execute("create table kbar_shape (ts integer, interval_ms integer,"
                " symbol text, label text, vol_rel real)")
    con.execute("create table kbar_reaction (ts integer, interval_ms integer,"
                " symbol text, side text, vol_rel real, pen_depth_pct real,"
                " line_key text)")
    for ts, label, side in bars:
        con.execute("insert into kbar_shape values (?,?,?,?,?)",
                    (ts, IV, SYM, label, 1.0))
        if side:
            con.execute("insert into kbar_reaction values (?,?,?,?,?,?,?)",
                        (ts, IV, SYM, side, 1.0, 0.5, "ema50@5m"))
    con.commit()
    con.close()


def _append(path, ts, label, side="from_above"):
    con = sqlite3.connect(path)
    con.execute("insert into kbar_shape values (?,?,?,?,?)",
                (ts, IV, SYM, label, 1.0))
    con.execute("insert into kbar_reaction values (?,?,?,?,?,?,?)",
                (ts, IV, SYM, side, 1.0, 0.5, "ema50@5m"))
    con.commit()
    con.close()


def test_static_view_does_not_see_new_bars(tmp_path):
    """先把坑本身钉住：静态 `KBarView` 就是看不见新数据。"""
    db = str(tmp_path / "kbar.db")
    _make_db(db, [(1000, "无·贴线", None)])
    v = load_kbar_view(db)
    assert v.at(2000) is None

    _append(db, 2000, "恐慌盘")
    assert v.at(2000) is None, "静态视图居然看见了新数据？（那这条测试就没意义了）"


def test_refreshable_view_sees_new_bars(tmp_path):
    """★ 可刷新代理必须看见启动之后才落库的 K 线。"""
    db = str(tmp_path / "kbar.db")
    _make_db(db, [(1000, "无·贴线", None)])
    v = RefreshableKBarView(db)
    assert v.at(1000) is not None
    assert v.at(2000) is None

    _append(db, 2000, "恐慌盘")
    got = v.at(2000)
    assert got is not None, "新 K 线看不见 —— 视图是一次性快照"
    assert got["label"] == "恐慌盘"
    assert got["side"] == "from_above"


def test_refreshable_view_reloads_on_ttl(tmp_path):
    """没被问到新 K 也算数：TTL 到期要重载（数据可能被回填/修正）。"""
    db = str(tmp_path / "kbar.db")
    _make_db(db, [(1000, "无·贴线", None)])
    v = RefreshableKBarView(db, ttl_ms=10)
    assert len(v) == 1

    _append(db, 2000, "恐慌盘")
    time.sleep(0.05)
    assert len(v) == 2, "TTL 到期没有重载"


def test_reload_failure_keeps_the_old_view(tmp_path):
    """重载失败不许把视图清空。

    ★ 一次临时的 `database is locked` 不该把所有持仓的出场判据都关掉 ——
      返回空视图 = 三道闸立刻全部失效，而持仓还在。
    """
    db = str(tmp_path / "kbar.db")
    _make_db(db, [(1000, "无·贴线", None)])
    v = RefreshableKBarView(db, ttl_ms=10)
    before = len(v)

    os.remove(db)                     # 模拟库暂时不可用
    time.sleep(0.05)
    assert len(v) == before, "重载失败后视图被清空了"
    assert v.at(1000) is not None, "旧事实丢了"


def test_main_returns_a_refreshable_view(tmp_path):
    """`main.kbar_view_for()` 必须返回可刷新代理，不是 `KBarView`。"""
    from ai_trader import main as M

    db = str(tmp_path / "kbar.db")
    _make_db(db, [(1000, "无·贴线", None)])

    class _Cfg:
        class store:
            path = db
        class trading:
            strategy = "pending"
            symbol = SYM

    v = M.kbar_view_for(_Cfg())
    assert v is not None
    assert isinstance(v, RefreshableKBarView), (
        "main 返回了 %r —— 静态快照会让 S5 在长跑里用启动那天的 K 线"
        % type(v).__name__)
    assert isinstance(v.view, KBarView)


def test_range_db_resolves_to_the_production_name(tmp_path):
    """★ 区间库在生产上叫 `market_range.db`。

    第一版把本地分析产物 `market_range_rebuilt.db` 写成了默认值，
    结果是本地跑得通、服务器上计划源直接返回 None —— 永远不产计划。
    """
    from ai_trader.trading.plan_source import resolve_range_db

    prod = tmp_path / "market_range.db"
    prod.write_bytes(b"")
    assert os.path.basename(resolve_range_db(str(tmp_path))) == "market_range.db"

    # 只有本地副本时也要能用（本地回放）
    prod.unlink()
    (tmp_path / "market_range_rebuilt.db").write_bytes(b"")
    assert os.path.basename(
        resolve_range_db(str(tmp_path))) == "market_range_rebuilt.db"

    # 两个都没有：返回空串，由调用方报错 —— 不许瞎猜一个路径
    (tmp_path / "market_range_rebuilt.db").unlink()
    assert resolve_range_db(str(tmp_path)) == ""
    assert resolve_range_db("") == ""


def test_production_range_db_name_is_the_documented_one():
    """与 `kbar/build.py` 的权威口径对齐，别各写一个。"""
    from ai_trader.kbar import build as KB
    from ai_trader.trading import plan_source as PS

    assert os.path.basename(KB.DEFAULT_RANGE_DB) == "market_range.db"
    assert os.path.basename(PS.DEFAULT_RANGE_DB) == "market_range.db"
