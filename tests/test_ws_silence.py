# -*- coding: utf-8 -*-
"""WS 静默检测的回归防线。

背景：币安 @trade 流里混着 {"p":"0","q":"0","X":"NA"} 的环境噪声，
实测 20 秒 137 条里 2 条（约 1.4%），还有 X=INSURANCE_FUND 的保险基金
结算事件（p/q 就是 0，属于正常市场事件，不是解析错）。

问题不在丢弃它们 —— 丢弃是对的。问题在 last_msg_ts 原来在解析【之前】
就刷新，于是噪声也算"活着"：
  - is_silent() 永远 False；
  - 真正该报的"连着但不推"被噪声盖住。
一分钟只要来一条噪声，静默检测就永久失效。
"""
from __future__ import annotations

import io
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src():
    return io.open(os.path.join(ROOT, "ai_trader", "market", "ws.py"),
                   encoding="utf-8").read()


def test_last_msg_ts_updates_only_after_validation():
    """last_msg_ts 必须在权重校验通过之后才刷新，不能收到报文就刷。"""
    src = _src()
    i_trade = src.index("async for msg in ws:")
    i_loop = src.index("elif msg.type in (aiohttp.WSMsgType.CLOSED", i_trade)
    body = src[i_trade:i_loop]
    # 循环体里只应有一次赋值，且在畸形判定之后
    assigns = [m.start() for m in re.finditer(r"self\.last_msg_ts = ", body)]
    assert len(assigns) == 1, "last_msg_ts 在循环里被赋值 %d 次（应为 1 次）" % len(assigns)
    i_assign = assigns[0]
    i_bad = body.index("self.dropped += 1")
    assert i_assign > i_bad, (
        "last_msg_ts 在畸形判定【之前】就刷新了 —— "
        "噪声会让静默检测永久失效")


def test_malformed_trades_still_dropped():
    """噪声仍必须被丢弃，不能因为改了时间戳就放进聚合器。

    p=0 进聚合器会造出 low=0 的 bar，进 last_price 会让决策看到价格 0。
    """
    src = _src()
    assert "self.dropped += 1" in src
    assert "_p > 0.0" in src
