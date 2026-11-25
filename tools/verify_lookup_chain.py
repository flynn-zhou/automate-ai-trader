# -*- coding: utf-8 -*-
"""验证：`closed_order_by_client_id` 在**真实近期数据**上确实能查到单。

不依赖我方订单存在：先从交易所 allOrders 里挑一个近期**真的成交过**的
clientOrderId，再走引擎那条查询链，看能不能查回来。
查得到 = B1 的查找链路在生产可用；查不到 = B1 是空转，要修。
"""
import asyncio
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, "/home/cryptobot/ai-trader")

from ai_trader.config import load                       # noqa: E402
from ai_trader.exchange.live import LiveExecutor        # noqa: E402


def env(name):
    v = os.environ.get(name)
    if v:
        return v
    try:
        for line in open("/etc/ai-trader/env", encoding="utf-8"):
            line = line.strip()
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


async def main():
    key, sec = env("AI_API_KEY"), env("AI_API_SECRET")
    cfg = load()
    cfg.trading.symbol = "BTCUSDT"
    ex = LiveExecutor(SimpleNamespace(last_price=0.0), cfg, key, sec)
    try:
        rows = await ex._req("GET", "/fapi/v1/allOrders",
                             {"symbol": "BTCUSDT", "limit": 60})
        print("allOrders 返回 %d 条" % len(rows))
        filled = [r for r in rows
                  if str(r.get("status")) == "FILLED"
                  and str(r.get("clientOrderId") or "")]
        print("其中带 clientOrderId 的已成交单：%d 条" % len(filled))
        if not filled:
            print("!! 没有可用的样本")
            return 1
        for r in filled[:3]:
            cid = r["clientOrderId"]
            print("\n=== 样本 %s（%s 成交 %.3f 均价 %s）"
                  % (cid, r.get("type"), float(r.get("executedQty") or 0),
                     r.get("avgPrice")))
            o = await ex.closed_order_by_client_id(cid)
            if o is None:
                print("  !! 查不到 —— B1 的查找链在真实数据上不工作")
                return 1
            print("  查到：status=%s 成交 %.6f 均价 %.4f"
                  % (o.status, o.executed_qty, o.avg_price))
        # 再验一次"真的不存在"的 id 必须返回 None（不能乱给一张单）
        print("\n=== 不存在的 id 必须返回 None")
        o = await ex.closed_order_by_client_id("AI-SLP-does-not-exist-xyz")
        print("  返回 %r" % (o,))
        assert o is None, "不存在的 id 居然查到了东西 —— 查找链在乱匹配"
        print("\n全部通过：B1 的查找链在真实数据上可用。")
    finally:
        await ex.close()
    return 0


sys.exit(asyncio.get_event_loop().run_until_complete(main()))
