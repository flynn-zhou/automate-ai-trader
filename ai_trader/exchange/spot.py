"""现货执行器（币安现货 BTCUSDT）。

为什么最终选择现货而不是永续：这条策略只做多，而永续的空头资金费率
长期为正（多头付钱），实测平均 0.01062%/8h ≈ 年化 11.63%。
1.5x 杠杆下这是每年 17% 的纯损耗，而且杠杆会等比例放大它。

实测六年（含真实费率）：
  现货 1x taker 0.10%          +474%，回撤 48%，卡玛 0.61
  永续 1.5x 含资金费            +457%，回撤 66%，卡玛 0.43
现货手续费虽是永续两倍，但省下的资金费远大于多付的费用。

附带好处：没有爆仓可能，最小名义 5U（永续 50U），数量步长 0.00001（永续 0.001）。
对 100U 的账户，这意味着能精确建仓而不是被迫 77U 一跳。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from typing import Optional
from urllib.parse import urlencode

import aiohttp

from .base import Executor, FillResult

logger = logging.getLogger(__name__)


class SpotError(RuntimeError):
    pass


class SpotExecutor(Executor):
    name = "spot"

    def __init__(self, feed, cfg, api_key: str, api_secret: str,
                 base: str = "https://api.binance.com", proxy: str = ""):
        self.feed = feed
        self.cfg = cfg
        self._key = api_key
        self._secret = api_secret
        self._base = base.rstrip("/")
        self._proxy = proxy or None
        self._session: Optional[aiohttp.ClientSession] = None
        self._spec_cache = None
        self._transport = None
        self.halted: str = ""
        # 熔断置位的时刻。用来让熔断【自己过期】——
        # 见 _maybe_clear_halt()。没有这个时间戳，一次瞬态错误就要靠人重启，
        # 7x24 运行等于永远停摆。
        self.halted_ts: float = 0.0
        # 熔断多久后自动解除。取 600 秒：
        # 周期对账是 5 分钟一次，两个对账周期足以分辨「瞬态抖动」和「真的对不上」。
        # 真问题在解除后会立刻被再次触发并留下日志，不会悄悄溜过去。
        self.halt_ttl_s: float = 600.0
        # 凭证被交易所永久拒绝后置为原因字符串：不再发签名请求。
        # 见 _note_auth_failure：key 错了不会自己变对，重试只会烧配额和 IP。
        self._auth_dead: str = ""

    async def _sess(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=12),
                connector=aiohttp.TCPConnector(limit=4, ttl_dns_cache=300))
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    _AUTH_MARKS = ("-2014", "-2015", "-1022", "HTTP 401", "HTTP 403",
                   "API-key", "Signature")

    def _scan_auth_error(self, path: str, msg: str, signed: bool) -> None:
        """从错误内容判断是不是凭证问题。

        看内容而不是看"从哪条路回来的"：HTTP 分支、注入的 transport、
        未来任何新路径，只要说的是凭证错误，就都要熔断。
        """
        if not signed:
            return
        if any(m in msg for m in self._AUTH_MARKS):
            self._note_auth_failure(path, msg)

    def _note_auth_failure(self, path: str, txt: str) -> None:
        """凭证永久性失效：只记一次，之后不再发请求。

        币安用 -2014（key 格式错）/ -2015（key 无效或权限不足）/ 401 表示
        凭证问题。这类错误重试一万次结果都一样，重试只会耗配额、还可能被封 IP。
        """
        if self._auth_dead:
            return
        self._auth_dead = "%s: %s" % (path, txt[:200])
        logger.critical(
            "实盘凭证被交易所拒绝，已停止所有签名请求（不会再重试）：%s"
            "  这不会自己恢复。请检查 /etc/ai-trader/env 里的 AI_API_KEY / "
            "AI_API_SECRET，改好后重启 ai-trader。", self._auth_dead)

    def _sign(self, params: dict) -> str:
        qs = urlencode(params)
        return qs + "&signature=" + hmac.new(
            self._secret.encode(), qs.encode(), hashlib.sha256).hexdigest()

    async def _req(self, method: str, path: str, params: Optional[dict] = None,
                   signed: bool = True):
        if signed and self._auth_dead:
            # 已经确认凭证是坏的，不再发请求。
            # 静默重试在这里没有意义，只会把配额和 IP 都烧掉。
            raise SpotError("凭证无效，已停止请求（%s）" % self._auth_dead)
        p = dict(params or {})
        body, headers = "", {}
        if signed:
            p["timestamp"] = int(time.time() * 1000)
            p["recvWindow"] = 5000
            body = self._sign(p)
            headers = {"X-MBX-APIKEY": self._key}
        elif p:
            body = urlencode(p)
        if self._transport is not None:
            try:
                return await self._transport(method, path, p, signed)
            except SpotError as e:
                self._scan_auth_error(path, str(e), signed)
                raise
        try:
            s = await self._sess()
            url = self._base + path
            if method == "GET":
                async with s.get(url + ("?" + body if body else ""),
                                 headers=headers, proxy=self._proxy) as r:
                    txt = await r.text()
                    if r.status != 200:
                        raise SpotError("HTTP %s %s: %s" % (r.status, path, txt[:300]))
                    return json.loads(txt)
            async with s.post(url, data=body, headers=headers,
                              proxy=self._proxy) as r:
                txt = await r.text()
                if r.status != 200:
                    raise SpotError("HTTP %s %s: %s" % (r.status, path, txt[:300]))
                return json.loads(txt)
        except SpotError as e:
            self._scan_auth_error(path, str(e), signed)
            raise
        except Exception as e:
            raise SpotError("网络失败 %s: %s" % (path, e))

    async def spec(self) -> dict:
        if self._spec_cache:
            return self._spec_cache
        d = await self._req("GET", "/api/v3/exchangeInfo",
                            {"symbol": self.cfg.trading.symbol}, signed=False)
        s = (d.get("symbols") or [{}])[0]
        f = {y["filterType"]: y for y in s.get("filters", [])}
        lot = f.get("LOT_SIZE", {})
        notional = f.get("NOTIONAL", {})
        self._spec_cache = {
            "step": float(lot.get("stepSize") or 0.00001),
            "min_qty": float(lot.get("minQty") or 0.00001),
            "min_notional": float(notional.get("minNotional") or 5.0),
            "qty_precision": int(s.get("baseAssetPrecision", 8)),
            "base": s.get("baseAsset", "BTC"),
            "quote": s.get("quoteAsset", "USDT"),
        }
        return self._spec_cache

    def round_qty(self, qty: float, step: float) -> float:
        if step <= 0:
            return qty
        n = int(round(qty / step, 12) // 1)
        return round(n * step, 12)

    async def mark_price(self) -> float:
        """向交易所要一次权威现价。

        用途：止损/熔断这种"一执行就不可逆"的动作，不能只信本地行情缓存。
        行情断了的时候本地价格是陈旧的，拿陈旧价去平仓等于按幽灵价格卖币。
        """
        d = await self._req("GET", "/api/v3/ticker/price",
                            {"symbol": self.cfg.trading.symbol}, signed=False)
        return float(d.get("price") or 0.0)

    async def my_trades(self, limit: int = 500) -> list:
        """最近的成交明细（币安 /api/v3/myTrades）。

        现货的致命短板是【交易所只记余额，不记开仓价】。
        以前接管孤儿仓位时只能拿"当前市价"当成本基准，
        这在价格已经走开的情况下会算错两件事：
          1. 止损价按错误成本算，可能离真实成本很远；
          2. 平仓时 pnl 按错误成本结算，把真实亏损记小。
        对"用实测数据找真实边际"这个目标来说，第 2 条是致命的——
        账记错了，边际就是假的。
        这个端点能拿到真实成交价，所以成本基准不用再猜。
        """
        d = await self._req("GET", "/api/v3/myTrades",
                            {"symbol": self.cfg.trading.symbol, "limit": int(limit)})
        return d if isinstance(d, list) else []

    async def cost_basis(self) -> dict:
        """从真实成交回放，算出当前持仓的加权平均成本。

        按时间正序回放：买入累加成本，卖出按当时均价扣减成本。
        返回 {"qty": 净持仓, "avg": 加权均价, "ok": 是否可信}。
        ok=False 表示成交历史对不上当前持仓（例如充值进来的币、
        或历史被 limit 截断），此时调用方不应使用这个金额当成本。
        """
        try:
            trades = await self.my_trades()
        except Exception as e:
            logger.warning("[现货] 取成交历史失败，无法计算成本基准：%s", e)
            return {"qty": 0.0, "avg": 0.0, "ok": False}
        rows = sorted(trades, key=lambda x: int(x.get("time", 0) or 0))
        # 买入的手续费扣在【基础币】上，成交明细里的 qty 却是【成交毛量】。
        # 不把手续费减掉，回放出来的净持仓就会系统性地比真实余额大，
        # 差值正好等于累计用基础币支付的手续费。
        #
        # 实测（100U、0.1%/边、78000）：7 笔之后偏差 0.689%，
        # 而 reconcile_live 的容差是 0.5% —— 于是【重启时若正持仓】，
        # 成本基准会被判"对不上"而弃用，退回"按当前价当成本"并打 CRITICAL。
        # 那正是这段代码自己警告过的坏情况：成本错了，止损价跟着错，
        # 平仓 pnl 也会把真实亏损记小，边际统计全废。
        # 必须主动取一次 spec：self._spec_cache 只有在 spec() 被调用过之后才有值。
        # 读缓存会让 base_asset 在这里悄悄变成空字符串——手续费就一笔都不扣，
        # 而且不报错，看起来"修好了"其实没生效。（第 58 轮实测踩到：
        # 加了扣减逻辑后偏差仍是 0.705%，因为缓存是空的。）
        base_asset = ""
        try:
            spec = await self.spec()
            if spec.get("base"):
                base_asset = str(spec["base"])
        except Exception:
            logger.warning("[现货] 取交易规则失败，成本基准无法扣手续费", exc_info=True)
        qty = 0.0
        cost = 0.0
        for t in rows:
            q = float(t.get("qty", 0) or 0)
            px = float(t.get("price", 0) or 0)
            if q <= 0 or px <= 0:
                continue
            if t.get("isBuyer"):
                # 只有"手续费币种 == 基础币"时才影响持仓数量；
                # 手续费若以 USDT 计价（返佣、BNB 抵扣等），持仓数量就是毛量。
                comm = float(t.get("commission", 0) or 0)
                q_net = q
                if base_asset and t.get("commissionAsset") == base_asset:
                    q_net = max(0.0, q - comm)
                qty += q_net
                # 成本按【实付金额】累计：q*px 是成交额，被当手续费扣掉的那部分币
                # 也是钱换来的。用 q_net*px 会把成本低估一个手续费率
                # （实测：买 0.001@40000，实付 40U 只到手 0.000999，
                #  真实成本是 40040 而不是 40000），于是每笔 pnl 都系统性偏乐观。
                # 账错了，边际就是假的。
                cost += q * px
            else:
                if qty <= 1e-12:
                    continue
                avg = cost / qty
                take = min(q, qty)
                qty -= take
                cost -= avg * take
                if qty <= 1e-12:
                    qty = 0.0
                    cost = 0.0
        if qty <= 1e-12:
            return {"qty": 0.0, "avg": 0.0, "ok": True}
        return {"qty": qty, "avg": cost / qty, "ok": True}

    async def position(self) -> dict:
        """当前现货持仓（供启动对账用）。

        现货没有"开仓价"这个概念——交易所只知道你持有多少币。
        entry 返回 None，由上层决定用什么兜底（绝不能用 0：
        entry=0 会让止损价算成 0，等于"永远触发止损"，一开机就平仓）。
        """
        e = await self.equity()
        qty = float(e["position_qty"])
        # 粉尘必须当作空仓，否则系统在第一笔交易之后就会永久卡死。
        #
        # 现场推导（每一笔往返都会发生）：
        #   买入 0.00128 BTC，手续费 0.1% 扣在 BTC 上 -> 净到账 0.00127872
        #   平仓按 step=0.00001 向下取整 -> 只能卖 0.00127，剩 0.00000872
        #   这 0.00000872 比一个 step 还小，交易所根本不收，永远卖不掉
        # 于是：
        #   引擎 self.pos = None，但对账看到交易所还有 0.00000872（容差 1e-9，
        #   粉尘比它大 8700 倍）-> 判定不一致 -> "以交易所为准"接管这笔粉尘
        #   -> 引擎认为自己持仓中，不再开新仓
        #   -> 要平掉它时 qty 取整成 0，market_order 直接拒绝
        #   -> 系统做完一笔就永久卡住，既不交易也不报错
        #
        # 注意：这里改的是【可交易性】，不是估值。
        #   粉尘的钱依然算在 equity 里（见 equity()），不会凭空消失。
        try:
            spec = await self.spec()
            step = float(spec.get("step") or 0.0)
        except Exception:
            step = 0.0
        if step > 0 and 0.0 < abs(qty) < step:
            logger.info("[现货] 持仓 %.8f 小于一个步长 %.8f，按粉尘处理（视为空仓）",
                        qty, step)
            qty = 0.0
        return {"qty": qty, "entry": None, "side": "long" if qty > 0 else "",
                "leverage": 1.0}

    async def equity(self) -> dict:
        """现货权益 = USDT 余额 + BTC 市值。"""
        d = await self._req("GET", "/api/v3/account")
        spec = await self.spec()
        free = {b["asset"]: float(b["free"]) + float(b["locked"])
                for b in d.get("balances", [])}
        usdt = free.get(spec["quote"], 0.0)
        btc = free.get(spec["base"], 0.0)
        px = self.feed.last_price or 0.0
        # 本地没价时不能把持仓估值成 0。
        # 现场：进程重启时 feed.start() 只拉起后台任务、不等第一个 tick，
        # 紧接着 main.py 就调 reconcile_live()，那一刻 last_price 还是 0。
        # 若当时手里有 BTC，usdt + btc*0 会把一个持有 100U 的账户算成只剩零头，
        # 于是：权益表被写进垃圾、_affordability_note 误报资金不足、
        # 按权益算的仓位全部算错。
        # 只在"持仓>0 且本地无价"这一种情况下才向交易所要牌价——
        # 正常有行情时一次都不多发请求（否则 3s 循环会把账户接口打爆）。
        if px <= 0 and btc > 0:
            try:
                px = await self.mark_price()
                logger.warning("[现货] 本地无价，权益改用交易所牌价 %.2f 估值", px)
            except Exception as e:
                logger.error("[现货] 本地无价且取牌价失败，权益估值将偏低：%s", e)
        return {"balance": round(usdt, 6),
                "unrealized": 0.0,
                "equity": round(usdt + btc * px, 6),
                "position_qty": btc,
                "position_side": "long" if btc > 0 else ""}

    def _maybe_clear_halt(self) -> None:
        """让熔断自己过期。

        为什么需要：熔断的唯一触发条件是「下单回报说成交、余额却没动」。
        这个判断依赖 0.4 秒后的账户查询，而交易所接口是最终一致的——
        一次瞬时延迟就会置位，而置位后所有下单（含平仓）都被拒、且没人会去重启，
        7x24 运行等于永久停摆。

        为什么过期是安全的：能导致账目暂时不准的每一种情形，
        都在别处有兜底——
          - 每一单在下单前都按【账上真实余额】截断（买入看 USDT，卖出看 BTC），
            所以账目不准不会造成超买或卖超；
          - 实盘每 5 分钟一次周期对账，用交易所持仓覆盖引擎认知；
          - 真问题在解除后会立刻再次触发熔断并重新计时，日志里一次都不会少。
        """
        if not self.halted:
            return
        age = time.time() - (self.halted_ts or 0.0)
        if self.halted_ts and age >= self.halt_ttl_s:
            logger.warning("[现货] 熔断已持续 %.0f 秒，自动解除后重试；"
                           "原原因：%s", age, self.halted[:120])
            self.halted = ""
            self.halted_ts = 0.0

    async def market_order(self, side: str, qty: float, reason: str = "",
                           reduce_only: bool = False) -> FillResult:
        self._maybe_clear_halt()
        if self.halted and not reduce_only:
            return FillResult(ok=False, reason="已熔断：" + self.halted)
        if self.halted and reduce_only:
            # 熔断期间【必须】放行减仓。
            #
            # 2026-11-02 修正：以前这里对开仓和平仓一视同仁地返回「已熔断」。
            # 而熔断是「账目不可信」时的保守姿态，不是「不许交易」的通牒——
            # 拒绝平仓的后果是止损彻底失效、敞口无上限，比账目暂时不准危险得多。
            # 而且卖出侧在下单前已按账上持币量截断（见下方 sell 分支），
            # 永远不会变成做空，所以放行减仓不会带来新的风险。
            logger.warning("[现货] 熔断期间放行减仓（%s）：%s", side, self.halted[:80])
        spec = await self.spec()
        qty = self.round_qty(qty, spec["step"])
        if qty < spec["min_qty"]:
            return FillResult(ok=False, reason="数量 %.8f < 最小 %.8f"
                              % (qty, spec["min_qty"]))
        px = self.feed.last_price or 0.0
        if px and qty * px < spec["min_notional"]:
            return FillResult(ok=False, reason="名义 %.2fU < 最小 %.2fU"
                              % (qty * px, spec["min_notional"]))

        # 下单前先看一次账户：买入看可用 USDT，卖出看可用基础币。
        #
        # 两侧都必须截断，但缺的恰好是【卖出侧】——而卖出才是保护本金的那条路。
        #
        # 买入侧不截断的后果是"少买一点/整单失败"，可恢复；
        # 卖出侧不截断的后果是交易所按 -2010（余额不足）拒单，
        # 平仓失败 -> 只能等 5 分钟一次的 reconcile_live 把 pos["qty"] 改对
        # 才会成功。在下跌里，那 5 分钟仓位是裸奔的。
        #
        # pos["qty"] 大于账上持币量有四条真实路径：
        #   1. 用户在币安 App 里手动卖了币 / 用"一键转 BNB"清了零头
        #   2. 上一笔平仓部分成交，回执数量与账面对不上
        #   3. 手续费被记在别的币种上，净额算多了
        #   4. 从旧库恢复的持仓，账上早已不同
        # 任何一条都让"卖出 p['qty']+dust"变成卖超。
        #
        # 现货卖出被截断到实际持有量永远是安全的：不会变成做空。
        acct = await self.equity()
        before = float(acct["position_qty"])
        if side.lower() == "buy" and px:
            # 留 0.15% 给手续费，防止刚好差一点。
            usable = max(0.0, acct["balance"]) / (1.0 + 0.0015)
            max_qty = self.round_qty(usable / px, spec["step"])
            if qty > max_qty:
                logger.warning("[现货] 买入量 %.8f 超可用余额，截断到 %.8f（可用 %.2fU）",
                               qty, max_qty, acct["balance"])
                qty = max_qty
            if qty < spec["min_qty"] or qty * px < spec["min_notional"]:
                return FillResult(ok=False,
                                  reason="可用余额 %.2fU 不足以满足最小下单"
                                  % acct["balance"])
        elif side.lower() == "sell":
            avail = self.round_qty(max(0.0, before), spec["step"])
            if qty > avail:
                # 这是【异常】而不是常态：正常往返里 pos["qty"] 与账上差
                # 不会超过一个 step（取整残留）。差额明显更大时说明
                # 账和记录真的分家了，必须让它出现在日志里而不是悄悄截断。
                logger.warning("[现货] 卖出量 %.8f 超账上持币 %.8f，"
                               "截断到 %.8f（差额 %.8f 已不在账户里）",
                               qty, before, avail, qty - avail)
                qty = avail
            # 截断后仍然凑不出最小下单（典型：账上只剩取整零头），
            # 明确说"没东西可卖"，而不是发一张必然被拒的单。
            # 上层据此会走进"对账已无此仓"的分支，而不是无限重试。
            if qty < spec["min_qty"] or (px and qty * px < spec["min_notional"]):
                return FillResult(
                    ok=False,
                    reason="账上无可卖数量（持币 %.8f，可卖 %.8f < 最小 %.8f）"
                    % (before, qty, spec["min_qty"]))
        try:
            r = await self._req("POST", "/api/v3/order", {
                "symbol": self.cfg.trading.symbol, "side": side.upper(),
                "type": "MARKET",
                "quantity": ("%.*f" % (spec["qty_precision"], qty))})
        except SpotError as e:
            logger.error("[现货] 下单失败 %s %.8f: %s", side, qty, e)
            return FillResult(ok=False, reason=str(e)[:200])

        filled = float(r.get("executedQty", 0) or 0)
        quote_qty = float(r.get("cummulativeQuoteQty", 0) or 0)
        avg = (quote_qty / filled) if filled else (px or 0.0)

        # 手续费必须【在这里】就解析出来，不能等到最后。
        #
        # 原来这段在余额核对之后，于是核对时还不知道手续费扣在哪个币上，
        # 期望值的算法天然就是错的 —— 这是上一版把部分成交判成
        # 「余额核对失败」并永久熔断的根因。
        #
        # 手续费扣在哪个币种上，必须读交易所回报，不能假设：
        #   executedQty = 0.00128 BTC
        #   实际到账    = 0.00128 - 0.0000128 = 0.0012672 BTC（买时扣 BTC）
        # 若拿 executedQty 当持仓数量，平仓卖出会多卖 0.1%，
        # 交易所直接以「余额不足」拒单——仓位永远平不掉。
        # 这个错误在旧假交易所上测不出来：它把手续费记在了 USDT 上。
        base_asset = spec.get("base") or ""
        fee_base = 0.0
        fee_quote = 0.0
        fills = r.get("fills") or []
        fee_is_real = bool(fills)
        for f in fills:
            try:
                c = float(f.get("commission") or 0)
            except (TypeError, ValueError):
                continue
            if f.get("commissionAsset") == base_asset:
                fee_base += c
            else:
                fee_quote += c
        if not fee_is_real:
            # 回报里没有 fills：退回估算，并明确按 USDT 处理。
            # 必须用【现货】费率（0.1%），不能用永续的 0.05%——
            # 兜底低估会让 PnL 偏乐观，而这条路径只有交易所不返回 fills 时才走，
            # 也就是最不该再低估的时候。
            _fee_rate = self.cfg.trading.taker_fee_for(True)
            fee_quote = abs(avg * filled) * _fee_rate
            logger.warning("[现货] 交易所未返回 fills，手续费按 %.4f%% 估算 %.6fU",
                           _fee_rate * 100, fee_quote)

        # 同样不信任返回值：用余额核对真实成交
        import asyncio
        await asyncio.sleep(0.4)
        after = (await self.equity())["position_qty"]
        # ★ 期望值必须按【实际成交量】算，不能按【请求量】。
        #
        # 这里曾经用 qty（请求量），于是部分成交会被判成「余额核对失败」：
        # 请求 0.0005、成交 0.0003，期望 0.0005 实际 0.0003，差额远超容差。
        # 后果不是"报个错"那么轻——它会回滚（把真买到的卖回去）
        # 并把 self.halted 置位，之后所有下单都返回「已熔断」，
        # 只有人工重启才恢复。一次正常的部分成交能让系统永久停止交易。
        #
        # 但这个检查本身必须保留：它防的是「回报说成交、余额却没动」。
        # 所以修的是期望值的算法，不是放宽检查——见最后一条测试。
        #
        # 买入时手续费扣在基础币上，到账比成交少 fee_base，要一并算进期望。
        signed = (filled - fee_base) if side.lower() == "buy" else -filled
        if filled <= 0:
            # 完全没成交：干净失败，不熔断。下一轮还能再试。
            logger.warning("[现货] %s 回报成交量为 0（请求 %.8f），本轮视为未成交",
                           side, qty)
            return FillResult(ok=False, reason="交易所回报成交量为 0（未成交）")
        expected = before + signed
        tol = max(spec["step"] * 2, abs(expected) * 0.02)
        if abs(after - expected) > tol:
            msg = ("余额核对失败：期望 %.8f，实际 %.8f（下单前 %.8f，回报成交 %.8f）"
                   % (expected, after, before, filled))
            logger.critical("[现货] %s", msg)
            try:
                back = after - before
                if abs(back) > 0:
                    await self._req("POST", "/api/v3/order", {
                        "symbol": self.cfg.trading.symbol,
                        "side": "SELL" if back > 0 else "BUY", "type": "MARKET",
                        "quantity": ("%.*f" % (spec["qty_precision"], abs(back)))})
            except Exception:
                logger.critical("[现货] 回滚也失败，需人工介入", exc_info=True)
            self.halted = msg
            self.halted_ts = time.time()
            return FillResult(ok=False, reason=msg)

        # 到账净额：买入时扣在基础币上的手续费要减掉
        net_qty = max(0.0, filled - fee_base) if side.lower() == "buy" else filled
        fee_usdt = fee_quote + fee_base * (avg or 0.0)
        if fee_base:
            logger.info("[现货] %s 手续费 %.8f %s 从成交币中扣除，净到账 %.8f",
                        side, fee_base, base_asset, net_qty)

        logger.info("[现货] %s %.8f @ %.2f 持仓 %.8f (%s)",
                    side, net_qty, avg, after, reason[:40])
        return FillResult(ok=True, price=avg, qty=net_qty, fee=fee_usdt,
                          order_id=str(r.get("orderId", "")),
                          status="filled", reason=reason)
