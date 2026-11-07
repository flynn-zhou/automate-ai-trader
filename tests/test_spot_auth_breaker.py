# -*- coding: utf-8 -*-
"""凭证熔断的失败测试：401 之后必须停止发请求。

为什么这条重要：
  实测过——用无效 key 跑 live 现货，系统不崩溃，但每 3 秒打一次
  /api/v3/account 拿 401，一天 28,800 次。既浪费配额，又可能触发风控封 IP。

  所以这里直接数【实际发出的 HTTP 请求次数】。
  只断言"抛异常"是不够的——旧的实现也抛异常，但它每次都发请求。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.config import AppCfg
from ai_trader.exchange.spot import SpotError, SpotExecutor


class _Feed:
    last_price = 0.0


def _mk(counts):
    """造一个 executor，_transport 模拟币安返回 401 并计数。"""
    cfg = AppCfg()
    ex = SpotExecutor(_Feed(), cfg, api_key="A" * 64, api_secret="B" * 64)

    async def transport(method, path, params, signed):
        counts["n"] += 1
        raise SpotError('HTTP 401 %s: {"code":-2015,"msg":"Invalid API-key"}'
                        % path)

    ex._transport = transport
    return ex


@pytest.mark.asyncio
async def test_first_401_marks_auth_dead():
    counts = {"n": 0}
    ex = _mk(counts)
    with pytest.raises(SpotError):
        await ex.equity()
    assert ex._auth_dead, "401 之后没有标记凭证失效"
    assert counts["n"] == 1


@pytest.mark.asyncio
async def test_subsequent_calls_do_not_hit_network():
    """关键断言：第二次、第三次必须【不发请求】。"""
    counts = {"n": 0}
    ex = _mk(counts)
    with pytest.raises(SpotError):
        await ex.equity()
    first = counts["n"]

    for _ in range(20):
        with pytest.raises(SpotError):
            await ex.equity()

    assert counts["n"] == first, (
        "凭证已确认无效，却还在发请求：共 %d 次（第一次之后又发了 %d 次）。"
        "一天 28,800 次 401 会烧配额、可能封 IP。"
        % (counts["n"], counts["n"] - first))


@pytest.mark.asyncio
async def test_public_endpoints_still_work_after_auth_dead():
    """凭证坏了不该影响公开接口——行情还能看，便于运维判断。"""
    counts = {"n": 0}

    async def transport(method, path, params, signed):
        counts["n"] += 1
        if signed:
            raise SpotError('HTTP 401 %s: {"code":-2015,"msg":"Invalid API-key"}'
                            % path)
        return {"symbols": [
            {"symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT",
             "baseAssetPrecision": 8, "quoteAssetPrecision": 8,
             "filters": [
                 {"filterType": "LOT_SIZE", "stepSize": "0.00001000",
                  "minQty": "0.00001000"},
                 {"filterType": "NOTIONAL", "minNotional": "5.00000000"}]}]}

    cfg = AppCfg()
    ex = SpotExecutor(_Feed(), cfg, api_key="A" * 64, api_secret="B" * 64)
    ex._transport = transport

    with pytest.raises(SpotError):
        await ex.equity()
    assert ex._auth_dead

    sp = await ex.spec()
    assert sp["step"] == 0.00001, "凭证坏了连行情都拿不到了"
