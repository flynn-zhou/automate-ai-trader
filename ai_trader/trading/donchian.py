# -*- coding: utf-8 -*-
"""唐奇安通道趋势跟随（1h 主周期，只做多）。

## 为什么是它

2026-11-02 在 7.15 年币安永续 1h K 线（62,679 根，2019-09-08 ~ 2026-11-02）上，
把 18 个策略族 × 4 个周期扫了一遍。5m 和 15m **全军覆没**（扣掉成本后负期望），
1h 上唐奇安是唯一同时通过全部五道关的东西：

  第 1 关 逐年拆：8 年里 6 年为正（2019 只有 4 笔、2022 熊市 -12.6%）
  第 2 关 滚动 2 年窗：7/7 全正
  第 3 关 样本外：前 70% +2.89%/笔，后 30% +1.09%/笔，两段同号
  第 4 关 参数邻域：(100~200)/(50~100) 七档全部为正，是高原不是针尖
  第 5 关 100U 真实账户（整数手 0.001 BTC / 最小名义 50U / 资金费 / 双边费 0.05%）：
         1x 最终 614.45U（+514%），最大回撤 32.4%，17.9 笔/年

跨品种同一套参数（(150/75) 1h，1x，100U）：BTC +514% / ETH +176% /
XRP +135% / BNB +469% / PAXG +33%。SOL 和 DOGE 全期更猛但在后 30% 段翻负，
所以**不用它们**——这条边界写在这里，防止以后被"单品种收益更高"诱惑。

## 和 4h SMA200 的差别

|            | 4h SMA200        | 1h 唐奇安(150/75)  |
|------------|------------------|--------------------|
| 判断频率    | 一天 6 次         | 一天 24 次          |
| 年交易笔数  | 约 25             | 约 18               |
| 每笔净收益  | +1.5% 量级        | +1.94%              |
| 最大回撤    | 30-47%           | 32.4%               |
| 停损        | 25% 硬熔断        | 无（通道出场）        |

注意笔数【没有变多】：唐奇安在高位突破才进，比 SMA 更挑。
"更高频"不是换个周期就自动得到的，是有代价的——高频版本在 5m/15m 上全是负期望。

## 三条被数据否决的做法（写进代码，防止以后重犯）

1. **不做空。** 双向版：35.5 笔/年、每笔 +1.07%、满仓复利 +404%、**回撤 51.4%**。
   只做多：17.9 笔/年、每笔 +2.42%、复利 +978%、回撤 28.4%。
   加做空等于用一半的边际换几乎两倍的回撤。
2. **不加硬停损。** 加 2.0×ATR 停损：笔数 128->189、每笔 +2.42%->+1.39%、
   复利 +978%->+680%；100U 账户上最终权益 689U->559U。
   唐奇安本来就靠"通道出场"止损，额外套一层只会砍掉正在赢的单。
3. **不做持仓时间上限。** 240 小时上限：每笔 +2.42%->+1.68%，复利 +978%->+758%。
   时间止损对趋势系统有害，因为它恰好在趋势最慢的那一段把人赶下车。

## 口径（必须和回测逐字一致）

    上轨(N) = max(high[i-N : i])      不含第 i 根自己
    下轨(M) = min(low[i-M : i])       不含第 i 根自己
    进场     = close[i] > 上轨(N)     且当前空仓
    出场     = close[i] < 下轨(M)     且当前持多

回测里信号在 i 根收盘产生、i+1 根开盘成交。线上靠"只用已收盘 K"实现同一时序。
"""
from __future__ import annotations

import json
import logging
import time
import urllib.parse
import urllib.request
from typing import List, Optional

logger = logging.getLogger(__name__)


