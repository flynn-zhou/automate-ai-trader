"""行情主管线：逐笔成交 -> 3s 桶（原子事实）-> 多周期聚合 -> 落库 + 广播。

两条逐笔通道，同一套聚合与落库逻辑：
  WS   —— aggTrade 推送（有就用，最省）
  REST —— /fapi/v1/aggTrades 增量拉取（按 aggTradeId 游标，永不丢笔）
实测：本机经代理 WS 握手成功但零推送；REST 逐笔约 5.6 笔/秒、延迟 238ms，
3s 一轮 20 笔上下，1000 笔容量可覆盖约 3 分钟断流 —— 所以 REST 这条路径用于
生产完全够用，**3s 桶里照样有真实成交量与主动买卖方向**。

健康三态（写进看板，DOWN 时严禁任何交易决策）：
  WS | REST | DOWN
"""
from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time
from collections import deque
from typing import Callable, Deque, Dict, List, Set

from .config import INTERVALS_MS, AppCfg
from .market.agg import Bar, MultiAggregator
from .market.rest import RestMarket
from .market.ws import AggTradeWS
from .store import Store

logger = logging.getLogger(__name__)


class Hub:
    """广播中心：每个订阅者一个线程安全队列，满了丢最旧（看板绝不阻塞采集）。"""

    def __init__(self, maxlen: int = 4000):
        self._subs: Set[queue.Queue] = set()
        self._lock = threading.Lock()
        self._maxlen = maxlen

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=self._maxlen)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._subs.discard(q)

    def publish(self, msg: dict) -> None:
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(msg)
            except queue.Full:
                try:
                    q.get_nowait()
                    q.put_nowait(msg)
                except Exception:
                    pass

    @property
    def subscribers(self) -> int:
        with self._lock:
            return len(self._subs)


