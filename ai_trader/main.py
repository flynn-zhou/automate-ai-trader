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
from typing import List

from . import config as cfgmod
from .exchange.live import LiveExecutor
from .exchange.paper import PaperExecutor
from .guard import Guard, feishu_notifier
from .journal import Journal
from .feed import Feed
from .market.multikline import MultiKlineCollector
from .market.subscription import SubscriptionStore
from .range import live as range_live
from .range.store import RangeStore
from .settings import SettingsStore
from .trading.pending_thesis_store import PendingThesisStore
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
                 settings=None, range_db=None, loop=None) -> None:
    import waitress

    feed.store = store
    app = create_app(cfg, feed, engine=engine, tdb=tdb, journal=journal, guard=guard,
                     range_db=range_db, settings=settings, store=store, loop=loop)
    logger.info("历史 API 已监听 http://%s:%d", cfg.web.host, cfg.web.port)
    waitress.serve(app, host=cfg.web.host, port=cfg.web.port, threads=8, _quiet=True)


def _kbar_db_path(cfg):
    """kbar 库路径由**交易库所在目录**推导（与 `kbar_lines_for` 同一口径）。"""
    return os.path.join(os.path.dirname(os.path.abspath(cfg.store.path)),
                        "kbar.db")


def kbar_view_for(cfg):
    """S5 出场闸要的 K 线演化视图；库不存在时返回 None。

    ★ 返回 None 而不是空视图：空视图会让三道闸「永不触发」，
      而看板上显示的是一笔一笔正常持仓 —— 没人会去怀疑闸没数据。
      引擎收到 None 时会把这件事记进 risk_events。
    """
    from .trading.kbar_view import RefreshableKBarView

    path = _kbar_db_path(cfg)
    if not os.path.exists(path):
        if str(getattr(cfg.trading, "strategy", "")).lower() == "pending":
            logger.warning("kbar 库不存在（%s）：S5 三道闸均不工作", path)
        return None
    try:
        # ★ 必须是**可刷新**的代理，不能是启动时的一次性快照。
        #   `KBarView` 是内存里的行字典，不会自己长 ——
        #   静态快照 = 跑第三天用的还是启动那天的 K 线事实。
        #   这与 `pending_lines` 当初踩的是同一个坑（见上面那个函数
        #   的 docstring），我在 S5 这条路上原样重犯了一遍。
        v = RefreshableKBarView(path)
    except Exception as e:
        logger.error("S5 K 线视图装载失败（%s）：%s", path, e)
        return None
    c = v.coverage()
    logger.warning("S5 K 线视图：%d 根（带方向 %d、带量比 %d）",
                   c["bars"], c["with_side"], c["with_vol_rel"])
    return v


def pending_source_for(cfg, book_provider):
    """计划源；库不存在时返回 None。

    没它引擎就永远不会有新计划（它只推进已存在的预期）——
    而「没信号」和「没源头」在事后完全一样。
    """
    from .trading.plan_source import PlanSource, resolve_range_db

    kbar_db = _kbar_db_path(cfg)
    base = os.path.dirname(os.path.abspath(cfg.store.path))
    # ★ 区间库在生产上叫 `market_range.db`。
    #   第一版我写死了本地分析用的 `market_range_rebuilt.db`，
    #   结果就是：本地跑得通，服务器上计划源直接返回 None，
    #   永远不产计划 —— 而日志里只有一行“区间库不存在”。
    range_db = resolve_range_db(base)
    if not range_db:
        logger.error("计划源：在 %s 下找不到区间库"
                     "（试过 market_range.db / market_range_rebuilt.db）", base)
        return None
    logger.warning("计划源：区间库 = %s", range_db)
    if not os.path.exists(kbar_db):
        logger.warning("计划源：kbar 库不存在（%s），不产计划", kbar_db)
        return None

    symbol = str(getattr(cfg.trading, "symbol", "") or "BTCUSDT")
    src = PlanSource(book_provider=book_provider, kbar_db=kbar_db,
                     range_db=range_db, symbol=symbol)

    def _produce(params):
        return src.plans(params)

    return _produce


