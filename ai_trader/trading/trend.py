# -*- coding: utf-8 -*-
"""趋势跟随（只做多）：4h SMA 过滤。

为什么是它（2026-11-01 回测结论）：
- 1m/3s 尺度的一切信号在扣掉 0.11% 往返成本后都是负期望，已在 120 天 1m 数据上
  逐个证伪（预测力显著为负、样本外不成立、利润全靠最极端的 5%）。
- 只做多 + 4h SMA200 过滤，在 6 年数据上：总收益 +666%，最大回撤 30-47%，
  买入持有是 +503% / 77%。样本外（后 3 年）+106%，回撤 18%。
  参数 140-280 全部为正，不是单一幸运参数。
- 它赚的是"长期上行漂移 + 躲开深熊"，而且交易频率极低（6 年约 180 次），
  费率占名义的部分可以忽略——这正好绕开了杀死 k-trader 的那个成本结构。

风险声明（写在代码里，防止以后自己骗自己）：
- 这个策略本质是"多头 beta + 趋势过滤"，不是市场中性。BTC 长期不走趋势时它会亏。
- 2022 年（熊市）它亏 20%，虽然同期买入持有亏 65%。
- 它不做空，所以在下跌市里是空仓吃灰，不是赚钱。
"""
from __future__ import annotations

import json
import logging
import time
import urllib.parse
import urllib.request
from typing import List, Optional

logger = logging.getLogger(__name__)


# 这里原本有个 _sma() 辅助函数，从未被调用，而 state() 里还内联了一份
# 口径略有不同的 SMA 计算。两份并存是 bug 温床：改了一个另一个不会同步，
# 而回测只验证了内联那一份。删掉死代码，只保留经过验证的实现。