class Feed:
    def __init__(self, cfg: AppCfg, store: Store):
        self.cfg = cfg
        self.store = store
        self.agg = MultiAggregator(INTERVALS_MS)
        self.rest = RestMarket(cfg.market.fapi_base, cfg.market.symbol, cfg.market.http_proxy)
        self.ws = AggTradeWS(cfg.market.ws_url, cfg.market.symbol,
                             self.on_trade, self.on_lifecycle,
                             proxy=cfg.market.http_proxy,
                             stream=cfg.market.ws_stream)
        self.hub = Hub()

        self.src: str = "DOWN"               # WS | REST | DOWN
        self.last_price: float = 0.0
        self.last_price_ts: int = 0
        self.last_rest_ok_ts: int = 0
        self.rest_latency_ms: int = 0
        self.rest_fails: int = 0
        self.ticks_total: int = 0
        self.rest_ticks: int = 0
        self.ws_ticks: int = 0
        self.trades_per_sec: float = 0.0
        self.started_at: int = int(time.time() * 1000)
        self._last_publish = 0.0
        self._last_bar_publish: Dict[int, float] = {}
        self._tasks: List[asyncio.Task] = []
        self._gap_marks: Dict[int, int] = {}
        self._recent_trades: Deque[dict] = deque(maxlen=500)
        self._rest_cursor: int = 0           # 已消费的最后一个 aggTradeId
        # 已收盘 bar 的滚动历史（决策层的唯一输入，前视安全）
        self.history: Dict[int, Deque[Bar]] = {
            iv: deque(maxlen=400) for iv in INTERVALS_MS}
        self.bar_hooks: List[Callable] = []   # 收盘回调（引擎挂这里）

    # ---------------- 生命周期 ----------------
    async def start(self) -> None:
        self.store.put_event("info", "feed", "start",
                             "symbol=%s intervals=%s mode=%s" % (
                                 self.cfg.market.symbol, INTERVALS_MS, self.cfg.market.tick_mode))
        await self._seed_history()
        await self._bootstrap_ticks()
        if self.cfg.market.enable_ws and self.cfg.market.tick_mode != "rest":
            await self.ws.start()
        if self.cfg.market.enable_rest_fallback:
            self._tasks.append(asyncio.create_task(self._rest_loop(), name="rest-ticks"))
        self._tasks.append(asyncio.create_task(self._heartbeat_loop(), name="heartbeat"))
        self._tasks.append(asyncio.create_task(self._prune_loop(), name="prune"))

    async def stop(self) -> None:
        """顺序：cancel -> 等它们真结束 -> 关 WS -> 关 REST。

        以前是 cancel 完立刻 close() session，任务其实还停在 await 里用它，
        于是 aiohttp 退出时吐 "Unclosed client session"，进程要等 systemd
        的 TimeoutStopSec（90 秒）才被 SIGKILL —— 表现就是"服务停不下来"。

        cancel() 只是**请求**取消，不是"已经结束"。必须 await 才能确认它
        真的从 aiohttp 调用里退出来了。
        """
        for t in self._tasks:
            t.cancel()
        if self._tasks:
            # 每一步都限时：某个任务卡住不能把整个关闭拖死
            try:
                await asyncio.wait_for(
                    asyncio.gather(*self._tasks, return_exceptions=True),
                    timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("行情任务 5s 内没退干净，继续关闭（可能有任务卡在网络调用）")
            self._tasks.clear()
        try:
            await asyncio.wait_for(self.ws.stop(), timeout=6.0)
        except asyncio.TimeoutError:
            logger.warning("关闭逐笔 WS 超时 6s，继续")
        try:
            await asyncio.wait_for(self.rest.close(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("关闭 REST session 超时 5s，继续")

    async def _seed_history(self) -> None:
        """启动时用官方 REST K 线预热 1m/3m/15m 历史，保证看板一打开就有图看。"""
        for name, iv in (("1m", 60000), ("3m", 180000), ("15m", 900000)):
            try:
                rows = await self.rest.klines(name, limit=500)
            except Exception as e:
                logger.warning("预热 %s 失败: %s", name, e)
                continue
            for r in rows[:-1]:          # 最后一根未收盘，不落库
                b = Bar(interval_ms=iv, open_time=int(r[0]), open=float(r[1]),
                        high=float(r[2]), low=float(r[3]), close=float(r[4]),
                        volume=float(r[5]), quote_volume=float(r[7]),
                        trades=int(r[8]), closed=True)
                b.buy_base = float(r[9]) if len(r) > 9 else 0.0
                b.sell_base = max(0.0, float(r[5]) - b.buy_base)
                self.store.put_bar(b)
                self.history[iv].append(b)      # 让决策层冷启动就有上下文
            logger.info("预热 %s 完成：%d 根", name, len(rows) - 1)
        self.store.put_event("info", "feed", "seeded", "1m/3m/15m 历史已预热")

    async def _bootstrap_ticks(self) -> None:
        """冷启动回补：把最近的逐笔成交喂进聚合器，避免启动头 3 秒桶为空。"""
        try:
            rows = await self.rest.agg_trades(limit=self.cfg.market.rest_tick_bootstrap)
        except Exception as e:
            logger.warning("逐笔回补失败: %s", e)
            return
        for r in rows:
            self._rest_cursor = max(self._rest_cursor, r["id"])
            closed, updated = self.agg.feed(r["price"], r["ts"], r["qty"],
                                            r["price"] * r["qty"], r["is_buy"], 1)
            self._after_feed(closed, updated, r["ts"], publish=False)
        logger.info("逐笔回补完成：%d 笔，游标=%s", len(rows), self._rest_cursor)

    # ---------------- 数据入口 ----------------
    async def on_trade(self, tr: dict) -> None:
        """WS aggTrade 逐笔。"""
        self.ws_ticks += 1
        self._ingest(tr)

    def _ingest(self, tr: dict) -> None:
        # 二次设防：价格 0 的畸形成交一旦写进 last_price，
        # 引擎看到的现价就是 0（涨跌幅、止损距离、清仓判定全错）。
        # ws.py 已在边界丢弃，这里兜住其它入口（REST 回补、手动注入）。
        try:
            if not (float(tr.get("price") or 0.0) > 0.0):
                logger.warning("忽略无效价格的成交: %r", tr)
                return
        except (TypeError, ValueError):
            return
        self.ticks_total += 1
        self.last_price = tr["price"]
        self.last_price_ts = tr["ts"]
        self._recent_trades.append(tr)
        closed, updated = self.agg.feed(tr["price"], tr["ts"], tr["qty"],
                                        tr["price"] * tr["qty"], tr["is_buy"], 1)
        self._after_feed(closed, updated, tr["ts"])

    async def on_lifecycle(self, event: str, detail: str) -> None:
        lvl = "warn" if event in ("disconnected", "reconnecting") else "info"
        self.store.put_event(lvl, "ws", event, detail)
        self.hub.publish({"type": "feed_event", "level": lvl, "source": "ws",
                          "event": event, "detail": detail,
                          "ts": int(time.time() * 1000)})

    def _after_feed(self, closed: List[Bar], updated: List[Bar],
                    ts_ms: int, publish: bool = True) -> None:
        now = time.time()
        for b in closed:
            self.store.put_bar(b)
            self._check_gap(b)
            self.history[b.interval_ms].append(b)
            if publish:
                self.hub.publish({"type": "bar", "closed": True, **b.as_dict()})
            if b.interval_ms == 3000 and publish:   # 冷启动回补的历史 bar 不触发决策
                snap = {iv: list(self.history[iv]) for iv in INTERVALS_MS}
                for hook in self.bar_hooks:
                    try:
                        hook(snap, b)
                    except Exception:
                        logger.error("bar hook 失败", exc_info=True)
        for b in updated:
            if b.closed:
                continue
            th = 0.3 if b.interval_ms == 3000 else 1.0
            if now - self._last_bar_publish.get(b.interval_ms, 0) >= th:
                self._last_bar_publish[b.interval_ms] = now
                if publish:
                    self.hub.publish({"type": "bar", "closed": False, **b.as_dict()})
        if publish and now - self._last_publish >= 0.25:
            self._last_publish = now
            self.hub.publish(self.snapshot())

    def _check_gap(self, b: Bar) -> None:
        if b.interval_ms < 60000:
            return
        prev = self._gap_marks.get(b.interval_ms)
        if prev is not None and b.open_time - prev > b.interval_ms:
            missing = (b.open_time - prev) // b.interval_ms - 1
            self.store.put_gap(b.interval_ms, prev + b.interval_ms)
            self.store.put_event("warn", "feed", "bar_gap",
                                 "interval=%s missing=%d" % (b.interval_ms, missing))
        self._gap_marks[b.interval_ms] = b.open_time

    # ---------------- REST 逐笔主/兜底路径 ----------------
    async def _rest_loop(self) -> None:
        poll_s = self.cfg.market.rest_poll_ms / 1000.0
        last_count = self.ticks_total
        last_rate_ts = time.time()
        while True:
            try:
                ws_ok = self._ws_healthy()
                # WS 活着就不重复拉逐笔（省限频配额）；WS 挂了/REST 模式才拉
                if not ws_ok:
                    rows = await self.rest.agg_trades(
                        from_id=(self._rest_cursor + 1) if self._rest_cursor else 0,
                        limit=1000)
                    got = 0
                    for r in rows:
                        if r["id"] <= self._rest_cursor:
                            continue
                        self._rest_cursor = r["id"]
                        self.rest_ticks += 1
                        got += 1
                        self._ingest(r)
                    if got == 0 and self._rest_cursor:
                        # 游标之后没有新成交：补一个价格心跳，保持 K 线连续
                        try:
                            px = await self.rest.price()
                            self.last_price = px
                            self.last_price_ts = int(time.time() * 1000)
                            self.agg.feed_price_only(px, self.last_price_ts)
                        except Exception:
                            pass
                    self.last_rest_ok_ts = int(time.time() * 1000)
                    self.rest_fails = 0
                    self.src = "REST"
                else:
                    self.last_rest_ok_ts = int(time.time() * 1000)
                    self.rest_fails = 0
                    self.src = "WS"

                # 成交速率（每轮刷新一次，看板用）
                now = time.time()
                if now - last_rate_ts >= 1.0:
                    self.trades_per_sec = (self.ticks_total - last_count) / (now - last_rate_ts)
                    last_count = self.ticks_total
                    last_rate_ts = now
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.rest_fails += 1
                if self.rest_fails in (1, 5, 20) or self.rest_fails % 60 == 0:
                    logger.warning("REST 逐笔失败 x%d: %s", self.rest_fails, e)
                    self.store.put_event("error", "rest", "agg_trades_failed", str(e)[:200])
                if not self._ws_healthy():
                    self.src = "DOWN"
            await asyncio.sleep(poll_s)

    def _ws_healthy(self) -> bool:
        return (self.ws.connected
                and self.ws.seconds_since_last_msg() * 1000 < self.cfg.market.ws_stale_ms)

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            if self._ws_healthy():
                self.src = "WS"
            elif self.rest_fails == 0 and self.last_rest_ok_ts:
                self.src = "REST"
            else:
                self.src = "DOWN"
            self.hub.publish(self.snapshot())

    async def _prune_loop(self) -> None:
        """滚动清理：3s/15s/1m 只留最近 N 小时。

        两个曾经的坑（2026-11-02 查证）：

        1) sleep 写在第一次清理【之前】。
           于是每重启一次，清理就要重新等满一个小时的连续运行时间。
           线上实测：29 小时日志里只有 9 次清理，全是"0 行"——
           频繁重启时清理几乎不执行，而磁盘会一直涨到写满为止。

        2) 失败只记 debug。
           线上 AI_LOG_LEVEL=INFO，debug 直接丢掉。
           于是"清理一直失败"这件事在日志里完全不存在，
           要等到磁盘满、写库失败，才第一次看见症状。

        现在：启动后先等一小会儿（让交易面跑起来），先清一次，再按小时循环；
        失败记 warning。
        """
        first = True
        while True:
            await asyncio.sleep(120 if first else 3600)
            first = False
            try:
                n = self.store.prune(self.cfg.store.tick_retention_hours)
                logger.info("滚动清理 %d 行", n)
            except Exception:
                # 不能是 debug：级别在 INFO 时等于把故障吞掉，
                # 后果是不可控的磁盘增长，直到写满。
                logger.warning("滚动清理失败（磁盘可能无限增长）", exc_info=True)

    # ---------------- 快照 ----------------
    def snapshot(self) -> dict:
        bars = {}
        for iv in INTERVALS_MS:
            b = self.agg.current(iv)
            if b:
                bars[iv] = b.as_dict()
        return {
            "type": "state",
            "symbol": self.cfg.market.symbol,
            "src": self.src,
            "price": self.last_price,
            "price_ts": self.last_price_ts,
            "ws_connected": self.ws.connected,
            # 连上但长时间没数据，是第三种状态：不是断线，也不是健康。
            # 只报 connected 会把"连着但不推"当成健康——2026-11-02 的坑。
            "ws_stream": self.ws.url.rsplit("@", 1)[-1],
            "ws_silent": self.ws.is_silent(max(60.0, self.cfg.market.ws_stale_ms / 1000.0)),
            "ws_idle_ms": int(self.ws.seconds_since_last_msg() * 1000),
            "ws_reconnects": self.ws.reconnects,
            # 被丢弃的畸形成交数：让"流很干净"和"脏数据被静默丢掉"可区分
            "ws_dropped": self.ws.dropped,
            "ws_ticks": self.ws_ticks,
            "rest_latency_ms": self.rest_latency_ms,
            "rest_fails": self.rest_fails,
            "ticks_total": self.ticks_total,
            "rest_ticks": self.rest_ticks,
            "trades_per_sec": round(self.trades_per_sec, 2),
            "uptime_s": int((time.time() * 1000 - self.started_at) / 1000),
            "bars": bars,
            "server_ts": int(time.time() * 1000),
        }
