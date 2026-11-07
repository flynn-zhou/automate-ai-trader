"""入口：一个 asyncio 事件循环跑行情采集 + 看板 WebSocket，Flask 在旁路线程提供历史 API。

用法：
    python -m ai_trader.main                # 正常常驻
    python -m ai_trader.main --seconds 30   # 自检：跑 30 秒打印统计后退出
    python -m ai_trader.main --no-web       # 只采集
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import threading
import time

from . import config as cfgmod
from .exchange.live import LiveExecutor
from .exchange.paper import PaperExecutor
from .exchange.spot import SpotExecutor
from .guard import Guard, feishu_notifier
from .journal import Journal
from .feed import Feed
from .market.multikline import MultiKlineCollector
from .market.subscription import SubscriptionStore
from .range import live as range_live
from .range.store import RangeStore
from .settings import SettingsStore
from .store import Store
from .trade_db import TradeDB
from .trading.engine import DecisionEngine
from .web.app import create_app
from .ws_server import BoardServer

logger = logging.getLogger("ai_trader")


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _start_flask(cfg, feed, store, engine, tdb, journal, guard,
                 settings=None, range_db=None) -> None:
    import waitress

    feed.store = store
    app = create_app(cfg, feed, engine=engine, tdb=tdb, journal=journal, guard=guard,
                     range_db=range_db, settings=settings, store=store)
    logger.info("历史 API 已监听 http://%s:%d", cfg.web.host, cfg.web.port)
    waitress.serve(app, host=cfg.web.host, port=cfg.web.port, threads=8, _quiet=True)


async def run(cfg, seconds: int = 0, with_web: bool = True) -> None:
    # 实盘凭证必须在任何 I/O 之前校验：带着空凭证去连行情，
    # 最后虽然也会退出，但会先跑一段"看起来正常"的流程，掩盖真实原因。
    if cfg.trading.mode == "live" and (not cfg.trading.api_key
                                       or not cfg.trading.api_secret):
        raise SystemExit("AI_TRADE_MODE=live 但缺少 AI_API_KEY / AI_API_SECRET")
    store = Store(cfg.store.path)
    store.start()
    feed = Feed(cfg, store)
    feed.store = store

    trade_path = cfg.store.path.replace(".db", "") + "_trade.db"
    tdb = TradeDB(trade_path)
    tdb.start()
    journal = Journal(trade_path)
    journal.start()

    # 运行时可调的仓位参数（Web 上改即时生效）。
    # 必须在 engine 构造【之前】apply：引擎构造时保存的是 cfg.trading 引用，
    # 先 apply 保证启动那一刻就带上了上次在页面上设定的值。
    base = cfg.store.path.replace(".db", "")
    settings = SettingsStore(base + "_settings.db")
    try:
        # 数据库是唯一权威：库为空时先从 env/yaml 播种，此后只认库。
        # 漏掉播种这一步，env 里的值会被硬编码默认值静默覆盖。
        seeded = settings.seed_from(cfg.trading)
        applied = settings.apply_to(cfg.trading)
        logger.info("仓位参数（%s）: 本金 %.2fU 风险 %.2f%% 杠杆 %.1fx",
                    "已从 env 播种，此后以数据库为准" if seeded else "来自数据库/Web 配置",
                    applied["sizing.capital"], applied["sizing.risk_pct"],
                    applied["sizing.leverage"])
    except Exception as e:
        # 参数不合法就拒绝启动，而不是带着一个算不出爆仓距离的组合去下单
        raise SystemExit("仓位参数非法，拒绝启动: %s" % e)

    range_db = RangeStore(base + "_range.db")

    # 行情订阅清单：决定收谁的行情（不等于交易谁的品种）。
    # 首次启动把 config 里的品种播种进去，之后以库为权威、由页面增删。
    subs = SubscriptionStore(base + "_settings.db")
    subs.seed([cfg.market.symbol])
    logger.info("行情订阅: %s（交易品种固定为 %s）",
                ",".join(subs.symbols()) or "(空)", cfg.trading.symbol)

    await feed.start()

    # 多品种 K 线采集：订阅清单里除交易品种外的，慢速拉 1h K 线入库。
    # 交易品种仍由 Feed 全速负责，两者互不影响。
    collector = MultiKlineCollector(cfg, store, subs, interval="1h", period_s=300.0)
    collector_task = None
    extra = collector.symbols()
    if extra:
        logger.info("多品种采集启动: %s", ",".join(extra))
        # 必须留引用：不留引用就没法 cancel，而 run_forever 里是 sleep(300)——
        # 关闭时这个 task 会被 asyncio.run 隐式取消，它持有的 aiohttp session
        # 就没人关了，于是进程退出时吐 "Unclosed client session"。
        collector_task = asyncio.create_task(collector.run_forever(),
                                             name="multikline")
    else:
        logger.info("多品种采集: 无额外订阅品种，不启动")

    # 区间演化实时推进：每 60 秒把最新 1h 桶重算一遍并入库。
    # 没有它，区间演化页永远停在最后一次回填的时刻（用户看到的就是"只有昨天 23 点"）。
    range_task = None
    try:
        interval_s = float(os.environ.get("AI_RANGE_TICK_S", "60"))
    except ValueError:
        interval_s = 60.0
    if interval_s > 0:
        range_task = asyncio.create_task(
            range_live.run_forever(range_db, store, cfg.trading.symbol,
                                   interval_s),
            name="range-live")
        logger.info("区间演化实时推进: 每 %.0f 秒", interval_s)
    else:
        logger.warning("区间演化实时推进已关闭（AI_RANGE_TICK_S=%s）", interval_s)

    if cfg.trading.mode == "live":
        # 实盘：缺凭证就直接拒绝启动，绝不"降级成纸面"偷偷跑起来——
        # 那样最危险：你以为在下真单，其实在记账。
        if cfg.trading.exec_market == "spot":
            executor = SpotExecutor(feed, cfg, cfg.trading.api_key,
                                    cfg.trading.api_secret,
                                    proxy=cfg.market.http_proxy)
            logger.warning("=== 实盘现货：真实资金，无资金费、无爆仓，verify-first 已启用 ===")
        else:
            executor = LiveExecutor(feed, cfg, cfg.trading.api_key,
                                    cfg.trading.api_secret,
                                    proxy=cfg.market.http_proxy)
            logger.warning("=== 实盘永续：真实资金，含资金费成本，verify-first 已启用 ===")
    else:
        executor = PaperExecutor(feed, cfg)
        if hasattr(executor, "restore_from_db"):
            executor.restore_from_db(tdb)
    hook = feishu_notifier(os.environ.get("AI_FEISHU_WEBHOOK", ""))
    guard = Guard(cfg, tdb=tdb, journal=journal, notify=hook)
    # settings 必须传进去：4 个仓位参数（本金/风险%/杠杆/保证金上限）
    # 的唯一来源是 Web 配置库，闸门每笔开仓前都要现读一次。
    # 不传的话 _sizing_params() 返回 None，闸门会一律拒绝开仓 —— 宁可不开，
    # 也不按硬编码默认值在真钱上算仓位。
    engine = DecisionEngine(cfg, feed, executor, tdb, guard=guard,
                            settings=settings)
    engine.attach()
    await engine.reconcile_live()

    # 把杠杆真正推到交易所去。
    # LiveExecutor.set_leverage() 早就写好了（含幂等、日志、状态记录），
    # 但全项目从未调用过——典型的"写好了但没接线"。
    # 后果：交易所那边用的是默认杠杆或上次残留值。
    # 1x 趋势策略下名义额 = 权益，只要交易所杠杆 >= 1x 就不会出事，
    # 所以这个 bug 至今没暴露；但一旦把 trend_leverage 调到 2x/3x，
    # 实际杠杆就和配置不一致了——要么开仓被拒，要么风险敞口超出预期。
    if cfg.trading.mode == "live" and hasattr(executor, "set_leverage"):
        try:
            await executor.set_leverage(engine._plan_leverage())
        except Exception as e:
            # 设不上就大声记一笔，但不要因此拒绝启动：
            # 杠杆没设成不等于不能交易（1x 时尤其如此）。
            logger.error("设置杠杆失败（将使用交易所现有杠杆）：%s", e)
            tdb.log_risk("leverage_set_failed", str(e)[:200])
    # 这一行日志曾经是错的，而且错得很危险：它打印的是
    # _last_equity or paper_balance —— 第一次循环还没跑，_last_equity 是 None，
    # 于是永远打印 AI_PAPER_BALANCE（100），而纸面执行器其实从库里恢复了 71.77U。
    # 运维看到"权益=100.00U"会以为钱是够的，而引擎随后每一轮都在"资金不足"里空转。
    # 日志把"不可操作"伪装成了"正常启动"。
    #
    # 改成读执行器真实余额：纸面读 self.balance（已从库恢复过），
    # 实盘这一轮还查不到，就如实写"待查询"，不拿配置值顶替。
    _bal = getattr(executor, "balance", None)
    if _bal is None:
        _bal_txt = "待查询"
    else:
        _bal_txt = "%.2fU" % float(_bal)
    _truth = ("（引擎可用 %.2fU）" % (engine._last_equity or 0.0)
              if engine._last_equity else "")
    logger.info("决策引擎已挂载：mode=%s 策略=%s 执行器余额=%s%s 品种=%s",
                executor.name, cfg.trading.strategy, _bal_txt, _truth,
                getattr(cfg.trading, "exec_market", "-"))

    board = None
    if with_web:
        board = BoardServer(feed, cfg.web.host, cfg.web.port + 1, cfg.web.auth_token)
        await board.start()
        threading.Thread(target=_start_flask,
                         args=(cfg, feed, store, engine, tdb, journal, guard,
                               settings, range_db),
                         daemon=True, name="flask").start()

    stop = asyncio.Event()

    def _sig(*_a):
        stop.set()

    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            asyncio.get_event_loop().add_signal_handler(s, _sig)
        except (NotImplementedError, RuntimeError):
            try:
                signal.signal(s, lambda *_: stop.set())
            except Exception:
                pass

    t0 = time.time()
    last_report = 0.0
    while not stop.is_set():
        if seconds and time.time() - t0 >= seconds:
            break
        try:
            await asyncio.wait_for(stop.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            pass
        if seconds and time.time() - t0 - last_report >= 5:
            last_report = time.time() - t0
            snap = feed.snapshot()
            b3 = snap["bars"].get(3000) or {}
            logger.info("[自检 %4.1fs] src=%s price=%s 3s(o=%s h=%s l=%s c=%s n=%s) ticks=%d rest=%d",
                        last_report, snap["src"], snap["price"],
                        b3.get("o"), b3.get("h"), b3.get("l"), b3.get("c"), b3.get("n"),
                        snap["ticks_total"], snap["rest_ticks"])

    logger.info("正在关闭…")

    # 诊断用：关闭超过 4 秒，就把**所有线程的真实调用栈**打到文件里。
    # 没有这个，我只能说"关闭慢"，说不出"卡在哪一行"——上一版就是这样
    # 撞满 20 秒超时却不知道为什么。repeat=True 每 4 秒重打一次，
    # 所以卡住的过程会被连续记录下来，能看到它到底停在哪个 await 上。
    _stacks_fp = None
    try:
        import faulthandler
        _stacks_path = os.path.join(
            os.path.dirname(os.path.abspath(cfg.store.path)),
            "shutdown.stacks.txt")
        _stacks_fp = open(_stacks_path, "w")
        _stacks_fp.write("关闭诊断：进程 %d，开始于 %s\n" %
                         (os.getpid(), time.strftime("%Y-%m-%d %H:%M:%S")))
        _stacks_fp.flush()
        faulthandler.dump_traceback_later(4.0, repeat=True, file=_stacks_fp)
    except Exception:
        _stacks_fp = None

    async def _await_task(t):
        """等一个已 cancel 的 task 真正结束（吞掉 CancelledError）。"""
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass

    async def _shutdown() -> None:
        """按顺序释放资源，顺序有讲究：

        1. 先 cancel 采集 task —— 它每 5 分钟醒一次，不先停就会在下面
           关 session 的时候又去建一个新的。
        2. 再关 collector 的 session，然后是 board 与 feed。
        3. 最后落盘并停 DB。

        每一步都单独 try：某一步抛异常不能把后面的资源释放全跳过去，
        那正是"关了一半"的来由——进程退出了，但库没 flush、session 没关。
        """
        # 每一步都单独限时 + 计时。没有计时的关闭日志只能说"慢"，
        # 说不出"哪一步慢"——上一版就是这样把 20s 超时撞满却不知道为什么。
        async def _step(name, coro, limit):
            t = time.time()
            try:
                await asyncio.wait_for(coro, timeout=limit)
                logger.info("  关闭[%s] 完成 %.2fs", name, time.time() - t)
            except asyncio.TimeoutError:
                logger.warning("  关闭[%s] 超时 %.1fs（已跳过）", name, time.time() - t)
            except Exception as e:
                logger.warning("  关闭[%s] 失败 %.2fs: %s",
                               name, time.time() - t, str(e)[:100])

        if range_task is not None:
            range_task.cancel()
            await _step("range-task", _await_task(range_task), 4.0)
        if collector_task is not None:
            collector_task.cancel()
            await _step("collector-task", _await_task(collector_task), 4.0)
        await _step("collector-session", collector.close(), 4.0)
        if board:
            await _step("board", board.stop(), 5.0)
        await _step("feed", feed.stop(), 12.0)
        for iv in cfgmod.INTERVALS_MS:
            b = feed.agg.current(iv)
            if b:
                store.put_bar(b)
        await asyncio.sleep(0.6)
        for fn, name in ((tdb.stop, "trade_db"), (journal.stop, "journal"),
                         (store.stop, "store")):
            try:
                fn()
            except Exception as e:
                logger.warning("关闭 %s 失败: %s", name, str(e)[:120])

    try:
        # 关闭路径不能可以无限卡。以前没有这层保护，任何一步卡住就要等
        # systemd 的 TimeoutStopSec（默认 90 秒）才 SIGKILL——表现就是
        # "服务停不下来，重启要等一分半"。这里给它 20 秒，超时就继续往下走，
        # 让进程自己退出；剩下的交给 systemd 兜底。
        await asyncio.wait_for(_shutdown(), timeout=20.0)
    except asyncio.TimeoutError:
        logger.error("关闭超时 20s：有资源没释放干净，强制退出"
                     "（下次启动会重新建连接，但请查上面哪一步没返回）")
    except Exception:
        logger.error("关闭过程异常", exc_info=True)
    if _stacks_fp is not None:
        try:
            import faulthandler
            faulthandler.cancel_dump_traceback_later()
            _stacks_fp.write("关闭在 4s 内完成，没有卡住。\n")
            _stacks_fp.close()
        except Exception:
            pass
    logger.info("已停止。本轮写入 K 桶 %d 条，当前数据源=%s", store.written, feed.src)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="")
    ap.add_argument("--seconds", type=int, default=0)
    ap.add_argument("--no-web", action="store_true")
    args = ap.parse_args()
    cfg = cfgmod.load(args.config)
    _setup_logging(cfg.log_level)
    logger.info("automate-ai-trader 启动 env=%s symbol=%s db=%s",
                cfg.env, cfg.market.symbol, cfg.store.path)
    try:
        asyncio.run(run(cfg, seconds=args.seconds, with_web=not args.no_web))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()