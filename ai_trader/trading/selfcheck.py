# -*- coding: utf-8 -*-
"""链路自检单 —— 用真钱把整条链路证一遍。

**为什么需要它**

生产数据上三条形态在 107 天里一次都没通过（`detect_all` 的 `:ok` 计数为 0），
而链路（预期 → 挂单 → 成交 → 推进 → 出场 → 记账 → 对账）一次都没被真实订单
走通过。等一个可能永远不来的信号来验收链路，等于不验收。

**它做了什么、没做什么**

只跳过**一个**判断：这条计划该不该存在（形态认不认）。
之后的每一步都是生产代码本身：

    engine._pending_params()   ← 止损口径（公式 d_max，唯一来源）
    engine._pending_size()     ← 仓位口径（sizing.plan_position，唯一来源）
    engine._pending_place()    ← 安全闸 + 交易所规格 + 真下单
    thesis.step()              ← 成交判定 / 推进 / 出场（下一根 5m 起由引擎驱动）
    trades / risk_events       ← 记账
    engine.reconcile_live()    ← 对账

**为什么用请求文件而不是直接下单**

生产只有一个进程持有交易所连接（systemd 服务）。第二个进程去下单会和它抢
同一个持仓，且它看不到我们写的内存状态。所以：

    python -m ai_trader.trading.selfcheck --arm     # 写请求文件，不下单
    systemctl restart ai-trader                     # 服务启动后第一根 5m 消费请求

请求被**读走即删除**（原子 rename），所以只会触发一次；重复 `--arm`
会覆盖，不会堆积。

**身份标记**

`thesis_id` 以 ``selfcheck-`` 开头。这是唯一的识别依据 —— 不靠 `kind`
（复用已注册形态，否则状态机的注册表查不到会整条跳过），也不靠内存标志
（重启就丢）。`kind` 用的是 `KIND_WICKS`：它属于反转家族，按用户 2026-11-19
的口径「反转家族不看 ema 失效」，于是自检仓**只会**因止损 / 止盈 / 显式撤单
而结束，不会被无关的失效判据提前平掉，测试目的才清晰。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import time
from typing import Any, Dict, Optional

from . import pending_plan as P
from . import thesis as T

logger = logging.getLogger(__name__)

#: 请求文件名（放在交易库同目录，与其它 *_trade.db / *_pending.db 同级）
REQUEST_FILE = "selfcheck.request"
#: 撤单请求文件名
CANCEL_FILE = "selfcheck.cancel"
#: 身份前缀。**唯一**识别依据，见模块 docstring。
ID_PREFIX = "selfcheck-"
#: 复用哪一种形态。必须是有注册表条目的 kind，否则 `thesis.step()` 找不到它。
KIND = P.KIND_WICKS

#: 请求文件里允许出现的动作
ACTION_FIRE = "fire"
ACTION_CANCEL = "cancel"


# --------------------------------------------------------------- 路径与文件

def _base_dir(cfg: Any) -> str:
    return os.path.dirname(os.path.abspath(str(cfg.store.path)))


def request_path(cfg: Any) -> str:
    return os.path.join(_base_dir(cfg), REQUEST_FILE)


def cancel_path(cfg: Any) -> str:
    return os.path.join(_base_dir(cfg), CANCEL_FILE)


def _write_atomic(path: str, payload: Dict[str, Any]) -> None:
    """先写临时文件再 rename —— 半个 JSON 读出来会让服务启动即崩。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def arm(cfg: Any, *, direction: str = P.UP, note: str = "",
        path: Optional[str] = None) -> str:
    """写一份自检请求（不下单）。返回请求文件路径。"""
    d = str(direction or P.UP).lower()
    if d not in (P.UP, P.DOWN):
        raise ValueError("direction 只能是 up/down，收到 %r" % (direction,))
    p = path or request_path(cfg)
    _write_atomic(p, {"action": ACTION_FIRE, "direction": d,
                      "note": str(note or ""), "armed_at": int(time.time() * 1000)})
    return p


def arm_cancel(cfg: Any, *, note: str = "",
               path: Optional[str] = None) -> str:
    p = path or cancel_path(cfg)
    _write_atomic(p, {"action": ACTION_CANCEL, "note": str(note or ""),
                      "armed_at": int(time.time() * 1000)})
    return p


def disarm(path: str) -> bool:
    """删掉请求文件。返回是否真的删了。"""
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return False