def kbar_lines_for(cfg):
    """返回一个「取当前线值」的可调用对象；kbar 库不存在时返回 None。

    ★ 为什么返回**可调用对象**而不是 LineBook 本身：
      LineBook 是「截至某收盘时刻已就绪」的静态快照，而引擎每小时都会
      多出一批线值。启动时读一次，跑到第三天用的还是第一天的线 ——
      `ema20` 失效判定会拿一根三天前的均线去比，而且不会报错。

    ★ 为什么库不存在时返回 None 而不是空 LineBook：
      空 LineBook 会让 trend 家族的失效判定**恒不成立**（永远拿不到 ema）。
      那等于这条保护被静默关掉，而看板上一切正常。返回 None 时引擎会
      用空 LineBook 并把这件事记进 risk_events（见 engine._step_pending）。
    """
    import sqlite3
    from .trading import thesis as T

    # kbar 库的位置由**交易库所在目录**推导，不另开环境变量。
    # 理由：多一个 env 就多一个「代码读了但模板没写」的缺口
    # （tests/test_deploy_env_completeness.py 正是钉这个的），
    # 而 data/kbar.db 本来就是固定布局（见 kbar/build.py 的 DEFAULT_KBAR_DB）。
    path = os.path.join(os.path.dirname(os.path.abspath(cfg.store.path)), "kbar.db")
    if not os.path.exists(path):
        # ★ 只在真要用它的策略下告警。跑 trend/donchian 的机器上根本没有
        #   kbar.db，无条件 warning 就是每周报一次的假警报 ——
        #   喊狼来了的次数多了，真的那条也就没人看了。
        if str(getattr(cfg.trading, "strategy", "")).lower() == "pending":
            logger.warning("kbar 库不存在（%s）：挂单策略的均线失效判定无法工作", path)
        return None

    # ★ 装载名单必须从 `kbar.lines::RANGE_LINE_KINDS` **推导**，不能另写一份。
    #
    # 这里原来是一张硬编码的 4 键名单：
    #     (SLOW_LINE_KEY, FAST_LINE_KEY, "range_soft_up@1h", "range_soft_dn@1h")
    # 而 `kbar_line` 里其实还有 range_p@1h(44k 行)、range_key@1h(45k 行)、
    # range_hard_up@1h(20k 行)、range_hard_dn@1h(20k 行) —— **一条都没装**。
    #
    # 后果不是报错，是静默：`LineBook.at("range_p@1h", ts)` 对每一根都返回
    # None，调用方拿到「这条线不存在」，和「这根上确实没有值」长得一模一样。
    # S5 想用区间演化的腿（P 顺向边界 / R 反向边界 / K 关键价）时就永远取不到，
    # 而且会得到一个**假测量**：曾经记下「1h 的 R 落在止损内 0/11」，
    # 当时当成几何结论，实际是因为 R 一直是 None，所以这个数字恒等于 0。
    #
    # 用 RANGE_LINE_KINDS 推导，和写入侧（kbar/lines.py::build）共用同一个常量，
    # 以后加第七、第八条线不会再需要「记得来这里补一行」——
    # 那种「记得」正是本仓库反复吃亏的「同一事实两个来源」。
    from .kbar.lines import RANGE_LINE_KINDS

    _range_keys = ["%s@1h" % k for k in RANGE_LINE_KINDS]
    keys: List[str] = []
    # ★★ 2026-11-20 修：观察线/趋势线必须**全部装进来**。
    #
    #   上一版只装了 (SLOW_LINE_KEY=ema20@15m, FAST_LINE_KEY=ema20@30m,
    #   range_soft_up@1h, range_soft_dn@1h) 四条。而 `kbar_line` 里实际有
    #   50 条线，其中 **ema50@15m / ema144@15m / prior_high@15m /
    #   prior_low@15m 一条都没装**。
    #
    #   后果不是报错，是静默 —— 而且后果比 range 那次更严重：
    #     · `thesis.observation_reject` 的观察线里 4/5 条恒为 None，
    #       于是「关键价拒绝出场」**只可能在 ema20 上触发**。
    #       11-20 那笔真实出场用的是 ema50 拒绝（62345.59），
    #       生产**永远看不到** —— 这正是 §二十六之十二 的「只装 4 键」
    #       同一个坑，上次只修了 range 线，没修 ema/prior 这几条。
    #     · `pending_plan.trend_context` 要 ema50@15m 判趋势背景，
    #       缺线时判不了，位置闸只能保守拒绝。
    #
    #   名单从**消费方自己的常量**推导（`OBSERVATION_KEYS` /
    #   `TREND_SLOW_KEY`），不另立一份 —— 以后谁加了观察线，
    #   这里自动跟上，不需要「记得来补一行」。
    _want = list(T.REQUIRED_LINE_KEYS) + list(T.OPTIONAL_LINE_KEYS)
    for k in _want + _range_keys:
        if k not in keys:           # 去重保序（SLOW/FAST 可能撞上观察线）
            keys.append(k)

    # ★★ 装完之后**验一遍**：名单里有、表里 0 行的线 = 永久 `None`。
    #
    #   这种缺失不会抛错：`LineBook.at()` 对不存在的键返回 None，而调用方
    #   拿到 None 和「这根上确实没有值」长得一模一样。于是依赖它的判据
    #   （观察线出场 / 趋势背景闸）会**静默变成永不触发**，看板上一片正常。
    #   ema50@15m 就是这么漏的 —— 而 11-20 唯一一次真实出场靠的正是它。
    #
    #   所以：缺线必须在**启动时**喊出来，而不是等到复盘时才发现。
    #
    # ★ 但要分级（2026-11-20 第一次上生产就踩到）：
    #   `REQUIRED_LINE_KEYS` 缺 = 真缺陷，ERROR；
    #   `OPTIONAL_LINE_KEYS` 缺 = **已知状态**（`ema20@30m` 生产库里从来没有，
    #   且有回退链），只能 INFO —— 拿它喊 ERROR 就是「狼来了」，
    #   喊上几次真正的那条也就没人看了。
    try:
        con = sqlite3.connect(path, timeout=5)
        try:
            have = {str(r[0]) for r in
                    con.execute("select distinct line_key from kbar_line")}
        finally:
            con.close()
        missing = [k for k in T.REQUIRED_LINE_KEYS if k not in have]
        if missing:
            logger.error(
                "[挂单] kbar_line 缺这些必需线：%s —— 依赖它们的判据会静默失效"
                "（观察线出场只可能在剩下的线上触发、趋势背景闸会保守拒单）。"
                "补数据：python tools/ingest_kbar.py", ", ".join(missing))
        else:
            logger.info("[挂单] 必需线路装载齐全：%d 条", len(T.REQUIRED_LINE_KEYS))
        opt_missing = [k for k in T.OPTIONAL_LINE_KEYS if k not in have]
        if opt_missing:
            logger.info(
                "[挂单] 可选线缺失（已知，有回退，不影响）：%s",
                ", ".join(opt_missing))
    except Exception as e:          # 表可能还没建（首启）——不能因此不启动
        logger.warning("[挂单] 线路自检失败（不影响启动）：%s", e)

    #: 缓存：LineBook 是 `kbar_line` 表内容的**纯函数**，同样的表就该得到同样的
    #: book 对象。引擎每根新 5m 收盘都会要一次（`_step_pending` 的 5m 闸），
    #: 实测重建要 0.9 秒 / 108900 点，装齐六条 range 线后 1.95 秒 / 240000 点。
    #: 按 (max_ts, max_rowid) 作键：新 K 线进来 max_ts 变，补历史进来 max_rowid 变，
    #: 两种变化都能自己发现，不需要谁记得来清缓存。
    _cache: dict = {"sig": None, "book": None}

    def _signature():
        con = sqlite3.connect(path, timeout=5)
        try:
            r = con.execute("select max(ts), max(rowid) from kbar_line").fetchone()
        finally:
            con.close()
        return (int(r[0] or 0), int(r[1] or 0))

    def _build():
        sig = _signature()
        cached = _cache.get("book")
        if cached is not None and _cache.get("sig") == sig:
            return cached
        book = T.LineBook()
        con = sqlite3.connect(path, timeout=5)
        try:
            for k in keys:
                for ts, price, ready in con.execute(
                        "select ts,line_price,line_ready_ms from kbar_line"
                        " where line_key=? order by ts", (k,)):
                    book.add((k, T.LinePoint(price=float(price),
                                             src_ts=int(ts),
                                             ready_ms=int(ready or 0))))
        finally:
            con.close()
        _cache["sig"] = sig
        _cache["book"] = book
        return book

    return _build


