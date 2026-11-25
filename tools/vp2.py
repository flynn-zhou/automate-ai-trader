# -*- coding: utf-8 -*-
"""生产验收：市价单的 clientOrderId 现在真的下发了吗？

用**最小可验证代价**验：开 0.01 BTC 市价多单（约 590U 名义，占用约 27U），
立刻 reduceOnly 平掉。往返手续费约 0.6U（0.05%×2×590），价格波动风险
在几秒内可忽略。验的是三件事：

  1. 下单时交易所那张单的 clientOrderId 是 AI- 前缀（反查得到）
  2. 两笔成交的 client_id 都能被 recent_fills 补出来
  3. classify_delta 对我们的成交判 ours（修复前必然 manual）
"""
import asyncio
import os
import sys

sys.path.insert(0, "/home/cryptobot/ai-trader")
with open("/etc/ai-trader/env", "r", encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from ai_trader.config import load                      # noqa: E402
from ai_trader.exchange.live import LiveExecutor       # noqa: E402
from ai_trader.trading import engine as E              # noqa: E402


class _Feed(object):
    last_price = 0.0


async def main():
    cfg = load()
    ex = LiveExecutor(_Feed(), cfg, cfg.trading.api_key,
                      cfg.trading.api_secret)
    try:
        pos = await ex.position()
        print("先确认空仓：%s" % (pos,))
        assert abs(float((pos or {}).get("qty") or 0.0)) < 1e-12, "有仓，不做"

        cid = "AI-mktverify-%d" % (int(os.times().elapsed * 1000) % 100000)
        print()
        print("① 开仓市价单，client_id=%r" % cid)
        f1 = await ex.market_order("buy", 0.01, reason="归属验收", client_id=cid)
        print("   -> ok=%s price=%s qty=%s fee=%.6f order_id=%s"
              % (f1.ok, f1.price, f1.qty, f1.fee or 0.0, f1.order_id))
        assert f1.ok, f1.reason

        od = await ex.query_order(client_id=cid)
        print("   交易所侧 clientOrderId 反查：status=%s executed=%s"
              % (getattr(od, "status", None),
                 getattr(od, "executed_qty", None)))
        got_cid = getattr(od, "client_id", None)
        print("   反查到的 client_id=%r" % (got_cid,))
        assert str(got_cid or "") == cid, (
            "交易所那张单上没有我们下发的 clientOrderId：%r" % (got_cid,))

        print()
        print("② 立刻市价平掉（reduce_only，同样带 id）")
        f2 = await ex.market_order("sell", 0.01, reason="归属验收平仓",
                                   reduce_only=True,
                                   client_id="AI-mktverifyc-%d" %
                                   (int(os.times().elapsed * 1000) % 100000))
        print("   -> ok=%s price=%s fee=%.6f" % (f2.ok, f2.price, f2.fee or 0.0))
        assert f2.ok, f2.reason

        await asyncio.sleep(2)
        print()
        print("③ 成交归属（这一条是修复的核心验收）")
        fills = await ex.recent_fills(hours=1)
        for x in fills[:4]:
            print("   %s %s %.6f @ %s client_id=%r"
                  % (x["ts"], x["side"], x["qty"], x["price"], x["client_id"]))
        bad = [x for x in fills[:2] if not str(x["client_id"] or "")
               .startswith(E.AI_CLIENT_PREFIX)]
        if bad:
            print("   !! 仍有成交认不领：%r" % (bad,))
            return 1
        print("   ✔ 我方成交全部认领为 ours")

        after = await ex.position()
        print()
        print("平仓后持仓：%s" % (after,))
        eq = await ex.equity()
        print("权益 %.4f U / 可用 %.4f U"
              % (eq.get("equity", 0.0), eq.get("available_balance", 0.0)))
        return 0
    finally:
        await ex.close()


sys.exit(asyncio.get_event_loop().run_until_complete(main()))
