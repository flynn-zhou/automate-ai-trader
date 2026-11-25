# -*- coding: utf-8 -*-
"""真实持仓回合：从交易所的逐笔成交还原「一单」，并给入场/出场打分。

为什么要单独建一层
==================
`trades` 表只记「引擎经手过的」仓。用户手工开的仓，引擎根本不知道
它是什么时候开的、为什么开、打算拿到哪里；只有在对账发现
「交易所有仓、库里没有」的时候才补一条接管记录，而那时开仓已经过去了。

生产实证（11-11 09:04 → 11-12 22:57，同一段区间）：

    交易所 /fapi/v1/userTrades   25 笔成交
        实现盈亏 +109.80  手续费 -16.92  资金费 -0.30   →  净 ≈ +92.59
    trades 表                    4 条记录             →  净 -20.46

差 113U。所以这一层的数据来源只有交易所，**不读 trades、也不和它合并**。
两套账各自独立，对不上才看得见差异；合并掉差异就没了。

「一单」怎么定义
================
一张单 = 一次「从空仓到空仓」。仓位回到 0 就收一个回合。
中间加仓、部分平仓都算同一单——因为它们共享同一个入场决策，
分开记会把一笔仓拆成好几笔「交易」，胜率和平均持仓时间全失真。

反手怎么处理
------------
一笔单把多头直接打成空头（或反过来）时，币安返回的 realizedPnl
**只覆盖被平掉的那部分**，剩下的开的是反向新仓。所以这种单要拆成
两段：平仓段归上一个回合，开仓段起一个新回合，手续费按数量比例摊。
不拆的话，新回合的入场价里会混进平仓量，成本率算出来是错的。
"""
from __future__ import annotations

import logging
from typing import Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

EPS = 1e-9
MS_PER_DAY = 86_400_000

#: 引擎写 trades.entry_ts 时用的是「它自己那一拍」的时间，
#: 而交易所成交时间是撮合时间，两者天然差几秒。精确匹配永远匹配不上，
#: 于是所有仓都会被标成 manual —— 那等于放弃了「谁开的」这个信息。
#: 给一个容差，并在容差内取最近的那一条。
TRADE_MATCH_TOL_MS = 180_000

#: 拉不到 K 线时，多久之内还值得重试。
#: 为什么需要这个上限：K 线接口临时故障时不能把整页分数永久作废，
#: 所以失败不能记成"已分析"；但也不能无限重试（20 单 × 每 5 分钟
#: 全是无效请求，还会烧掉下单要用的权重）。7 天足够覆盖任何一次故障，
#: 也不至于让一个季度的历史单每隔 5 分钟被打一次。
KLINE_RETRY_MS = 7 * MS_PER_DAY

#: 打分权重。改这四个数就是改口径，所以集中放在一处、写进文档。
W_PNL, W_ENTRY, W_EXIT, W_COST = 40, 25, 25, 10

#: 盈亏映射：净额占名义价值 ±2% 就打到满/零。选 2% 而不是 ±1%：
#: 这一组样本里真实的单笔波动就在 0.1%~1% 量级（名义 5000U、净额几 U），
#: 用 ±1% 会让绝大多数单直接顶到 0 或 40，等于没有区分度。
PNL_FULL_PCT = 2.0

#: 成本率映射：0.04%（maker 开 + maker 平）拿满分，0.40% 以上零分。
#: 依据实测费率：maker 0.02%、taker 0.05%，所以一次全 taker 的往返是 0.10%。
COST_BEST_PCT = 0.04
COST_WORST_PCT = 0.40

GRADE_CUTS = ((80, "A"), (65, "B"), (50, "C"))


def _round8(x: float) -> float:
    return round(float(x or 0.0), 8)


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else (hi if x > hi else x)