async def run(cfg, seconds: int = 0, with_web: bool = True,
              feed_only: bool = False) -> None:
    """主循环。

    feed_only=True 时**只采集**：行情 + 多品种 1h + 区间演化照跑，
    但完全不构造执行器 / 闸门 / 决策引擎，因此不可能产生任何订单。
    用于验证行情链路、补历史 bar，以及在实盘配置下做安全自检。
    """
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

    if feed_only:
        # --feed-only：只采集，不建执行器、不建闸门、不建决策引擎。
        #
        # 为什么必须先判：下面每一行都在构造「可能下单」的对象
        # （LiveExecutor 会连交易所，DecisionEngine.attach() 会开始跑决策循环）。
        # 2026-11-09 审计发现这个入口没有"只采集"模式 —— `--no-web` 只关 Web，
        # 引擎照跑。想验证行情链路通不通，就得连带起一个决策循环，
        # 这在实盘配置下是不可接受的。
        #
        # 连带后果：feed-only 也不建 Web（页面依赖 engine，强制喂 None 更容易出事），
        # 想边采边看请用正常运行模式。
        executor = guard = engine = None
        logger.warning("=== 只采集模式：不构造执行器/闸门/决策引擎，"
                       "不会产生任何订单，也不开 Web ===")
    elif cfg.trading.mode == "live":
        # 实盘：缺凭证就直接拒绝启动，绝不"降级成纸面"偷偷跑起来——
        # 那样最危险：你以为在下真单，其实在记账。
        # ★ 2026-11-17 用户裁决：只做合约。现货执行器从交易路径彻底断开。
        #
        # 以前这里是 `if exec_market == "spot": SpotExecutor(...) else: LiveExecutor(...)`。
        # 那个 if 不只是“选哪个执行器”：现货不能做空，而
        # pending 策略的 11 条计划里约一半是 DOWN。
        # 保留分支 = 保留一条“一半计划永远开不出仓”的静默失败路径。
        executor = LiveExecutor(feed, cfg, cfg.trading.api_key,
                                cfg.trading.api_secret,
                                proxy=cfg.market.http_proxy)
        logger.warning("=== 实盘永续：真实资金，含资金费成本，verify-first 已启用 ===")
    else:
        executor = PaperExecutor(feed, cfg)
        if hasattr(executor, "restore_from_db"):
            executor.restore_from_db(tdb)
    if not feed_only:
        hook = feishu_notifier(os.environ.get("AI_FEISHU_WEBHOOK", ""))
        guard = Guard(cfg, tdb=tdb, journal=journal, notify=hook)
        # settings 必须传进去：4 个仓位参数（本金/风险%/杠杆/保证金上限）
        # 的唯一来源是 Web 配置库，闸门每笔开仓前都要现读一次。
        # 不传的话 _sizing_params() 返回 None，闸门会一律拒绝开仓 —— 宁可不开，
        # 也不按硬编码默认值在真钱上算仓位。
        # ★ 挂单策略的预期仓库。建在同一个目录下，跟交易库同生命周期。
        #
        # 为什么必须落库而不是放内存：状态机每根 5m 往前推一次，两次推之间
        # 进程可能重启，而**交易所那头挂着的单还活着**。忘了状态就会重复挂单
        # 或永远不平仓。库里的 (thesis, bar_ts, kind) 还是动作幂等的键。
        pending_store = PendingThesisStore(base + "_pending.db")
        lines = kbar_lines_for(cfg)
        if str(getattr(cfg.trading, "strategy", "")).lower() == "pending":
            logger.warning("挂单策略：%s（%s）",
                           "只出计划不挂单（dry-run）" if cfg.trading.pending_dry_run
                           else "会下真实订单",
                           "AI_PENDING_DRY_RUN=0 才会真的挂单")
        # ★ S5：K 线演化视图（出场三道闸的输入）
        kbar_view = kbar_view_for(cfg)
        # ★ 计划源（不接就永远不会有新计划）
        plan_source = pending_source_for(cfg, lines)
        engine = DecisionEngine(cfg, feed, executor, tdb, guard=guard,
                                settings=settings, pending_store=pending_store,
                                pending_lines=lines, pending_kbar=kbar_view,
                                pending_source=plan_source)
        engine.attach()
        await engine.reconcile_live()
        # ★ BUS-A：把出站拦截链打出来。
        #
        # 为什么值得占一行启动日志：这套架构的核心主张是「拦截是有序的、
        # 具名的、唯一的出口」。而 2026-11-17 那个缺陷之所以能活下来，
        # 正是因为挂单路径**漏了**一道闸，而没有任何东西会说"这关不见了"。
        # 打印出来之后，"这道闸还在不在、排第几"从代码问题变成运维一眼可见。
        try:
            logger.info("[出站] 挂单开仓拦截链（顺序即优先级）：%s",
                        " → ".join(engine._pending_router().gate_names()))
        except Exception as e:      # 打不出来不能影响启动
            logger.warning("[出站] 拦截链自检失败（不影响启动）：%s", e)

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
    # engine 可能是 None（feed-only），所以这里必须短路取值，
    # 不能写 `engine._last_equity` —— 那会在只采集模式下直接 AttributeError，
    # 而且是在启动日志这一行，看起来像"行情起不来"。
    _truth = ("（引擎可用 %.2fU）" % (engine._last_equity or 0.0)
              if (engine is not None and engine._last_equity) else "")
    if engine is None:
        logger.info("只采集模式运行中：品种=%s（无执行器、无决策引擎）",
                    cfg.trading.symbol)
    else:
        logger.info("决策引擎已挂载：mode=%s 策略=%s 执行器余额=%s%s 品种=%s",
                    executor.name, cfg.trading.strategy, _bal_txt, _truth,
                    getattr(cfg.trading, "exec_market", "-"))

    board = None
    if with_web and not feed_only:
        board = BoardServer(feed, cfg.web.host, cfg.web.port + 1, cfg.web.auth_token)
        await board.start()
        threading.Thread(target=_start_flask,
                         args=(cfg, feed, store, engine, tdb, journal, guard,
                               settings, range_db, asyncio.get_event_loop()),
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
    ap.add_argument("--feed-only", action="store_true",
                    help="只采集行情/聚合/区间演化，不构造执行器和决策引擎，"
                         "不可能下单；也不开 Web")
    args = ap.parse_args()
    cfg = cfgmod.load(args.config)
    _setup_logging(cfg.log_level)
    if args.feed_only and not args.no_web:
        # 静默忽略会让运维以为 Web 起来了。明说。
        logger.warning("--feed-only 已隐含 --no-web：Web 页面依赖决策引擎，"
                       "只采集模式下不启动")
    logger.info("automate-ai-trader 启动 env=%s symbol=%s db=%s feed_only=%s",
                cfg.env, cfg.market.symbol, cfg.store.path, args.feed_only)
    try:
        asyncio.run(run(cfg, seconds=args.seconds,
                        with_web=not args.no_web and not args.feed_only,
                        feed_only=args.feed_only))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()