def _take(path: str) -> Optional[Dict[str, Any]]:
    """读走即删。读不出来（半截文件/坏 JSON）也删掉并返回 None。

    ★ 坏文件必须删：否则服务每根 5m 都会重新踩同一个坑，日志被刷满，
      而真正的原因埋在几千行里。
    """
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        logger.error("[自检] 请求文件解析失败，已丢弃：%s", path, exc_info=True)
        disarm(path)
        return None
    disarm(path)
    if not isinstance(data, dict):
        logger.error("[自检] 请求文件不是对象，已丢弃：%r", type(data))
        return None
    return data


def take_request(cfg: Any) -> Optional[Dict[str, Any]]:
    return _take(request_path(cfg))


def take_cancel(cfg: Any) -> Optional[Dict[str, Any]]:
    return _take(cancel_path(cfg))


# ------------------------------------------------------------------- 身份

def selfcheck_id(ts_ms: int) -> str:
    return "%s%d" % (ID_PREFIX, int(ts_ms))


def is_selfcheck_id(thesis_id: Any) -> bool:
    return str(thesis_id or "").startswith(ID_PREFIX)


def live_selfcheck_rows(rows) -> list:
    """从库里筛出还没结束的自检行。

    「还没结束」= 不在终态。终态集合只认 `thesis.TERMINAL_STATES`
    （不在这里另写一份，两处定义漂了就会重复开仓）。
    """
    out = []
    for row in rows or ():
        if not is_selfcheck_id(row.get("thesis_id")):
            continue
        if str(row.get("state") or "") in T.TERMINAL_STATES:
            continue
        out.append(row)
    return out


# ------------------------------------------------------------------- 定价

def amp_of(bars) -> float:
    """一段 K 线的振幅（最高 - 最低）。空/单根不合法时返回 0。"""
    seq = [b for b in (bars or ()) if b is not None]
    if not seq:
        return 0.0
    hi = max(float(getattr(b, "h", 0.0) or 0.0) for b in seq)
    lo = min(float(getattr(b, "l", 0.0) or 0.0) for b in seq)
    return max(0.0, hi - lo)


def build_prices(price: float, direction: str, params: P.PendingParams,
                 h1_amp: float = 0.0, amp5: float = 0.0,
                 order_type: str = T.ORDER_LIMIT) -> P.PricePlan:
    """用**生产的定价函数**算止损/止盈，不在这里写任何数。

    `max_stop_pct` 已经由 `engine._pending_params()` 从 `sizing.derive()` 填好，
    所以走的是公式止损那条分支；两档振幅在这条分支里不参与计算，但它们
    **必须是真的**（`plan_prices` 会拒绝 <=0），不能为了过校验填假值。
    """
    px = float(price or 0.0)
    if px <= 0:
        raise ValueError("现价无效：%r" % (price,))
    if float(getattr(params, "max_stop_pct", 0.0) or 0.0) <= 0:
        raise ValueError(
            "max_stop_pct 为 0：止损会落回振幅规则而不是公式。"
            "自检单必须用 engine._pending_params() 生成的参数。")
    return P.plan_prices(px, str(direction).lower(), float(h1_amp), float(amp5),
                         params, order_type=order_type)


# ------------------------------------------------------------------- 拒绝

def refusal(engine: Any, cfg: Any) -> str:
    """能不能开自检单。返回拒绝理由，空字符串 = 可以。

    ★ 这些条件缺一条都会造成真实损失或脏账：
      非 live / dry-run → 什么都不会真的发生，等于没测；
      已有持仓或挂单   → 会叠加敞口，且平仓时抢同一个仓位；
      已有未结束自检行 → 重复开仓。
    """
    mode = str(getattr(getattr(cfg, "trading", None), "mode", "") or "")
    if mode != "live":
        return "当前模式是 %r，不是 live：自检单必须用真钱才有意义" % (mode,)
    try:
        if engine._pending_is_dry():
            return "挂单策略处于 dry-run：不会真的下单"
    except Exception:
        pass
    pos = getattr(engine, "pos", None) or {}
    if abs(float(pos.get("qty") or 0.0)) > 0:
        return "已有持仓（%s %.6f）：先平掉再自检" % (
            pos.get("side"), float(pos.get("qty") or 0.0))
    try:
        rows = engine.pending.live(limit=200) or ()
    except Exception:
        rows = ()
    if live_selfcheck_rows(rows):
        return "已有一条未结束的自检单：不重复开仓"
    return ""


# ------------------------------------------------------------------- 执行

def _build_thesis(pp: P.PricePlan, direction: str, tid: str) -> Any:
    return T.Thesis(
        thesis_id=tid, kind=KIND, direction=str(direction).lower(),
        order_price=float(pp.order_price), stop_price=float(pp.stop_price),
        target_price=float(pp.target_price), order_type=T.ORDER_LIMIT,
        family=P.PATTERN_FAMILY[KIND], surprise_pts=0.0)


