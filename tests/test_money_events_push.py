# -*- coding: utf-8 -*-
"""A：钱动了就要叫人（飞书推送）。

背景（2026-11-25）：全仓唯一的 notify 调用在 `guard._event(alert=True)` 里
—— 只有硬停/对账不一致才会推飞书。开仓成交、平仓、拒单这些「钱真的动了」
的时刻一律只进本地库，人在手机上看不到。用户原话：「干等也不是事」。

钉三件事：
  1. 四种事件真的会推（而且内容里有该有的数字）
  2. 通知抛异常**不许**把交易搞崩（feishu 走 urllib，会超时）
  3. 节流按 key 生效（-2019 死循环那种每 5 分钟一轮，不节流等于把频道静音）
"""
import asyncio
import inspect
from types import SimpleNamespace

import pytest

from ai_trader.trading import engine as E


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(asyncio.new_event_loop())


def _st(notify=None):
    st = SimpleNamespace()
    st.guard = SimpleNamespace(notify=notify) if notify else None
    return st


def test_push_sends_title_and_body():
    sent = []
    st = _st(lambda t, b: sent.append((t, b)))
    E.DecisionEngine._push(st, "标题", "正文")
    assert sent == [("标题", "正文")]


def test_push_is_silent_without_a_notifier():
    """没配 webhook → 静默跳过，不是错误。"""
    st = _st(None)
    E.DecisionEngine._push(st, "标题", "正文")      # 不许抛
    st2 = SimpleNamespace(guard=SimpleNamespace(notify=None))
    E.DecisionEngine._push(st2, "标题", "正文")


def test_push_swallows_errors_from_the_notifier():
    """通知抛异常绝不能把交易搞崩（止损这一轮必须照跑）。"""
    def boom(t, b):
        raise RuntimeError("网络断了")
    st = _st(boom)
    E.DecisionEngine._push(st, "标题", "正文")      # 不许抛


def test_push_throttles_by_key():
    """同 key 在窗口内只推一次；不同 key 互不影响。"""
    sent = []
    st = _st(lambda t, b: sent.append(t))
    for _ in range(5):
        E.DecisionEngine._push(st, "A", "x", key="k1", min_gap_s=3600)
    assert len(sent) == 1, "同 key 没节流：%r" % (sent,)
    E.DecisionEngine._push(st, "B", "x", key="k2", min_gap_s=3600)
    assert len(sent) == 2, "不同 key 被误节流"


def test_unthrottled_events_are_never_swallowed():
    """开仓/平仓不节流：每一次都必须推。"""
    sent = []
    st = _st(lambda t, b: sent.append(t))
    for _ in range(3):
        E.DecisionEngine._push(st, "开仓", "x")
    assert len(sent) == 3


# ------------------------------------------------- 接线（源码事实）

def test_open_and_close_push():
    for fn in (E.DecisionEngine._open, E.DecisionEngine._close):
        src = inspect.getsource(fn)
        assert "_push(" in src, "%s 里没有推送调用" % fn.__name__


def test_pending_fill_and_pending_close_push():
    for fn in (E.DecisionEngine._pending_book_fill,
               E.DecisionEngine._pending_close):
        assert "_push(" in inspect.getsource(fn), \
            "%s 里没有推送调用" % fn.__name__


def test_gate_rejections_push_with_throttle():
    """两条闸的拒单要推，而且必须带节流（否则 -2019 死循环会刷屏）。"""
    for fn in (E.DecisionEngine._gate_pending_unattended,
               E.DecisionEngine._gate_pending_size,
               E.DecisionEngine._gate_pending_margin):
        src = inspect.getsource(fn)
        assert "_push(" in src, "%s 拒单没有推送" % fn.__name__
        assert "min_gap_s" in src, (
            "%s 的推送没有节流 —— 每 5 分钟一轮的死循环会把通知频道刷成"
            "静音，那时候真出事也没人看" % fn.__name__)