class DonchianStrategy:
    """1h 唐奇安通道，只做多。

    状态机：
      - 收盘突破 N 根新高 -> 开多
      - 收盘跌破 M 根新低 -> 平多
      - 中间不管波动（这就是它能在 32% 回撤下活下来的原因）
    """

    def __init__(self, cfg, symbol: str = "BTCUSDT",
                 fapi_base: str = "https://fapi.binance.com",
                 proxy: str = "", exec_market: str = ""):
        self.cfg = cfg
        self.symbol = symbol
        self.fapi = fapi_base.rstrip("/")
        self.proxy = proxy
        self.exec_market = (exec_market or "").lower()
        if not self.exec_market:
            self.exec_market = "spot" if str(
                getattr(cfg, "exec_market", "") or "").lower() == "spot" else "perp"
        # 通道回看长度。默认值必须是被验证过的那一组，不能是"也还行"的邻居。
        self.entry_n = int(getattr(cfg, "donchian_entry", 0) or 150)
        self.exit_m = int(getattr(cfg, "donchian_exit", 0) or 75)
        # 两个通道宽度必须 self.exit_m < self.entry_n，否则会出现
        # "刚进场就被同一根判出场"的自相矛盾状态。
        if self.exit_m >= self.entry_n:
            logger.warning("唐奇安出场通道 %d >= 进场通道 %d，退回默认 75/150",
                           self.exit_m, self.entry_n)
            self.entry_n, self.exit_m = 150, 75
        # 主周期 1h。这是回测验证过的周期，不是随口挑的：
        # 同样的唐奇安在 5m/15m 上扣成本后全负，在 4h 上笔数太少。
        self.iv = int(getattr(cfg, "donchian_interval_ms", 0) or 3600000)
        self.bars: List[dict] = []             # {t,o,h,l,c}
        self.last_eval_bar = 0
        self.seeded = False
        self.need_reconcile = True             # 启动时做一次状态对账
        # 滞回：唐奇安的"突破"本身已经是滞回（要创 N 根新高才进），
        # 不需要再套 SMA 那套百分比滞回带，套上会变成另一个没验证过的策略。
        self.hyst = 0.0

    # ---------- 数据 ----------
    def seed_from_rest(self, need: int = 400) -> int:
        """从币安拉历史 1h K 线填满窗口。

        没有这一步，进程刚起来时通道要 150 根 1h（6.25 天）数据，
        期间策略空转。虽然比 SMA200 的 33 天短得多，仍然必须补。
        """
        need = max(int(need), self.entry_n + 30)
        start = int(time.time() * 1000) - need * self.iv
        # 补历史必须和【实时行情流】用同一个市场（fapi 永续），
        # 与 4h 策略同一条不变量，由 tests/test_seed_venue_matches_feed.py 钉住。
        api_base = self.fapi
        opener = (urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": self.proxy, "https": self.proxy}))
            if self.proxy else urllib.request.build_opener())
        got: List[dict] = []
        cur = start
        iv_name = self.interval_name()
        while len(got) < need:
            q = urllib.parse.urlencode({"symbol": self.symbol, "interval": iv_name,
                                        "startTime": cur, "limit": 500})
            req = urllib.request.Request(api_base + "/fapi/v1/klines?" + q,
                                         headers={"User-Agent": "ai-trader/1.0"})
            try:
                with opener.open(req, timeout=20) as r:
                    rows = json.loads(r.read())
            except Exception as e:
                logger.warning("唐奇安策略补历史失败：%s", e)
                break
            if not rows:
                break
            for x in rows:
                got.append({"t": int(x[0]), "o": float(x[1]), "h": float(x[2]),
                            "l": float(x[3]), "c": float(x[4]), "v": float(x[5])})
            cur = int(rows[-1][0]) + self.iv
            if len(rows) < 500:
                break
        # 去掉正在形成的那根未收盘 K（用 close_time 判断，比 now 更准）
        now_ms = int(time.time() * 1000)
        got = [b for b in got if b["t"] + self.iv <= now_ms]
        self.bars = got[-need:]
        self.seeded = bool(self.bars)
        # last_eval_bar 必须跟着补上，否则重启后它是 0，
        # 而熔断防重入的闸门写的是
        #   if self.last_eval_bar and self.last_eval_bar <= block_bar
        # 0 为假 -> 整个闸门被跳过 -> "熔断->立即再进->再熔断"在重启后敞开。
        if self.bars:
            self.last_eval_bar = int(self.bars[-1]["t"])
        logger.info("唐奇安策略已补 %d 根 %s K 线（覆盖 %.1f 天）",
                    len(self.bars), iv_name, len(self.bars) * self.iv / 86400000)
        return len(self.bars)

    def interval_name(self) -> str:
        iv = self.iv
        for ms, nm in ((3600000, "1h"), (7200000, "2h"), (14400000, "4h"),
                       (900000, "15m"), (1800000, "30m")):
            if iv == ms:
                return nm
        return "1h"

    def on_bar(self, bar) -> bool:
        """喂一根已收盘的 K。返回是否产生了新的判断点。"""
        t = getattr(bar, "open_time", None)
        if t is None:
            return False
        if int(t) + self.iv > int(time.time() * 1000):
            return False                      # 还没收盘，不能当已实现信号用
        if self.bars and t <= self.bars[-1]["t"]:
            return False
        # 字段名必须是 Bar 的真实属性：是 volume 不是 vol。
        # 写错会每 3 秒抛一次 AttributeError，把整个决策步骤打断——
        # 系统看起来在跑，实际上从未真正评估过。
        self.bars.append({"t": int(t), "o": float(bar.open), "h": float(bar.high),
                          "l": float(bar.low), "c": float(bar.close),
                          "v": float(getattr(bar, "volume", 0.0) or 0.0)})
        if len(self.bars) > 1000:
            self.bars = self.bars[-1000:]
        self.last_eval_bar = int(t)
        return True

    # ---------- 官方 K 线（保证线上喂进去的和回测同源同构造）----------
    def bar_from_kline(self, raw):
        """把交易所 kline 数组变成策略认识的 bar，不可用则返回 None。"""
        try:
            if not raw or len(raw) < 6:
                return None
            t = int(raw[0])
            o = float(raw[1]); h = float(raw[2]); l = float(raw[3])
            c = float(raw[4]); v = float(raw[5])
            close_time = int(raw[6]) if len(raw) > 6 else t + self.iv - 1
        except (TypeError, ValueError, IndexError):
            return None
        if close_time >= int(time.time() * 1000):
            return None
        if not (o > 0 and h > 0 and l > 0 and c > 0):
            return None
        if l > min(o, c) or h < max(o, c):
            return None

        class _KBar:
            __slots__ = ("open_time", "open", "high", "low", "close", "volume")

            def __init__(self, *a):
                (self.open_time, self.open, self.high,
                 self.low, self.close, self.volume) = a

        return _KBar(t, o, h, l, c, v)

    def fetch_official_bar(self, open_time: int):
        """取某一根已收盘 K 的官方数据。失败返回 None（调用方退回本地 bar）。"""
        opener = (urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": self.proxy, "https": self.proxy}))
            if self.proxy else urllib.request.build_opener())
        q = urllib.parse.urlencode({"symbol": self.symbol,
                                    "interval": self.interval_name(),
                                    "startTime": int(open_time), "limit": 1})
        req = urllib.request.Request(self.fapi + "/fapi/v1/klines?" + q,
                                     headers={"User-Agent": "ai-trader/1.0"})
        try:
            with opener.open(req, timeout=15) as r:
                rows = json.loads(r.read())
        except Exception as e:
            logger.warning("取官方 %s K 线失败：%s", self.interval_name(), e)
            return None
        if not rows:
            return None
        return self.bar_from_kline(rows[0])

    # ---------- 状态 ----------
    def _hlc(self):
        return ([b["h"] for b in self.bars], [b["l"] for b in self.bars],
                [b["c"] for b in self.bars])

    def state(self, price: float = 0.0) -> dict:
        """计算当前状态。**必须和回测逐字一致**，否则上线的是另一个策略。

        回测里的定义：
            up(i)   = max(high[i-N : i])      <- 不含第 i 根自己
            dn(i)   = min(low[i-M : i])
            entry   = close[i] > up(i)
            exit    = close[i] < dn(i)
        """
        n, m = self.entry_n, self.exit_m
        hs, ls, cl = self._hlc()
        px = float(price) if price else (cl[-1] if cl else 0.0)
        base = {"bars": len(cl), "entry_n": n, "exit_m": m, "price": px,
                "upper": None, "lower": None, "upper_prev": None,
                "lower_prev": None, "ready": False}
        # 需要 max(n,m)+2 根：判定那根的窗口要 n 根，还要有上一根做"新鲜突破"判断
        if len(cl) < n + 2:
            return base
        up_now = max(hs[-(n + 1):-1])          # 不含最后一根
        dn_now = min(ls[-(m + 1):-1])
        up_prev = max(hs[-(n + 2):-2])
        dn_prev = min(ls[-(m + 2):-2])
        above_up = cl[-1] > up_now
        below_dn = cl[-1] < dn_now
        above_up_prev = cl[-2] > up_prev
        below_dn_prev = cl[-2] < dn_prev
        base.update({"upper": up_now, "lower": dn_now,
                     "upper_prev": up_prev, "lower_prev": dn_prev,
                     "above_upper": above_up, "below_lower": below_dn,
                     "above_upper_prev": above_up_prev,
                     "below_lower_prev": below_dn_prev,
                     "entry_line": up_now, "exit_line": dn_now,
                     "dist_to_entry_pct": (up_now - px) / px * 100.0 if px else 0.0,
                     "ready": True})
        return base

    def want_entry(self, price: float) -> Optional[dict]:
        st = self.state(price)
        if not st["ready"]:
            return None
        # 进场条件就是"突破"这个事件：最新一根收盘创 N 根新高、上一根没有。
        # 用事件而不是状态，是为了和回测的 i/i+1 时序对齐。
        if st["above_upper"] and not st["above_upper_prev"]:
            return {"ok": True, "name": "donchian_up",
                    "reason": "%s 收盘 %.1f 突破 %d 根新高 %.1f（唐奇安只做多）"
                              % (self.interval_name(), st["price"],
                                 st["entry_n"], st["upper"]),
                    "upper": st["upper"], "state": st}
        return None

    def startup_entry(self, price: float) -> Optional[dict]:
        """启动对账：已经站在通道上方却没持仓，就直接补进。

        为什么必须做：want_entry 认的是"突破"这个事件。进程如果在上涨途中重启，
        空仓状态下前一根也在上方，交叉条件永远不成立，就会一路空仓错过整段趋势。
        这正是"回测赚钱、实盘没上车"的典型断裂。
        """
        if not self.need_reconcile:
            return None
        st = self.state(price)
        if not st["ready"]:
            return None                    # 数据还不够，下次再试
        # 关键：不能在这里就把标志清掉。
        # 站在通道下方 -> 本来就该空仓，对账确实完成了；
        # 站在通道上方 -> 这笔补仓还欠着，要留到【真的开成】为止。
        if not st["above_upper"]:
            self.need_reconcile = False
            return None
        return {"ok": True, "name": "donchian_reconcile",
                "reason": "启动对账：现价 %.1f 已突破 %d 根新高 %.1f（超 %.2f%%），补开多仓"
                          % (st["price"], st["entry_n"], st["upper"],
                             (st["price"] - st["upper"]) / st["upper"] * 100),
                "upper": st["upper"], "state": st}

    def mark_reconciled(self) -> None:
        """持仓真的建起来之后调用：对账这才算完成。"""
        self.need_reconcile = False

    def want_exit(self, price: float) -> Optional[dict]:
        """持仓期间，只要【最后一根已收盘 K】跌破 M 根新低就该平。

        ★ 和 4h 策略同一条陷阱，这里提前避开：
        出场条件【不能】要求 prev 还在上方（"新鲜跌破"）。
        那样一旦进程在跌破之后重启而持仓还在，条件永远不成立，
        仓位只能靠硬熔断出场——而唐奇安版没有硬停损，等于永远不出场。

        不要用 prev 条件之后：
          - 连续运行时行为相同（首次跌破那根就出场）；
          - 重启后若最后一根已在下方，立刻补出场；
          - 平仓单失败时，下一跳还会再给信号（可重试），不会卡死。
        """
        st = self.state(price)
        if not st["ready"]:
            return None
        if not st["below_lower"]:
            return None
        # ★ 分支方向很容易写反：「上一根已经在下轨下方」说明跌破发生在
        # 【更早】的 K 线上，是本进程没赶上的那次——那才是"补出场"。
        # 反过来的话，每一次正常出场都会被标成补出场，
        # 事后复盘再也分不清"按计划平的"和"是补平的"。
        # tests/test_donchian_strategy.py 用两种输入分别钉住了这两个分支。
        if st["below_lower_prev"]:
            why = ("%s 收盘 %.1f 已在 %d 根新低 %.1f 下方（通道出场，平多；"
                   "跌破发生在之前的 K 线，此处补出场）"
                   % (self.interval_name(), st["price"], st["exit_m"], st["lower"]))
        else:
            why = ("%s 收盘 %.1f 跌破 %d 根新低 %.1f（通道出场，平多）"
                   % (self.interval_name(), st["price"], st["exit_m"], st["lower"]))
        return {"ok": True, "name": "donchian_down", "reason": why,
                "lower": st["lower"], "state": st}

    def roll_forward_to_break(self, price: float, max_bars: int = 2000) -> dict:
        """通道自己在动：价格不动的话，还要几根 K 才会上破上轨？

        通道滚动时，被挤出去的老 K 如果是高的，上轨就自己往下走。
        对大屏的意义：用户会盯着一个"还需涨 X%"的数字，以为在等上涨，
        实际上可能在等上轨下来。两种状态的"还要多久"完全不同。
        """
        n = self.entry_n
        hs, ls, cl = self._hlc()
        if len(cl) < n + 1 or price <= 0:
            return {}
        px = float(price)
        window = [float(x) for x in hs[-(n + 1):-1]]
        for k in range(1, max_bars + 1):
            window.pop(0)
            window.append(px)                 # 冻结现价
            if px > max(window):
                return {"roll_bars": k,
                        "roll_days": round(k * self.iv / 86400000.0, 1)}
        return {"roll_bars": None, "roll_days": None}

    def next_bar_eta_h(self) -> float:
        """距离"下一根收盘"还有几小时。"""
        if not self.bars:
            return 0.0
        nxt = self.bars[-1]["t"] + 2 * self.iv
        return max(0.0, (nxt - time.time() * 1000) / 3600000.0)
