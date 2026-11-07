# -*- coding: utf-8 -*-
"""跨线程访问 store —— 线上才暴露的那一类 bug。

为什么单独立一个文件
--------------------
Flask 的 test_client 在**同一个线程**里跑视图函数，而 waitress 用工作线程池。
所以「连接在主线程建、视图在工作线程用」这件事，任何走 test_client 的测试
都永远碰不到——线上却是 100% 触发。

实际发生过：部署后 /api/config/sizing 和 /api/range/evo 返回 500，
日志里是 sqlite3.ProgrammingError: created in a thread can only be used in
that same thread；而本地全套测试当时全绿。

修复是 check_same_thread=False + 实例锁。这个文件就是那道防线：
如果哪天有人把 lock 去掉或改回默认连接参数，这里会红。
"""
from __future__ import annotations

import threading

import pytest


def _run_in_thread(fn):
    """在新线程里执行，把异常原样带回来。"""
    box = {}

    def work():
        try:
            box["ok"] = fn()
        except Exception as e:                      # noqa: BLE001
            box["err"] = e

    t = threading.Thread(target=work)
    t.start()
    t.join(15)
    assert not t.is_alive(), "工作线程超时（可能死锁）"
    if "err" in box:
        raise box["err"]
    return box.get("ok")


def test_settings_store_cross_thread(tmp_path):
    from ai_trader.settings import SettingsStore

    path = str(tmp_path / "s.db")
    st = SettingsStore(path)                        # 主线程建连接
    try:
        got = _run_in_thread(lambda: st.sizing())   # 另一线程读
        assert got["sizing.risk_pct"] == 5.0
        assert _run_in_thread(lambda: st.set_sizing(200.0, 3.0, 15.0))[0]
        assert _run_in_thread(lambda: st.sizing())["sizing.capital"] == 200.0
    finally:
        st.close()


def test_range_store_cross_thread(tmp_path):
    from ai_trader.range.store import RangeStore

    st = RangeStore(str(tmp_path / "r.db"))
    try:
        rows = [(1760000000000, "2026-10-09 16:00", 81000.0, 79000.0, 82000.0,
                 None, 78500.0, 1, 1, "DOWN", "birth")]
        assert _run_in_thread(lambda: st.upsert_states("BTCUSDT", rows)) == 1
        got = _run_in_thread(lambda: st.states("BTCUSDT"))
        assert len(got) == 1
        got2 = _run_in_thread(lambda: st.state_at_dict("BTCUSDT", 1760000000000))
        assert got2["family"] == 1
        assert _run_in_thread(lambda: st.counts("BTCUSDT"))["range_evolution"] == 1
    finally:
        st.close()


def test_subscription_store_cross_thread(tmp_path):
    from ai_trader.market.subscription import SubscriptionStore

    st = SubscriptionStore(str(tmp_path / "s.db"))
    try:
        assert _run_in_thread(lambda: st.add("ethusdt")) == "ETHUSDT"
        assert _run_in_thread(lambda: st.symbols()) == ["ETHUSDT"]
        assert _run_in_thread(lambda: st.is_subscribed("ETHUSDT")) is True
    finally:
        st.close()


def test_concurrent_writers_do_not_corrupt(tmp_path):
    """8 个线程同时写同一个库：要么都成功，要么报错，不能写出半条记录。"""
    from ai_trader.settings import SettingsStore

    st = SettingsStore(str(tmp_path / "s.db"))
    errs = []
    try:
        def work(i):
            def inner():
                for _ in range(10):
                    st.set_sizing(100.0 + i, 5.0, 22.0)
                    st.sizing()
            return inner
        threads = [threading.Thread(target=lambda i=i: _safe(_run_in_thread, work(i), errs))
                   for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        assert not errs, "并发写入报错: %r" % (errs[:3],)
        s = st.sizing()
        assert s["sizing.risk_pct"] == 5.0            # 库没被写坏
        assert 100.0 <= s["sizing.capital"] <= 108.0  # 是某个线程写的值之一
    finally:
        st.close()


def _safe(fn, arg, errs):
    try:
        fn(arg)
    except Exception as e:                          # noqa: BLE001
        errs.append(e)