async def fire_once(engine: Any, b15, cfg: Any, b5=None) -> Dict[str, Any]:
    """执行一次自检单。返回结果字典（供日志与测试断言）。

    `b15` 必须是引擎当前这根 5m 步进里用的 15m 序列 —— `next_index` 要落在
    最后一根上，否则状态机会从历史第一根开始重放成交。
    """
    req = take_request(cfg)
    if req is None:
        return {"fired": False, "reason": "没有请求文件"}
    if str(req.get("action") or "") != ACTION_FIRE:
        return {"fired": False, "reason": "请求动作不是 fire：%r" % (req.get("action"),)}

    why = refusal(engine, cfg)
    if why:
        logger.warning("[自检] 拒绝执行：%s", why)
        try:
            engine.db.log_risk("selfcheck_refused", why[:220])
        except Exception:
            pass
        return {"fired": False, "reason": why}

    bars = list(b15 or ())
    if not bars:
        return {"fired": False, "reason": "没有 15m 序列：算不出游标"}

    direction = str(req.get("direction") or P.UP).lower()
    if direction not in (P.UP, P.DOWN):
        return {"fired": False, "reason": "方向非法：%r" % (direction,)}

    price = float(await engine.ex.mark_price())
    params = engine._pending_params()
    # 振幅：1h 用最后 4 根 15m 拼出当前小时，5m 用最后一根。
    # 公式止损分支不用它们，但 plan_prices 拒绝 <=0 —— 不许填假值糊过去。
    h1_amp = amp_of(bars[-4:])
    amp5 = amp_of(list(b5 or ())[-1:])
    if amp5 <= 0:
        amp5 = h1_amp / 4.0 if h1_amp > 0 else 0.0
    if h1_amp <= 0 or amp5 <= 0:
        return {"fired": False, "reason": "算不出振幅（15m 最高=最低？）：h1=%r amp5=%r"
                % (h1_amp, amp5)}
    pp = build_prices(price, direction, params, h1_amp=h1_amp, amp5=amp5)

    idx = len(bars) - 1
    bar_ts = int(getattr(bars[idx], "ts", 0) or 0)
    tid = selfcheck_id(bar_ts or int(time.time() * 1000))

    th = _build_thesis(pp, direction, tid)
    from .thesis import Action
    act = Action(kind="place_order", thesis_id=tid, reason="selfcheck",
                 price=float(th.order_price), side=th.side,
                 order_type=T.ORDER_LIMIT, state_after=T.ARMED)

    # 真正下单（安全闸 / 仓位口径 / 交易所规格都在里面）
    #
    # ★★ 必须看返回值（2026-11-24 生产事故的同一处缺陷）。
    #
    #   以前这里是 `await engine._pending_place(act, th)`，不看结果，
    #   接着无条件把 thesis 记成 ARMED、返回 `fired=True`。
    #   而 11-20 起每一张挂单都被交易所拒（-4015 clientOrderId 超长），
    #   自检单却每轮都报「已下链路自检单」——**自检本身失效了**，
    #   它验证的是「我调了下单函数」，不是「单挂出去了」。
    #   一张没挂出去的单还被记成 ARMED，之后就会按 K 线判出幽灵成交。
    #
    #   自检的全部意义是「证明链路真的通」，所以挂单没成功必须报失败，
    #   不能记 ARMED，也不能返回 fired=True。
    if not await engine._pending_place(act, th):
        reason = "挂单没成功（安全闸/仓位/交易所拒单），见 pending_place_* 风险事件"
        logger.error("[自检] 链路自检单**没有挂出去**：%s（%s）", tid, reason)
        try:
            engine.db.log_risk("selfcheck_place_failed", "%s: %s" % (tid, reason))
        except Exception:
            pass
        return {"fired": False, "reason": reason, "thesis_id": tid}

    from . import pending_thesis_store as _PTS
    engine.pending.add_if_new(_PTS.row_of(
        th, state=T.ARMED, next_index=idx, armed_at_bar=idx,
        armed_ref_close=price, bar_ts=bar_ts, note="selfcheck"))

    detail = ("%s %s %.2f（止损 %.2f 距离 %.3f%% / 止盈 %.2f）"
              % (th.direction, th.side, th.order_price, th.stop_price,
                 pp.stop_distance / th.order_price * 100.0, th.target_price))
    logger.warning("[自检] 已下链路自检单 %s：%s", tid, detail)
    try:
        engine.db.log_risk("selfcheck_fired", detail[:220])
    except Exception:
        pass
    return {"fired": True, "thesis_id": tid, "detail": detail,
            "order_price": th.order_price, "stop_price": th.stop_price,
            "target_price": th.target_price, "next_index": idx,
            "bar_ts": bar_ts}


