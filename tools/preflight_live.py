# -*- coding: utf-8 -*-
"""切 live 之前的体检。只读，绝不下单。

为什么需要它：从「系统建好了」到「敢让它动真钱」之间，有一堆
只在真账户上才成立的条件——密钥权限、钱在哪个钱包、最小一手买不买得起、
库里有没有会被误接管的残留仓。任何一条不成立，开起来就是亏钱。

设计要点：
  - 顺序按「不依赖网络 -> 依赖网络」排。没密钥时也要能看到记账库那条
    （切 live 最危险的一项：纸面仓会被当成真实持仓接管）。
  - 仓位计算直接调引擎的 _trend_plan，不另写一套。
    另写的和真实执行一旦有偏差，体检就成了自我安慰。
  - 绝不打印密钥。

退出码：0 = 可以切 live；1 = 有硬阻断。
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.config import load

OK = "GO  "
BAD = "NO-GO"
WARN = "WARN"

results = []


def rec(level, name, detail, fix=""):
    results.append((level, name, detail, fix))
    print("[%s] %-30s %s" % (level, name, detail))
    if fix and level != OK:
        print("%11s ^ %s" % ("", fix))


def hr(title):
    print()
    print("---- %s " % title + "-" * max(0, 56 - len(title)))


def summary():
    hard = [r for r in results if r[0] == BAD]
    warn = [r for r in results if r[0] == WARN]
    print()
    print("=" * 64)
    print("硬阻断 %d 项   提醒 %d 项" % (len(hard), len(warn)))
    for _lv, name, detail, fix in hard:
        print("  X %s：%s" % (name, detail))
        if fix:
            print("    修法：%s" % fix)
    if not hard:
        print("结论：可以切 live（把 AI_TRADE_MODE=live 写进 /etc/ai-trader/env）")
    else:
        print("结论：先解决上面的硬阻断，不要切 live")
    print("=" * 64)
    return 0 if not hard else 1


def check_config(t, is_spot):
    hr("1. 配置")
    if t.mode != "live":
        rec(WARN, "AI_TRADE_MODE", "当前是 %s，不是 live" % t.mode,
            "注资后改成 AI_TRADE_MODE=live")
    else:
        rec(OK, "AI_TRADE_MODE", "live")
    # 只看 == "trend" 的话，唐奇安（同样通过五道关）会被标成黄色告警，
    # 而真正被否决的 scalp 反而不在这条判断里——判断写窄了就会误导人。
    _ok_strat = (t.strategy or "").lower() in ("trend", "donchian")
    rec(OK if _ok_strat else WARN, "AI_STRATEGY", t.strategy,
        "trend 与 donchian 都是通过五道关的策略；scalp 已被证伪（样本外归零）")
    rec(OK, "执行市场", "%s（%s）"
        % (t.exec_market, "无资金费、无爆仓" if is_spot else "含资金费"))
    fee = t.taker_fee_for(is_spot)
    rec(OK, "单边手续费", "%.2f%%（来回 %.2f%%）" % (fee * 100, fee * 200))
    if not is_spot and t.trend_min_notional > 0:
        rec(WARN, "永续最小名义额",
            "%.1f U（现货是 5.0 U）" % t.trend_min_notional,
            "100U 账户用现货更省手续费且不受最小一手限制")


def check_db(cfg):
    """记账库残留。不依赖网络，所以在凭证之前跑。"""
    hr("2. 记账库")
    from ai_trader.trade_db import TradeDB, equity_has_paper

    trade_path = cfg.store.path.replace(".db", "") + "_trade.db"
    if not os.path.exists(trade_path):
        rec(OK, "记账库", "不存在（首次运行会新建）")
        return
    tdb = TradeDB(trade_path)
    tdb.start()
    try:
        op = tdb.open_trade()          # 单数：最近一笔未平仓
        if op is None:
            rec(OK, "未平仓记录", "无")
        elif int(op.get("paper") or 0) == 1:
            rec(BAD, "未平仓记录",
                "有一笔【纸面】仓：%s qty=%.8f 开于 %.2f"
                % (op.get("side"), float(op.get("qty") or 0),
                   float(op.get("entry_price") or 0)),
                "切 live 前必须清掉纸面仓，否则会被当成真实持仓接管")
        else:
            rec(WARN, "未平仓记录",
                "%s qty=%.8f 开于 %.2f"
                % (op.get("side"), float(op.get("qty") or 0),
                   float(op.get("entry_price") or 0)),
                "切 live 时会对账，以交易所为准")

        has_paper = equity_has_paper(trade_path)
        rec(OK, "equity 表带 paper 列", "是" if has_paper else "否（旧库）",
            "旧库会把纸面峰值当实盘基准")
        p_live = tdb.peak_equity("trend", 0) if has_paper else 0
        p_paper = tdb.peak_equity("trend", 1) if has_paper else 0
        rec(OK if not p_live else WARN, "实盘历史峰值",
            "%.4f（纸面峰值 %.4f 已隔离）" % (p_live, p_paper),
            "实盘峰值非 0 会按它算回撤")
    finally:
        tdb.stop()


def check_creds(t):
    hr("3. 凭证")
    if t.api_key and t.api_secret:
        rec(OK, "AI_API_KEY / SECRET",
            "已配置（长度 %d/%d，值不打印）" % (len(t.api_key), len(t.api_secret)))
        return True
    rec(BAD, "AI_API_KEY / SECRET", "缺失",
        "在 /etc/ai-trader/env 写入密钥后重启服务")
    return False


async def check_exchange(cfg, t, is_spot):
    """返回 (ok, px, spec, equity)。"""
    hr("4. 交易所")
    from ai_trader.feed import Feed
    from ai_trader.store import Store
    # ★ 这里曾经【永远】构造 SpotExecutor，与 exec_market 无关。
    # 后果：切到永续之后跑体检，查的是现货钱包余额和现货交易对规则，
    # 结论看起来一本正经，但和真实下单的那条路径完全无关——
    # 典型的"验证手段本身在撒谎"。
    # 现在按 exec_market 选执行器，与 main.py 的选择口径一致。
    from ai_trader.exchange.spot import SpotExecutor
    from ai_trader.exchange.live import LiveExecutor

    store = Store(cfg.store.path)
    store.start()
    feed = Feed(cfg, store)
    feed.store = store
    if is_spot:
        ex = SpotExecutor(feed, cfg, t.api_key, t.api_secret,
                          proxy=cfg.market.http_proxy)
        rec(OK, "执行器类型", "SpotExecutor（现货：无资金费、无爆仓）")
    else:
        ex = LiveExecutor(feed, cfg, t.api_key, t.api_secret,
                          proxy=cfg.market.http_proxy)
        rec(OK, "执行器类型", "LiveExecutor（永续：含资金费、有爆仓价）")
    try:
        try:
            px = await ex.mark_price()
            rec(OK if px > 0 else BAD, "取到交易所牌价", "%.2f" % px,
                "检查网络/代理；服务器不能设代理")
            if px <= 0:
                return False, 0.0, None, None, feed, ex
        except Exception as e:
            rec(BAD, "取到交易所牌价", "失败：%s" % str(e)[:80],
                "检查 AI_HTTP_PROXY 与出网")
            return False, 0.0, None, None, feed, ex

        try:
            spec = await ex.spec()
            rec(OK, "交易对规则", "step=%s minQty=%s minNotional=%.2f"
                % (spec["step"], spec["min_qty"], spec["min_notional"]))
        except Exception as e:
            rec(BAD, "交易对规则", "失败：%s" % str(e)[:80])
            return False, px, None, None, feed, ex

        try:
            eq = await ex.equity()
            rec(OK, "账户查询（含签名）",
                "USDT=%.6f  qty=%.8f  权益=%.4f"
                % (eq["balance"], eq["position_qty"], eq["equity"]))
        except Exception as e:
            rec(BAD, "账户查询（含签名）", "失败：%s" % str(e)[:110],
                "密钥无效或没有现货权限；也可能是 IP 白名单没放通")
            return False, px, spec, None, feed, ex

        return True, px, spec, eq, feed, ex
    except Exception:
        return False, 0.0, None, None, feed, ex


def check_funds_and_plan(cfg, t, px, spec, eq, feed, ex):
    hr("5. 资金")
    equity = float(eq["equity"])
    usdt = float(eq["balance"])
    qty = float(eq["position_qty"])
    min_lot_notional = spec["min_qty"] * px

    if usdt > 0:
        rec(OK, "USDT 可用余额", "%.6f" % usdt)
    else:
        rec(BAD, "USDT 可用余额", "0",
            "钱在合约钱包里的话，要划转到现货钱包才能做现货")
    rec(OK if qty <= 0 else WARN, "当前持仓", "%.8f %s" % (qty, spec["base"]),
        "有持仓说明上一轮没平完或有人手工买过，切 live 时会对账接管")
    rec(OK, "最小一手名义额", "%.2f U（minQty %s × 价 %.2f）"
        % (min_lot_notional, spec["min_qty"], px))
    # ★ 真正的门槛是【两者取大】，不是只看 minNotional。
    # 永续 BTCUSDT：minNotional=50，但 minQty=0.001 手 = 78.1U。
    # 只比 minNotional 会得出"100U 很宽裕"的结论，实际只剩 28% 余量，
    # 而且一手就吃掉 78% 权益——这正是实盘第一次亏 3 笔就开不出仓的根源。
    need = max(min_lot_notional, spec["min_notional"])
    headroom = (equity - need) / equity * 100.0 if equity else 0.0
    detail = ("一手占权益 %.1f%%，余量 %.1f%%（再亏 %.1f%% 就开不出仓）"
              % (need / equity * 100.0 if equity else 0.0, headroom,
                 max(0.0, headroom)))
    if equity > need:
        rec(OK if headroom >= 30 else WARN, "够不够开一手",
            "权益 %.2f U > 门槛 %.2f U｜%s" % (equity, need, detail),
            "" if headroom >= 30 else "余量太薄：连亏几笔就会永久开不出仓，建议换粒度更细的品种或加资金")
    else:
        rec(BAD, "够不够开一手", "权益 %.2f U <= 门槛 %.2f U" % (equity, need),
            "开不出仓。注意门槛取 max(最小一手价, 最小名义额)，不只是 minNotional")

    # 真实计划：调引擎自己的 sizing，不另写一套
    from ai_trader.trading.engine import DecisionEngine
    try:
        eng = DecisionEngine(cfg, feed, ex, None, guard=None)
        plan = eng._trend_plan(px, equity, {"name": "trend_preflight"})
        if plan.ok:
            rec(OK, "按当前权益的实际计划",
                "qty=%.8f 名义=%.2fU 用掉 %.1f%% 止损 %.2f%%"
                % (plan.qty, plan.notional,
                   (plan.notional / equity * 100) if equity else 0, plan.stop_pct))
        else:
            rec(BAD, "按当前权益的实际计划", plan.reason,
                "这笔钱按引擎算法开不出仓位，注资后才会通过")
    except Exception as e:
        rec(WARN, "按当前权益的实际计划", "复核失败：%s" % str(e)[:70])

    # 回撤余量：账户小到一定程度就【永久】开不出仓。这不是亏损，是策略结构性失效。
    # 为什么要在体检里报：它不会报错、不会告警，只是永远不成交 ——
    # 那种「系统活着但死了」的状态最难发现。
    try:
        floor = None
        lo, hi = 0.0, max(equity, 1.0) * 20
        if eng._trend_plan(px, hi, {"name": "x"}).ok:
            for _ in range(50):
                mid = (lo + hi) / 2.0
                if eng._trend_plan(px, mid, {"name": "x"}).ok:
                    hi = mid
                else:
                    lo = mid
            floor = hi
        if equity <= 0:
            # 权益为 0 时这一项不能「消失」：
            # 一行不显示的检查等于没有这条检查，运维会以为它通过了。
            rec(BAD, "回撤余量", "权益为 0，谈不上余量",
                "先注资；现货生死线约 5~7U，永续约 78U")
        elif floor:
            room = (1 - floor / equity) * 100.0
            # 用【当前策略】的实测最大回撤，不是一个写死的数字。
            # 唐奇安 1h（7.15 年、100U 真实账户）实测 32.4%；
            # 4h SMA200 是 46.1%。拿错那个会让门槛偏松或偏紧。
            need_dd = 32.4 if (t.strategy or "").lower() == "donchian" else 46.1
            if room >= need_dd:
                rec(OK, "回撤余量",
                    "权益 %.2fU，跌到 %.2fU 才永久停摆（余量 %.0f%% ≥ 实测最大回撤 %.1f%%）"
                    % (equity, floor, room, need_dd))
            else:
                # 这里【故意保持 WARN，不升级成硬阻断】。
                # 曾经想升成 BAD，但反证后发现不成立：
                #   100U + BTC 一手 78U 的回测（7.15 年真实账户口径）
                #   最终 168U、最大回撤 24.4%，它【没有】永久停摆——
                #   因为权益总在下一笔进场前回到门槛之上。
                # 真正"连一手都开不出"的处境，上面「按当前权益的实际计划」
                # 已经报了 BAD，不需要在这里重复拦截。
                # 这里的语义是"能交易，但一轮正常回撤就可能卡住"，那是提醒。
                rec(WARN, "回撤余量",
                    "权益 %.2fU，跌到 %.2fU 就永久开不出仓（余量仅 %.0f%%，"
                    "而实测最大回撤是 %.1f%%）" % (equity, floor, room, need_dd),
                    "余量 < 实测最大回撤 = 一轮正常回撤就可能永久停摆。"
                    "想留足余量：加资金到 %.0fU 以上，或换粒度更细的品种"
                    "（ETH/SOL/PAXG 一手只占 1~4%%），不要靠调杠杆绕过"
                    % (floor / (1 - need_dd / 100.0)))
    except Exception as e:
        rec(WARN, "回撤余量", "算不出：%s" % str(e)[:70])


async def check_misc(t, ex):
    hr("6. 时钟")
    try:
        srv = await ex._req("GET", "/api/v3/time", signed=False)
        skew = int(srv.get("serverTime", 0)) - int(time.time() * 1000)
        rec(OK if abs(skew) < 2000 else BAD, "与交易所时钟偏差", "%+d ms" % skew,
            "签名请求会因时间偏移被拒，校时后重启")
    except Exception as e:
        rec(WARN, "与交易所时钟偏差", "取不到：%s" % str(e)[:60])

    hr("7. 当下策略状态")
    # 这一节原本写死 TrendStrategy + "SMA%d"，切到唐奇安之后它会拿
    # 一个 4h SMA200 的结论去回答"现在能不能交易"，而线上跑的是 1h 通道。
    # 体检报告和真实系统对着两套规则，是最坏的一种"检查通过"。
    try:
        if (t.strategy or "").lower() == "donchian":
            from ai_trader.trading.donchian import DonchianStrategy
            st = DonchianStrategy(t, symbol=t.symbol, exec_market=t.exec_market)
            warm = int(t.donchian_entry) + 2
        else:
            from ai_trader.trading.trend import TrendStrategy
            st = TrendStrategy(t, symbol=t.symbol, exec_market=t.exec_market)
            warm = int(t.sma_lookback) + 1
        st.seed_from_rest()
        s = st.state(ex_px[0])
        per = st.interval_name()
        if s["ready"]:
            rec(OK, "%s K 线" % per, "%d 根（需要 %d）" % (s["bars"], warm))
            if (t.strategy or "").lower() == "donchian":
                rec(OK, "%d 根新高上轨 / %d 根新低下轨" % (s["entry_n"], s["exit_m"]),
                    "%.2f / %.2f" % (s["upper"], s["lower"]))
                dist = (ex_px[0] - s["upper"]) / s["upper"] * 100.0
                rec(OK if s["above_upper"] else WARN, "现价相对上轨",
                    "%.2f（%+.2f%%）%s"
                    % (ex_px[0], dist,
                       "已突破，等本根收盘确认" if s["above_upper"] else "在上轨下方"),
                    "" if s["above_upper"] else "未突破上轨不做多，等收盘站上")
            else:
                rec(OK, "SMA%d" % t.sma_lookback, "%.2f" % s["sma"])
                dist = (ex_px[0] - s["sma"]) / s["sma"] * 100.0
                rec(OK if s["above"] else WARN, "现价相对均线",
                    "%.2f（%+.2f%%）%s"
                    % (ex_px[0], dist, "在均线上方" if s["above"] else "在均线下方"),
                    "" if s["above"] else "均线下方不做多，等上穿")
        else:
            rec(WARN, "%s K 线" % per, "只拿到 %d 根，不足 %d" % (s["bars"], warm),
                "历史不足时不会交易，需等补数")
    except Exception as e:
        rec(WARN, "策略状态", "取不到：%s" % str(e)[:70])


ex_px = [0.0]


async def main():
    cfg = load()
    t = cfg.trading
    is_spot = t.exec_market == "spot"
    print("AI 交易实盘体检  %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    print("模式=%s  市场=%s  策略=%s  标的=%s"
          % (t.mode, t.exec_market, t.strategy, t.symbol))

    check_config(t, is_spot)
    check_db(cfg)                       # 不依赖网络，先跑
    if not check_creds(t):
        return summary()

    ok, px, spec, eq, feed, ex = await check_exchange(cfg, t, is_spot)
    try:
        if ok:
            ex_px[0] = px
            check_funds_and_plan(cfg, t, px, spec, eq, feed, ex)
            await check_misc(t, ex)
    finally:
        try:
            await ex.close()
        except Exception:
            pass
    return summary()


if __name__ == "__main__":
    sys.exit(asyncio.get_event_loop().run_until_complete(main()))