def _new_round(side: str, symbol: str) -> dict:
    return {
        "round_key": "",
        "symbol": symbol,
        "side": side,
        "qty": 0.0,
        "entry_ts": 0,
        "entry_price": 0.0,
        "exit_ts": None,
        "exit_price": None,
        "gross": 0.0,
        "fee": 0.0,
        "funding": 0.0,
        "net": 0.0,
        "hold_sec": 0,
        "n_fills": 0,
        "notional": 0.0,
        "source": "manual",
        "trade_id": None,
        "open_reason": "",
        "mfe": None,
        "mae": None,
        "score_pnl": None,
        "score_entry": None,
        "score_exit": None,
        "score_cost": None,
        "score_total": None,
        "grade": "",
        "analysis": "",
        "analyzed_ts": 0,
        "_open_qty": 0.0,
        "_open_notional": 0.0,
        "_close_qty": 0.0,
        "_close_notional": 0.0,
    }


def _close_round(cur: dict) -> dict:
    """把回合封口：算出 VWAP、数量、自然键。

    **不在这里算净额**：资金费要等所有回合都还原完才知道落在哪一单，
    所以净额统一由 _apply_funding() 算（那也保证只有一处口径）。
    """
    oq = cur.pop("_open_qty", 0.0)
    on = cur.pop("_open_notional", 0.0)
    cq = cur.pop("_close_qty", 0.0)
    cn = cur.pop("_close_notional", 0.0)

    entry_price = (on / oq) if oq > EPS else 0.0
    exit_price = (cn / cq) if cq > EPS else None
    # 开仓量与平仓量在正常情况下相等；取小的那个，
    # 万一数据库/接口给了不完整的一段，也别把「没平掉的部分」当成已平仓。
    qty = _round8(min(oq, cq) if cq > EPS else oq)

    cur["entry_price"] = round(entry_price, 8)
    cur["exit_price"] = round(exit_price, 8) if exit_price is not None else None
    cur["qty"] = qty
    cur["notional"] = round(qty * entry_price, 8)
    if cur["exit_ts"] and cur["entry_ts"]:
        cur["hold_sec"] = max(0, int((cur["exit_ts"] - cur["entry_ts"]) // 1000))
    cur["round_key"] = "%s|%d|%s|%.8f" % (
        cur["symbol"], int(cur["entry_ts"] or 0), cur["side"], qty)
    return cur


def build_rounds(fills: Sequence[dict],
                 funding: Iterable[dict] = (),
                 symbol: str = "BTCUSDT",
                 now_ms: Optional[int] = None) -> List[dict]:
    """把逐笔成交还原成一单一单。纯函数，不碰网络也不碰库——这样才测得动。

    fills 需要按时间升序，字段见 LiveExecutor.user_trades()：
        ts / side(BUY|SELL) / qty / price / realized_pnl / commission
    """
    rounds: List[dict] = []
    cur: Optional[dict] = None
    pos = 0.0

    for f in fills:
        qty = abs(float(f.get("qty") or 0.0))
        if qty <= EPS:
            continue
        price = float(f.get("price") or 0.0)
        comm = abs(float(f.get("commission") or 0.0))
        rp = float(f.get("realized_pnl") or 0.0)
        ts = int(f.get("ts") or 0)
        sign = 1.0 if str(f.get("side", "")).upper() == "BUY" else -1.0
        remaining = qty
        touched_close = False

        # 第一段：平掉已有仓位（可能与后面的开仓段合并在同一笔里）
        while remaining > EPS and abs(pos) > EPS and (pos > 0) != (sign > 0):
            if cur is None:
                # 有仓位却没有回合，只可能是数据从中间截断了。
                # 照样建一个，宁可少算入场也不要静默丢单。
                cur = _new_round("long" if pos > 0 else "short", symbol)
            close_qty = min(remaining, abs(pos))
            part = close_qty / qty
            cur["_close_qty"] += close_qty
            cur["_close_notional"] += close_qty * price
            cur["fee"] += comm * part
            cur["n_fills"] += 1
            cur["exit_ts"] = ts
            if not touched_close:
                # realizedPnl 只对应被平掉的部分，整笔加一次
                cur["gross"] += rp
                touched_close = True
            if not cur["entry_ts"]:
                cur["entry_ts"] = ts
            pos += sign * close_qty
            remaining -= close_qty
            if abs(pos) <= EPS:
                pos = 0.0
                rounds.append(_close_round(cur))
                cur = None
            elif (pos > 0) != (sign > 0) and remaining <= EPS:
                # 不可能：仓位没被平完但单子用完了
                pass

        # 第二段：剩下的部分开新仓（也可能整笔都是开仓）
        if remaining > EPS:
            if cur is None:
                cur = _new_round("long" if sign > 0 else "short", symbol)
            part = remaining / qty
            cur["_open_qty"] += remaining
            cur["_open_notional"] += remaining * price
            cur["fee"] += comm * part
            cur["n_fills"] += 1
            if not cur["entry_ts"]:
                cur["entry_ts"] = ts
            pos += sign * remaining

    # 还开着的仓也要入库（用户要能看见"现在这笔"），只是没有出场价
    if cur is not None and cur["_open_qty"] > EPS:
        cur["exit_ts"] = None
        cur["_close_qty"] = 0.0
        cur["_close_notional"] = 0.0
        rounds.append(_close_round(cur))

    _apply_funding(rounds, funding, now_ms)
    return rounds


def _apply_funding(rounds: Sequence[dict], funding: Iterable[dict],
                   now_ms: Optional[int]) -> None:
    """把资金费按时间窗摊进对应的回合，并**在这里**算净额。

    资金费不在 userTrades 里：手续费是每笔成交扣的，资金费是按持仓时长
    每 8 小时扣的。只看手续费会漏掉这部分持仓成本——持仓过夜的单尤其明显。

    净额只在这一个地方算。原来 _close_round() 里也算一次，于是
    「漏掉资金费」这种错在一个地方改好、另一个地方还是错的，
    测试也只抓得到其中一半——重复计算本身就是缺陷。
    """
    recs = [(int(x.get("ts") or 0), float(x.get("income") or 0.0))
            for x in (funding or ())]
    recs.sort()
    for r in rounds:
        if recs:
            lo = int(r["entry_ts"] or 0)
            hi = int(r["exit_ts"] or now_ms or 0)
            if hi > 0:
                tot = sum(v for t, v in recs if lo <= t <= hi)
                if tot:
                    r["funding"] = round(r["funding"] + tot, 8)
        r["net"] = round(r["gross"] - r["fee"] + r["funding"], 8)


def mark_source(rounds: Sequence[dict], trade_index: dict) -> None:
    """标出这笔仓是谁开的：strategy / adopted / manual。

    trade_index: {entry_ts: {...}}，来自 TradeDB.trade_ids_for_adoption()。
    在容差内取时间最近的一条；匹配不上就是 manual。
    宁可标 manual（不知道）也不猜——来源标错了，后面「策略到底赚不赚钱」
    这个结论就整个不可信。
    """
    if not trade_index:
        return
    keys = sorted(trade_index)
    for r in rounds:
        ets = int(r["entry_ts"] or 0)
        best = None
        best_d = TRADE_MATCH_TOL_MS + 1
        for k in keys:
            d = abs(k - ets)
            if d < best_d:
                best_d, best = d, k
            elif k > ets + TRADE_MATCH_TOL_MS:
                break
        if best is None or best_d > TRADE_MATCH_TOL_MS:
            continue
        info = trade_index[best]
        why = (info.get("open_reason") or "").strip()
        r["trade_id"] = info.get("id")
        r["open_reason"] = why
        if why and why == "实盘对账接管":
            r["source"] = "adopted"
        elif info.get("strategy"):
            r["source"] = "strategy"
        else:
            # 有 trades 行但既不是接管、也没标策略 = 早期手工/未知，按策略记
            r["source"] = "strategy"


def analyze_round(rnd: dict, bars: Sequence[Sequence[float]],
                  now_ms: int, keep_prior: bool = False) -> None:
    """算 MFE/MAE、四项分数、评级和分析文字。就地改 rnd。

    bars: [[open_time, high, low], ...]（1m）
    口径与 trades 表一致：mfe 为正、mae 为负，都是相对入场价的**价格偏移**。
    """
    if keep_prior:
        return
    entry = float(rnd["entry_price"] or 0.0)
    qty = float(rnd["qty"] or 0.0)
    notional = float(rnd["notional"] or 0.0) or (qty * entry)
    long_side = rnd["side"] == "long"
    direction = 1.0 if long_side else -1.0
    lo_ts = int(rnd["entry_ts"] or 0)
    hi_ts = int(rnd["exit_ts"] or now_ms)
    win = [b for b in bars if int(b[0]) + 60_000 > lo_ts and int(b[0]) <= hi_ts]

    if not win:
        # ★ 这里**不能**无条件把 analyzed_ts 写成 now —— 那是「把失败记成成功」。
        #
        # 后果是永久性的：sync() 用 `if r.get("analyzed_ts"): continue` 跳过，
        # 这一单以后再也不会被重试。生产实证：那笔 20 秒的短单因为取数窗口
        # 起点没对齐分钟（已修）而返回空，analyzed_ts 被写成当时的时间，
        # 修好取数逻辑重启后它仍然是"缺 K 线"——修了也白修。
        #
        # 但也不能一律置 0：K 线接口临时故障时，20 单 × 每 5 分钟
        # 会变成持续的无效请求。所以只在一个**有限窗口**内重试；
        # 太早的单承认拉不到，标成已尝试、不再重试。
        too_old = (now_ms - int(rnd["exit_ts"] or now_ms)) > KLINE_RETRY_MS
        rnd["analysis"] = (
            u"缺 K 线，算不出持仓期间的浮盈浮亏"
            u"（1m K 线拉不到；这类记录会在下一轮重算）"
            if not too_old else
            u"缺 K 线，算不出持仓期间的浮盈浮亏"
            u"（这单太早，1m K 线已不在可取范围内）")
        rnd["analyzed_ts"] = now_ms if (rnd["exit_ts"] and too_old) else 0
        return

    best = max(float(b[1]) for b in win) if long_side \
        else min(float(b[2]) for b in win)
    worst = min(float(b[2]) for b in win) if long_side \
        else max(float(b[1]) for b in win)
    # 持仓比一根 1m K 线还短时，能拿到的只是"持仓所在的那一分钟"的最高最低价，
    # 里面还包含入场前 / 出场后的那段价格。于是 MFE 是**上界**，
    # 出场分（抓回浮盈的比例）会被系统性压低。
    # 这靠算法修不掉（数据源只有 1m），只能如实说出来——
    # 否则一笔 20 秒的单会得到一个看起来精确、其实偏低的分数。
    coarse = bool(rnd["exit_ts"]) and int(rnd["hold_sec"] or 0) < 180
    mfe = round((best - entry) * direction, 6)
    mae = round((worst - entry) * direction, 6)
    rnd["mfe"] = mfe
    rnd["mae"] = mae

    fav = max(0.0, mfe) * qty        # 最大浮盈（钱）
    adv = max(0.0, -mae) * qty       # 最大浮亏（钱，正数）
    fee = float(rnd["fee"] or 0.0)
    funding = float(rnd["funding"] or 0.0)
    gross = float(rnd["gross"] or 0.0)
    net = float(rnd["net"] or 0.0)
    hold_min = (rnd["hold_sec"] or 0) / 60.0

    closed = rnd["exit_ts"] is not None
    ret_pct = (net / notional * 100.0) if notional > 0 else 0.0
    cost_pct = ((fee + abs(funding)) / notional * 100.0) if notional > 0 else 0.0

    s_pnl = int(round(_clamp(20.0 + ret_pct * (20.0 / PNL_FULL_PCT), 0, W_PNL)))
    if fav + adv > EPS:
        # 入场质量：进场之后先给浮盈还是先给浮亏
        s_entry = int(round(W_ENTRY * (1.0 - adv / (adv + fav))))
    else:
        s_entry = W_ENTRY // 2       # 一动没动，给中性分，不奖励也不惩罚
    if fav > EPS:
        # 出场质量：最大浮盈里抓回来多少
        s_exit = int(round(W_EXIT * _clamp(gross / fav, 0.0, 1.0)))
    else:
        s_exit = 0                   # 全程没浮盈过，谈不上"抓回"
    s_cost = int(round(W_COST * _clamp(
        (COST_WORST_PCT - cost_pct) / (COST_WORST_PCT - COST_BEST_PCT), 0, 1)))

    rnd["score_pnl"] = s_pnl
    rnd["score_entry"] = s_entry
    rnd["score_exit"] = s_exit
    rnd["score_cost"] = s_cost
    total = s_pnl + s_entry + s_exit + s_cost
    rnd["score_total"] = total
    rnd["grade"] = _grade(total)

    # ---- 分析文字：把每个分数的来源数字写出来，不只给一个数 ----
    L = []
    L.append(u"入场 %.2f（%s）→ 出场 %s（%s）｜持仓 %.1f 分钟"
             % (entry, _fmt_ts(rnd["entry_ts"]),
                ("%.2f" % rnd["exit_price"]) if rnd["exit_price"] else "未平",
                _fmt_ts(rnd["exit_ts"]) if rnd["exit_ts"] else "—", hold_min))
    L.append(u"数量 %.6f（名义 %.1fU）｜毛 %+.4f｜手续费 %.4f｜资金费 %+.4f｜净 %+.4f（%+.3f%% 名义）"
             % (qty, notional, gross, fee, funding, net,
                ret_pct if closed else 0.0))
    if fav + adv > EPS:
        if adv < fav:
            verdict = u"进场后先给浮盈，位置偏好"
        elif abs(adv - fav) < 0.15 * max(fav, adv):
            verdict = u"浮盈浮亏相当，位置中性"
        else:
            verdict = u"进场后先给浮亏，位置偏差"
        L.append(u"入场 %d/%d：最大浮盈 %.4fU、最大浮亏 %.4fU → %s"
                 % (s_entry, W_ENTRY, fav, adv, verdict))
    else:
        L.append(u"入场 %d/%d：持仓期间价格几乎没动" % (s_entry, W_ENTRY))
    if fav > EPS and gross < 0:
        # 明明有过浮盈，最后是倒亏。这是最该被指出来的形态：
        # 写成「抓回 -5.40U（0%）」读起来像格式问题，等于把话说轻了。
        L.append(u"出场 %d/%d：最大浮盈 %.4fU 全部还回去，倒亏 %.4fU"
                 % (s_exit, W_EXIT, fav, -gross))
    elif fav > EPS:
        L.append(u"出场 %d/%d：最大浮盈 %.4fU 里抓回 %.4fU（%.0f%%）"
                 % (s_exit, W_EXIT, fav, gross,
                    100.0 * _clamp(gross / fav, 0, 1)))
    else:
        L.append(u"出场 %d/%d：全程没浮盈过，谈不上抓回" % (s_exit, W_EXIT))
    L.append(u"成本 %d/%d：手续费+资金费占名义 %.4f%%（%.3f%% 满分、%.2f%% 零分）"
             % (s_cost, W_COST, cost_pct, COST_BEST_PCT, COST_WORST_PCT))
    if closed:
        L.append(u"盈亏 %d/%d：净额 %+.4fU（占名义 %+.3f%%）"
                 % (s_pnl, W_PNL, net, ret_pct))
    else:
        unreal = (float(best) - entry) * direction * qty
        L.append(u"盈亏 %d/%d：还没平仓，当前浮盈 %+.4fU（未计入净额）"
                 % (s_pnl, W_PNL, unreal))
    L.append(u"综合 %d/100 → %s" % (total, rnd["grade"]))
    if coarse:
        # 不写这一句，用户会以为 20 秒的单也拿到了精确的浮盈浮亏。
        # 实际拿到的是那一整分钟的最高最低价 —— MFE 偏大、出场分偏低。
        L.append(u"注：持仓不足 3 分钟，1m K 线只能给出所在那一分钟的最高最低价，"
                 u"最大浮盈是上界、出场分偏低")
    rnd["analysis"] = u"\n".join(L)
    rnd["analyzed_ts"] = now_ms if closed else 0


def _grade(total: int) -> str:
    for cut, g in GRADE_CUTS:
        if total >= cut:
            return g
    return "D"


def _fmt_ts(ms) -> str:
    if not ms:
        return "—"
    import datetime as _dt
    return _dt.datetime.fromtimestamp(int(ms) / 1000).strftime("%m-%d %H:%M:%S")


def merge_prior(rounds: Sequence[dict], prior: dict) -> None:
    """把库里已算好的分析结果搬回刚还原出来的回合上。

    为什么需要：同步是幂等的「整行覆盖」，而还原出来的行【没有分数】
    （分数要靠额外拉 K 线才算）。如果每次都直接覆盖，
    已经算过的单会每 5 分钟被清空一次，看板上刚出现的分数又消失。
    """
    for r in rounds:
        p = prior.get(r["round_key"])
        if not p:
            continue
        same_fills = int(p.get("n_fills") or 0) == int(r.get("n_fills") or 0)
        if p.get("analyzed_ts") and same_fills:
            for k in ("mfe", "mae", "score_pnl", "score_entry", "score_exit",
                      "score_cost", "score_total", "grade", "analysis",
                      "analyzed_ts"):
                if r.get(k) in (None, "", 0):
                    r[k] = p[k]
        if p.get("trade_id") and not r.get("trade_id"):
            r["trade_id"] = p["trade_id"]
        if p.get("open_reason") and not r.get("open_reason"):
            r["open_reason"] = p["open_reason"]


async def sync(ex, tdb, days_back: int = 30, analysis_limit: int = 20,
               now_ms: Optional[int] = None) -> dict:
    """抓一次真账：成交 → 回合 → 打分 → 落库。幂等。

    调用点（engine）：每次 reconcile_live()、每次引擎平仓之后。
    失败只记日志、不抛——这是"记账"，绝不能反过来把对账和交易搞崩。
    """
    import time as _time
    now = int(now_ms or _time.time() * 1000)
    symbol = "BTCUSDT"
    try:
        symbol = ex.cfg.trading.symbol
    except Exception:
        pass

    fills = await ex.user_trades(days_back=days_back)
    try:
        funding = await ex.funding_income(days_back=days_back)
    except Exception as e:      # 资金费拉不到不影响主账
        logger.warning("资金费拉取失败（继续）：%s", str(e)[:120])
        funding = []

    rounds = build_rounds(fills, funding, symbol=symbol, now_ms=now)
    prior = tdb.live_round_keys()
    mark_source(rounds, tdb.trade_ids_for_adoption())
    merge_prior(rounds, prior)

    # 只给最近 N 单算 MFE/MAE：每单要单独拉一次 1m K 线，
    # 全history 都算的话一次同步几十个请求，而页面只展示 20 单。
    closed = [r for r in rounds if r["exit_ts"]]
    closed.sort(key=lambda r: r["exit_ts"], reverse=True)
    targets = closed[:analysis_limit]
    analyzed = 0
    for r in targets:
        if r.get("analyzed_ts"):
            continue
        try:
            bars = await ex.klines_1m(int(r["entry_ts"]), int(r["exit_ts"]))
            analyze_round(r, bars, now)
            analyzed += 1
        except Exception as e:
            logger.warning("算 MFE/MAE 失败 %s: %s", r.get("round_key"),
                           str(e)[:120])
    # 还开着的那一笔也算（能看见当前浮盈浮亏），但不给评级
    for r in rounds:
        if r["exit_ts"] is None:
            try:
                bars = await ex.klines_1m(int(r["entry_ts"]), now)
                analyze_round(r, bars, now)
            except Exception as e:
                logger.warning("算在持仓浮盈失败: %s", str(e)[:120])

    tdb.upsert_live_rounds(rounds)
    return {"fills": len(fills), "funding": len(funding),
            "rounds": len(rounds), "analyzed": analyzed, "ts": now}