class TrendStrategy:
    """4h 只做多趋势跟随。

    状态机很简单：
      - 收盘价上穿 SMA -> 开多
      - 收盘价下穿 SMA -> 平多
      - 中间不管波动（这就是它能在 47% 回撤下活下来的原因：不设紧止损）
    """

    def __init__(self, cfg, symbol: str = "BTCUSDT",
                 fapi_base: str = "https://fapi.binance.com",
                 proxy: str = "", exec_market: str = ""):
        self.cfg = cfg
        self.symbol = symbol
        self.fapi = fapi_base.rstrip("/")
        self.proxy = proxy
        # 用【哪个市场】的 4h K 线做决策，必须和回测一致。
        #
        # 回测与信号一致性验证用的是【现货】K 线（api.binance.com/api/v3）。
        # 而线上一直用【永续合约】K 线（fapi）。实测差异很小但确实存在：
        #   2000 根 4h（333 天）：above 判定分歧 2 处（0.11%）
        #   全部落在"价格离 SMA200 < 0.1%"的临界带里（20 根中 2 根）
        # 离均线超过 0.1% 时从无分歧。
        #
        # 影响有限（最多把入场挪一根，而延迟 1 根的代价在回测里几乎为零，
        # 延迟 2 根才开始明显变差），但这是"回测验证的世界"和
        # "线上实际决策的世界"不是同一个——这种偏差没必要留着。
        self.exec_market = (exec_market or "").lower()
        if not self.exec_market:
            self.exec_market = "spot" if str(
                getattr(cfg, "exec_market", "") or "").lower() == "spot" else "perp"
        # 滞回带半宽（小数，不是百分数）。0 = 现行规则，行为与历史完全一致。
        # 它只挪动"触发线相对均线的位置"，不改均线、不改信号判定。
        self.hyst = max(0.0, float(getattr(cfg, "trend_hysteresis_pct", 0.0) or 0.0)) / 100.0
        self.iv = 14400000                     # 4h，趋势策略的主周期
        self.bars: List[dict] = []             # {t,o,h,l,c}
        self.last_eval_bar = 0
        self.seeded = False
        self.need_reconcile = True     # 启动时做一次状态对账

    # ---------- 数据 ----------
    def seed_from_rest(self, need: int = 420) -> int:
        """从币安拉历史 4h K 线填满窗口。

        没有这一步，进程刚起来时 SMA200 需要 33 天数据，策略会一直空转。
        """
        need = max(int(need), int(self.cfg.sma_lookback) + 30)
        start = int(time.time() * 1000) - need * self.iv
        # 补历史必须和【实时行情流】用同一个市场，而不是和下单市场一致。
        #
        # 这里曾经写成「exec_market=spot 就取现货 K 线」，听起来很合理
        # （决策市场和下单市场一致），实际上只修了一半：
        # 实时行情流在 feed.py 里写死了 cfg.market.fapi_base = 永续合约，
        # 于是策略的输入变成【现货历史 + 合约实时】的混合序列，
        # 而这段注释声称的一致性并不存在。
        #
        # 现在统一走 fapi（与实时流同源）。为什么不反过来把行情流改成现货：
        # 行情流是 7x24 的命脉，改它风险大；而两个市场的信号质量实测几乎相同——
        # 取 2023-11 ~ 2026-11 共 6571 根 4h，同一条 SMA200 规则：
        #     现货数据 CAGR 23.23% / 回撤 28.01% / 59 笔
        #     合约数据 CAGR 24.26% / 回撤 27.41% / 57 笔
        # 差 1 个百分点，在估计噪声之内。合约盘口更厚，收盘价噪声更小，
        # 用合约数据做信号是站得住的；关键是【种子和实时流必须同源】。
        # 这条不变量由 tests/test_seed_venue_matches_feed.py 钉住。
        api_base = self.fapi
        kline_path = "/fapi/v1/klines"
        opener = (urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": self.proxy, "https": self.proxy}))
            if self.proxy else urllib.request.build_opener())
        got: List[dict] = []
        cur = start
        while len(got) < need:
            q = urllib.parse.urlencode({"symbol": self.symbol, "interval": "4h",
                                        "startTime": cur, "limit": 500})
            req = urllib.request.Request(api_base + kline_path + "?" + q,
                                         headers={"User-Agent": "ai-trader/1.0"})
            try:
                with opener.open(req, timeout=20) as r:
                    rows = json.loads(r.read())
            except Exception as e:
                logger.warning("趋势策略补历史失败：%s", e)
                break
            if not rows:
                break
            for x in rows:
                got.append({"t": int(x[0]), "o": float(x[1]), "h": float(x[2]),
                            "l": float(x[3]), "c": float(x[4]), "v": float(x[5])})
            cur = int(rows[-1][0]) + self.iv
            if len(rows) < 500:
                break
        # 去掉正在形成的那根未收盘 K
        now_ms = int(time.time() * 1000)
        got = [b for b in got if b["t"] + self.iv <= now_ms]
        self.bars = got[-need:]
        self.seeded = bool(self.bars)
        # last_eval_bar 必须跟着补上，否则重启后它是 0。
        #
        # 为什么这条是风控而不是整洁问题：熔断防重入的闸门写的是
        #   if self.trend.last_eval_bar and self.trend.last_eval_bar <= self._trend_block_bar
        # 0 为假 -> 整个闸门被跳过。于是"熔断 -> 立即再进 -> 再熔断"的死循环
        # 在【重启后的第一根 4h 内】完全敞开——而这一段恰恰是最常发生的场景：
        # 行情剧烈波动时哨兵重启，此时价格可能远高于 SMA200（强势上涨后回调 25%），
        # startup_entry 看到"还在均线上方"就立刻把仓补回去，再跌再补。
        # 每次 25%，两轮就把 100U 打成 56U。
        #
        # 补上它等于声明"我已经评估到最后一根已收盘 K"，语义与 on_bar() 一致。
        if self.bars:
            self.last_eval_bar = int(self.bars[-1]["t"])
        logger.info("趋势策略已补 %d 根 4h K 线（覆盖 %.1f 天）",
                    len(self.bars), len(self.bars) * self.iv / 86400000)
        return len(self.bars)

    def on_bar(self, bar) -> bool:
        """喂一根已收盘的 4h K。返回是否产生了新的判断点。"""
        t = getattr(bar, "open_time", None)
        if t is None:
            return False
        if int(t) + self.iv > int(time.time() * 1000):
            return False                      # 还没收盘，不能当已实现信号用
        if self.bars and t <= self.bars[-1]["t"]:
            return False
        # 字段名必须是 Bar 的真实属性：是 volume 不是 vol。
        # 写错会每 3 秒抛一次 AttributeError，把整个决策步骤打断——
        # 系统看起来在跑（日志有输出、看板有数据），实际上从未真正评估过。
        self.bars.append({"t": int(t), "o": float(bar.open), "h": float(bar.high),
                          "l": float(bar.low), "c": float(bar.close),
                          "v": float(getattr(bar, "volume", 0.0) or 0.0)})
        if len(self.bars) > 1000:
            self.bars = self.bars[-1000:]
        self.last_eval_bar = int(t)
        return True

    # ---------- 官方 K 线（保证线上喂进去的和回测同源同构造）----------
    def bar_from_kline(self, raw):
        """把交易所 kline 数组变成策略认识的 bar，不可用则返回 None。

        为什么需要它：回测用官方 4h K 线，线上原本用本地逐笔聚合出的 bar。
        2026-11-02 实测两者收盘价一致、但成交量只有官方的 0~7%
        （聚合器每次重启从零开始，一个 4h 桶只攒到本次运行期间的成交）。
        更危险的是收盘瞬间数据源不活跃时，本地 close 会是
        "我们最后一次收到的成交价"而不是官方收盘价——
        第 81 轮就实测到 WS 全程静默，那种情况下本地 close 是不可信的。

        宁可多一次 REST 请求（一天 6 次），也要让线上判断建立在与回测
        完全相同的输入上。
        """
        try:
            if not raw or len(raw) < 6:
                return None
            t = int(raw[0])
            o = float(raw[1]); h = float(raw[2]); l = float(raw[3])
            c = float(raw[4]); v = float(raw[5])
            close_time = int(raw[6]) if len(raw) > 6 else t + self.iv - 1
        except (TypeError, ValueError, IndexError):
            return None
        # 未收盘的 K 不能当已实现信号用
        if close_time >= int(time.time() * 1000):
            return None
        # 0 价/负价一律拒绝（第 81 轮那类畸形数据的防线）
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
        """取某一根已收盘 4h K 的官方数据。失败返回 None（调用方退回本地 bar）。

        同步实现，调用方需放到线程里跑，不能阻塞事件循环。
        """
        opener = (urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": self.proxy, "https": self.proxy}))
            if self.proxy else urllib.request.build_opener())
        q = urllib.parse.urlencode({"symbol": self.symbol, "interval": "4h",
                                    "startTime": int(open_time), "limit": 1})
        req = urllib.request.Request(self.fapi + "/fapi/v1/klines?" + q,
                                     headers={"User-Agent": "ai-trader/1.0"})
        try:
            with opener.open(req, timeout=15) as r:
                rows = json.loads(r.read())
        except Exception as e:
            logger.warning("取官方 4h K 线失败：%s", e)
            return None
        if not rows:
            return None
        bar = self.bar_from_kline(rows[0])
        if bar is None:
            return None
        if int(bar.open_time) != int(open_time):
            return None
        return bar

    # ---------- 判断 ----------
    def _closes(self) -> List[float]:
        return [b["c"] for b in self.bars]

    def state(self, price: float = 0.0) -> dict:
        """计算当前状态。**必须和回测逐字一致**，否则上线的是另一个策略。

        回测里的定义（tools/long_only.py 的 sma_at）：
            sma_at(i, n) = mean(C[i-n : i])      <- 不含第 i 根自己
            above(i)     = C[i] > sma_at(i, n)
        这里照抄：SMA 不含被判定的那根，用它的收盘价去比。
        """
        n = int(self.cfg.sma_lookback)
        cl = self._closes()
        px = float(price) if price else (cl[-1] if cl else 0.0)
        if len(cl) < n + 1:
            return {"bars": len(cl), "sma": None, "prev_sma": None, "price": px,
                    "above": False, "prev_above": None, "ready": False, "lookback": n}
        s_now = sum(cl[-(n + 1):-1]) / n            # 不含最后一根
        s_prev = sum(cl[-(n + 2):-2]) / n if len(cl) >= n + 2 else None
        # above 永远是【未经滞回修饰】的真实关系：现价到底在均线上方还是下方。
        # 看板、roll_forward_to_cross、日志都用它说话，不能被滞回污染。
        # 带滞回的是另外两个谓词，只在"该不该进/该不该出"的地方用：
        #   enter_above = 收盘 > SMA*(1+h)   进场的抬高档
        #   exit_above  = 收盘 > SMA*(1-h)   出场的压低档
        # h=0 时三者完全相同，所以默认配置下逐字等价于旧行为。
        above = cl[-1] > s_now
        prev_above = (cl[-2] > s_prev) if s_prev is not None else None
        h = self.hyst
        if h > 0:
            enter_above = cl[-1] > s_now * (1.0 + h)
            enter_prev = (cl[-2] > s_prev * (1.0 + h)) if s_prev is not None else None
            exit_above = cl[-1] > s_now * (1.0 - h)
            entry_line = s_now * (1.0 + h)
            exit_line = s_now * (1.0 - h)
        else:
            enter_above, enter_prev = above, prev_above
            exit_above = above
            entry_line = exit_line = s_now
        return {"bars": len(cl), "sma": s_now, "prev_sma": s_prev, "price": px,
                "above": above, "prev_above": prev_above,
                "enter_above": enter_above, "enter_prev_above": enter_prev,
                "exit_above": exit_above,
                "entry_line": entry_line, "exit_line": exit_line,
                "hyst_pct": h * 100.0,
                "ready": True, "lookback": n}

    def want_entry(self, price: float) -> Optional[dict]:
        st = self.state(price)
        if not st["ready"] or st["prev_above"] is None:
            return None
        # 进场条件就是"上穿"这个事件：最新一根在上方、上一根在下方。
        #
        # 曾试过加"连续 N 根确认"（whipsaw 过滤器），实测结论是【不采纳】：
        # 聚合看很漂亮（全历史 CAGR 44.4%->48.0%，2020 起回撤 47.8%->41.5%），
        # 但按三个独立时代拆开就散了——
        #   2018-2020: 收益改善、回撤变差(57.2%->59.7%)
        #   2021-2023: 收益改善、回撤变差(39.6%->41.5%)
        #   2024-2026: 收益变差、回撤改善
        # "同时改善收益与回撤"的时代数是 0/3。
        # 聚合改善来自复利放大，不是每段都真的更好。
        # 用带滞回的进场谓词：h=0 时 enter_above 就是 above，行为不变。
        if st["enter_above"] and not st["enter_prev_above"]:
            # name 字段是 _open() 和决策日志的必需契约。
            # 2026-11-01 事故：这里漏了 name，导致"上穿"这条常规开仓路径
            # 在第一次触发时抛 KeyError，被上层 except 吞成"决策步骤异常"——
            # 表现和 bar.vol 那个 bug 一模一样：策略永不建仓，但系统看起来在跑。
            return {"ok": True, "name": "trend_up",
                    "reason": "4h 收盘 %.1f 上穿 SMA%d %.1f（只做多趋势）"
                              % (st["price"], st["lookback"], st["sma"]),
                    "sma": st["sma"], "state": st}
        return None

    def startup_entry(self, price: float) -> Optional[dict]:
        """启动对账：已经站在 SMA 上方却没持仓，就直接补进。

        为什么必须做：want_entry 认的是"上穿"这个事件。进程如果在上涨途中重启，
        空仓状态下前一根也在上方，交叉条件永远不成立，就会一路空仓错过整段趋势。
        这正是"回测赚钱、实盘没上车"的典型断裂，必须在第一次评估时补掉。
        """
        if not self.need_reconcile:
            return None
        st = self.state(price)
        if not st["ready"]:
            return None                    # 数据还不够，下次再试
        # 关键：不能在这里就把标志清掉。
        # 站在均线下方 -> 本来就该空仓，对账确实完成了；
        # 站在均线上方 -> 这笔补仓还欠着，要留到【真的开成】为止。
        # 否则只要第一次评估时开不成（资金不足、闸门拦截、下单失败），
        # 对账机会就被永久消费，资金到位后也不会补——只能等下一次上穿。
        # 对账补仓用【进场】谓词：如果现价还在滞回带里（SMA 上方但没超过
        # SMA*(1+h)），按规则此刻本就不该有仓，那就不是"欠着没补"，
        # 而是"本来就不该持有"。用原版 above 会把它错误地补进去。
        if not st["enter_above"]:
            self.need_reconcile = False
            return None
        return {"ok": True, "name": "trend_reconcile",
                "reason": "启动对账：现价 %.1f 已在 SMA%d %.1f 上方（距 %.2f%%），补开多仓"
                          % (st["price"], st["lookback"], st["sma"],
                             (st["price"] - st["sma"]) / st["sma"] * 100),
                "sma": st["sma"], "state": st}

    def mark_reconciled(self) -> None:
        """持仓真的建起来之后调用：对账这才算完成。"""
        self.need_reconcile = False

    def want_exit(self, price: float) -> Optional[dict]:
        """持仓期间，只要【最后一根已收盘 4h】在 SMA 下方就该平。

        ★ 这里曾经多写了一个条件：必须 prev_above=True（新鲜下穿）。
        那是个真实缺陷，不是"更保守"：

          1. 它和回测不一致。回测出场条件是 C[i] < sma(i)，
             只要收盘跌破就出，没有"上一根还在上方"这个要求。

          2. 更致命的是"重启断裂"：
             进程如果在下穿【之后】重启、而持仓还在，
             prev_above 已经是 False，本函数从此永远返回 None。
             这笔持仓唯一的出路只剩 25% 熔断止损，
             而历史最深浮亏只有 -11.3%，熔断永远碰不到——
             于是仓位会一路持有，穿过整个下跌趋势。

          3. 同一类问题在【进场】侧已经修过（startup_entry）：
             "进程如果在上涨途中重启，交叉条件永远不成立，
              就会一路空仓错过整段趋势。"
             出场侧当时没做镜像处理，是因为连续跑的时候两边恰好一致，
             所以测试和实盘都没暴露——只有重启才暴露。

        去掉 prev_above 之后：
          - 连续运行时，行为与原来完全相同（首次跌破那根就出场）；
          - 重启后若最后一根已在下方，立刻补出场（修复断裂）；
          - 平仓单失败时，下一跳还会再给信号（可重试），不会卡死。
        """
        st = self.state(price)
        if not st["ready"]:
            return None
        # 出场用【压低档】：h=0 时 exit_above 就是 above，行为不变。
        # 有滞回时，价格必须真正跌穿 SMA*(1-h) 才算趋势结束，
        # 否则在均线附近来回擦就会反复进出、来回付手续费。
        if not st["exit_above"]:
            # 两种措辞要分开，因为它们代表不同的处境：
            #   新鲜下穿  -> 正常趋势结束
            #   早已在下方 -> 重启补出场（交叉发生在进程不在的时候）
            # 混成一句话，事后查日志就分不清"是按计划平的"还是"是补平的"。
            if st["prev_above"]:
                why = ("4h 收盘 %.1f 下穿 SMA%d %.1f（趋势结束，平多）"
                       % (st["price"], st["lookback"], st["sma"]))
            else:
                why = ("4h 收盘 %.1f 已在 SMA%d %.1f 下方（趋势结束，平多；"
                       "交叉发生在之前的 K 线，此处补出场）"
                       % (st["price"], st["lookback"], st["sma"]))
            return {"ok": True, "name": "trend_down",
                    "reason": why, "sma": st["sma"], "state": st}
        return None

    def roll_forward_to_cross(self, price: float, max_bars: int = 2000) -> dict:
        """均线自己在动：价格不动的话，还要几根 4h 才会上穿？

        为什么必须算：看板（和决策日志）写的是
            「开多触发线：涨到 82349（还需 +5.5%）并收盘确认」
        这句话把 SMA200 当成静止的门槛。它不是。
        SMA200 每过一根 4h 就把最老的一根挤出去——被挤出去的那根
        比现价高，均线就自己往下走。

        实测（2026-11-01，价 77719，SMA 82337，差 -5.61%）：
          价格完全不动，186 根 4h（约 31 天）后现价就会高于均线。
        也就是说「还需涨 5.5%」把门槛说高了：那次入场根本不需要涨价。

        对用户的实际影响：他会盯着一个"永远差 5.5%"的数字，
        以为系统在等一个可能永远不来的上涨，而实际上系统在等均线下来。
        这两种状态的"还要多久"完全不同，处置也不同。

        口径：冻结现价、逐根滚动。这是中性假设，不是预测——
        价格跌一点会让上穿更早（均线下来得更快），涨一点会更快。
        """
        n = int(self.cfg.sma_lookback)
        cl = self._closes()
        if len(cl) < n + 1 or price <= 0:
            return {}
        # 口径必须和 state() 逐字一致，否则算出来的天数属于另一个策略。
        #
        # state() 的窗口是 cl[-(n+1):-1]：n 个收盘价，且【不含被判定的那根】。
        # 冻结现价再补 1 根之后，新序列是 cl + [px]，其 state 窗口
        # = cl[-(n-1):] + [px]，也就是把当前窗口左移一格、尾部补现价。
        # 所以 "roll_bars = k" 的语义是：再过 k 根 4h，价格仍是现价时就会上穿。
        px = float(price)
        window = [float(x) for x in cl[-(n + 1):-1]]
        acc = sum(window)
        # ★ 触发线不能用裸均线。有滞回时，进场条件是 收盘 > SMA*(1+h)，
        # 比裸均线更晚。第六十六轮踩过一模一样的坑：看板当时把 SMA200
        # 说成静止门槛，而它每根都在滚动。现在若还按裸均线报天数，
        # 报的就是一个不会触发进场的时刻——等于把"还要等多久"说短了。
        up = 1.0 + self.hyst
        for k in range(1, max_bars + 1):
            acc += px - window[0]
            window.pop(0)
            window.append(px)                 # 冻结现价
            if px > (acc / n) * up:           # 严格高于，与 enter_above 一致
                return {"roll_bars": k,
                        "roll_days": round(k * self.iv / 86400000.0, 1)}
            if all(w == px for w in window):
                break                         # 窗口已全是现价，再滚也不会变
        return {"roll_bars": None, "roll_days": None}

    def next_bar_eta_h(self) -> float:
        """距离"下一根 4h 收盘"还有几小时。

        bars[-1] 是最后一根**已收盘**的 K，它占 [t, t+iv)。
        正在形成的那根是 t+iv..t+2iv，所以下一次判断点在 t+2iv。
        """
        if not self.bars:
            return 0.0
        nxt = self.bars[-1]["t"] + 2 * self.iv
        return max(0.0, (nxt - time.time() * 1000) / 3600000.0)