async def cancel_once(engine: Any, cfg: Any) -> Dict[str, Any]:
    """撤掉自检单的挂单并平掉它的持仓（走引擎自己的撤单/平仓路径）。"""
    req = take_cancel(cfg)
    if req is None:
        return {"fired": False, "reason": "没有撤单请求文件"}

    from .thesis import Action
    rows = []
    try:
        rows = list(engine.pending.live(limit=200) or ())
    except Exception:
        pass
    targets = live_selfcheck_rows(rows)
    if not targets:
        return {"fired": False, "reason": "没有未结束的自检单"}
    done = []
    for row in targets:
        tid = str(row.get("thesis_id") or "")
        try:
            await engine._pending_cancel(Action(kind="cancel", thesis_id=tid,
                                                reason="selfcheck_cancel"))
        except Exception as e:
            logger.error("[自检] 撤单失败 %s：%s", tid, e)
        closed_ok = False
        try:
            await engine._pending_close(Action(kind="close", thesis_id=tid,
                                               reason="selfcheck_cancel"), None)
            closed_ok = True
        except Exception as e:
            logger.error("[自检] 平仓失败 %s：%s", tid, e)
        _mark_cancelled(engine, row, closed_ok)
        done.append(tid)
        try:
            engine.db.log_risk("selfcheck_cancelled", tid)
        except Exception:
            pass
    return {"fired": True, "cancelled": done}


def _mark_cancelled(engine: Any, row: Dict[str, Any], closed_ok: bool) -> None:
    """把自检行推到终态。

    ★ 不推的后果（2026-11-20 实测）：撤单+平仓都成功了，行还停在 `FILLED` ——
      于是 (1) 下一张自检单被 `refusal()` 以「还有未结束的自检单」拒绝，
      (2) 状态机每根 5m 继续发平仓动作（空仓时是空转）。
      「动作做完了但状态没落」和没做一样，行不行看的是状态。
    """
    from . import pending_thesis_store as _PTS
    from . import thesis as _T

    try:
        th = _PTS.thesis_of(row)
        prog = _PTS.progress_of(row)
        state = _T.CANCELLED if closed_ok else _T.CLOSED_THESIS
        engine.pending.upsert_thesis(_PTS.row_of(
            th, state=state, next_index=int(prog.get("next_index") or 0),
            armed_at_bar=prog.get("armed_at_bar"),
            armed_ref_close=prog.get("armed_ref_close"),
            bar_ts=int(row.get("bar_ts") or 0),
            ambiguous=bool(row.get("ambiguous")),
            note="selfcheck_cancel" if closed_ok else "selfcheck_cancel_unclosed"))
        logger.warning("[自检] %s 已落终态 %s", th.thesis_id, state)
    except Exception as e:
        logger.error("[自检] %s 落终态失败：%s", row.get("thesis_id"), e, exc_info=True)
        try:
            engine.db.log_risk("selfcheck_state_error",
                               "%s: %s" % (row.get("thesis_id"), str(e)[:200]))
        except Exception:
            pass


# --------------------------------------------------------------------- CLI

def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m ai_trader.trading.selfcheck",
        description="链路自检单：写/撤请求文件，真正下单由服务进程执行")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--arm", action="store_true", help="写一份自检请求（不下单）")
    g.add_argument("--cancel", action="store_true", help="写一份撤单请求")
    g.add_argument("--status", action="store_true", help="只读：显示请求文件状态")
    g.add_argument("--disarm", action="store_true", help="删掉自检请求文件")
    ap.add_argument("--direction", default=P.UP, choices=[P.UP, P.DOWN])
    ap.add_argument("--note", default="")
    a = ap.parse_args(argv)

    from .. import config as cfgmod
    cfg = cfgmod.load()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    if a.arm:
        p = arm(cfg, direction=a.direction, note=a.note)
        print("已写自检请求：%s" % p)
        print("下一步：重启服务，它会在第一根 5m 消费该请求并真实下单")
        print("   systemctl restart ai-trader")
        return 0
    if a.cancel:
        p = arm_cancel(cfg, note=a.note)
        print("已写撤单请求：%s" % p)
        print("下一步：重启服务执行撤单+平仓")
        return 0
    if a.disarm:
        n = int(disarm(request_path(cfg))) + int(disarm(cancel_path(cfg)))
        print("已删除 %d 个请求文件" % n)
        return 0

    for label, path in (("自检请求", request_path(cfg)),
                        ("撤单请求", cancel_path(cfg))):
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    print("%s: %s" % (label, json.load(fh)))
            except Exception as e:
                print("%s: 存在但读不出来（%s）" % (label, e))
        else:
            print("%s: 无" % label)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
