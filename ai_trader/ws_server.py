"""看板 WebSocket 服务（aiohttp），把 Feed 的广播推给浏览器。

选 aiohttp 而非 Flask-SocketIO：Flask 在独立线程里跑，跨线程推送需要额外桥接；
aiohttp 与采集同在一个事件循环，push 零延迟、零锁。
"""
from __future__ import annotations

import asyncio
import json
import logging

from aiohttp import WSMsgType, web

from .feed import Feed

logger = logging.getLogger(__name__)


class BoardServer:
    def __init__(self, feed: Feed, host: str, port: int, auth_token: str = ""):
        self.feed = feed
        self.host = host
        self.port = port
        self.auth_token = auth_token
        self._runner = None
        self._site = None

    async def _handle(self, request: web.Request) -> web.WebSocketResponse:
        if self.auth_token:
            tok = request.query.get("token") or request.headers.get("X-Auth-Token")
            if tok != self.auth_token:
                raise web.HTTPUnauthorized()
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=4 * 1024 * 1024)
        await ws.prepare(request)
        q = self.feed.hub.subscribe()
        # 连接即刻推一次历史 + 快照，避免"先白屏等下一根 K"
        try:
            await ws.send_json({"type": "hello",
                                "snapshot": self.feed.snapshot(),
                                "history": {}})
            for iv in (180000, 60000, 15000, 3000):
                rows = self.feed.store.query_bars(iv, limit=300)
                if rows:
                    await ws.send_json({"type": "history", "interval_ms": iv, "bars": rows})
            while True:
                try:
                    msg = await asyncio.get_event_loop().run_in_executor(None, q.get, True, 1.0)
                except Exception:
                    # 超时：发个心跳，顺带检测浏览器是否已断开
                    if ws.closed:
                        break
                    await ws.send_json({"type": "ping", "ts": self.feed.snapshot()["server_ts"]})
                    continue
                if ws.closed:
                    break
                await ws.send_str(json.dumps(msg, separators=(",", ":")))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("看板连接结束", exc_info=True)
        finally:
            self.feed.hub.unsubscribe(q)
        return ws

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/ws", self._handle)
        # shutdown_timeout=2：这是关闭慢的真正原因。
        # AppRunner.cleanup() 默认会等所有活跃连接"优雅关闭"，而浏览器那条
        # 看板 WebSocket 是长连接、不会主动发关闭帧 —— 于是 cleanup 干等到
        # 默认的 60 秒，表现为「服务停不下来，要等 systemd 90 秒 SIGKILL」。
        # 注意这不是"绕过"：看板是只读推送，掐掉连接不丢任何数据。
        self._runner = web.AppRunner(app, access_log=None, shutdown_timeout=2.0)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        self._site = site
        await site.start()
        logger.info("看板 WebSocket 已监听 ws://%s:%d/ws", self.host, self.port)

    async def stop(self) -> None:
        """关闭分两段：先同步关监听（快、必须等），再把 cleanup 甩到后台。

        为什么不能 await cleanup()：
        aiohttp 的 AppRunner.cleanup() 会**捕获 CancelledError** 并继续等
        内部连接结束，所以 asyncio.wait_for 的超时对它无效——实测
        wait_for(4.0) 仍然卡了 15.7 秒才返回。这是 aiohttp 的已知行为，
        不是超时值设小了。

        而看板只是只读推送：掐断连接不丢任何数据、不影响任何状态，
        浏览器 3 秒后会自己重连。所以它没有任何资格进入关闭的关键路径。
        这里只保证端口释放（下一个实例能立刻绑定），剩下的交给进程退出。
        """
        if self._site is not None:
            try:
                await asyncio.wait_for(self._site.stop(), timeout=3.0)
            except (asyncio.TimeoutError, Exception) as e:
                logger.warning("看板监听关闭异常: %s", str(e)[:100])
            self._site = None
        if self._runner is not None:
            runner = self._runner
            self._runner = None
            # 不 await：甩成后台任务，进程退出时随之消失。
            # 加 done callback 只为吞掉可能的异常，避免 "Task exception
            # was never retrieved" 噪音。
            task = asyncio.create_task(runner.cleanup())
            task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
