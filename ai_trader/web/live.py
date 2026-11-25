# -*- coding: utf-8 -*-
"""准实时行情：3m / 15m / 1h K 线 + EMA 双通道（EMA144 与 EMA169 之间的井字带）。

为什么由服务端代理，而不是浏览器直连币安
----------------------------------------
老项目的实测教训：浏览器直连会吃 CORS、地区封锁、公司网络代理，
用户换个网络就白屏；服务端直连只要 110ms，且代理配置已经是对的。

为什么 EMA 必须用 1500 根预热，而不是只取 200 根
------------------------------------------------
EMA 是递推量，喂多少历史直接决定精度。老项目在生产真实行情上量过：
    3m 只用 200 根算 EMA200，与满窗口差 62.4 点（0.078%）
    1h 只用 200 根差 82.8 点（0.105%），用 500 根仍差 17.5 点
而 EMA144 与 EMA169 这两条线本身只差 83.6 点（3m 实测）——
各带 20~38 点误差就足以把「谁在上方」判反，而那个上下关系正是井字带要表达的
东西。所以必须取满（币安单次上限 1500），算完再切最后 200 根给前端显示。

缓存 1.8s：多个标签页同时 3s 轮询时，进程内只打一次币安，不打爆配额。
TTL 取得比轮询间隔略短（1.8s < 3s），所以每个轮询周期仍会真取一次价，
不会「看着在动，其实是旧值」。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request

from flask import Blueprint, current_app, jsonify, request

from .auth import login_required

logger = logging.getLogger(__name__)

live_bp = Blueprint("live", __name__, url_prefix="/admin/api")

# 只放行真实 K 线周期，避免把任意字符串透传给币安。
#
# ⚠ 加 5m 只是让**这个只读端点**能向币安要 5m K 线。
#   它**没有**动 `config.INTERVALS_MS` —— 那个列表决定实时聚合器要产出哪些周期，
#   加进去就会让聚合器开始往生产 market.db 里写 5m bar（而且要过
#   market/agg.py 那条"3s 是原子周期"的断言）。改聚合周期是另一件事，
#   得先确认下游都吃得住，不能顺手在 Web 层捎带。
ALLOWED_INTERVALS = {"3m": 180_000, "5m": 300_000, "15m": 900_000,
                     "1h": 3_600_000}
DEFAULT_INTERVAL = "3m"

EMA_PERIODS = (20, 50, 144, 169, 200)
EMA_WARMUP_BARS = 1500      # 币安单次上限
DISPLAY_BARS = 200
CACHE_TTL = 1.8

_cache: dict = {}           # interval -> (ts, payload)
_lock = threading.Lock()


def _symbol() -> str:
    cfg = current_app.config.get("CFG")
    try:
        return cfg.trading.symbol
    except Exception:
        return "BTCUSDT"


def _proxy_mode():
    """`AI_HTTP_PROXY` 想表达哪种模式：('default'|'direct'|'explicit', url)。

    单独抽出来是为了**可测**：`build_opener()` 的结果没法可靠断言
    —— 实测 Python 3.9 里 `build_opener(ProxyHandler({}))` 的 `.handlers`
    里根本看不到 ProxyHandler（`ProxyHandler({})` 与无参调用都是 0 个），
    所以"检查 handler 列表"这种测试是无效的，看着绿、其实什么都没验。
    把它拆成一个返回枚举的小函数，测试才钉得住真实决策。
    """
    raw = (os.environ.get("AI_HTTP_PROXY") or "").strip()
    if not raw:
        return ("default", "")
    if raw.lower() in ("off", "none", "direct", "0"):
        return ("direct", "")
    return ("explicit", raw)


def _opener():
    """按 `AI_HTTP_PROXY` 决定走不走代理。

    ## 为什么需要这个开关

    `urllib.request.build_opener()` 会自动读 Windows 注册表的代理设置
    （`ProxyEnable=1` / `ProxyServer=127.0.0.1:4780`）。这台机器上就有：
    代理没起来的时候，Python 拿到的错误是
    `URLError: [Errno 2] No such file or directory` —— 一个**看起来像文件问题、
    实际是代理 socket 连不上**的错误。2026-11-09 我就是被它误导，
    把"代理不通"写成了"币安不可达"。

    `AI_HTTP_PROXY` 三种取值：
        未设置 / 空  → 沿用 urllib 默认（读系统代理）。保持旧行为。
        "off"        → **强制直连**，完全不读系统代理。
        "http://…"   → 显式用这个代理。

    强制直连在"必须走代理才能出网"的机器上会超时 —— 所以它是个
    显式开关，不做默认，也不做自动降级（自动降级会让故障变成偶发，
    更难查）。
    """
    mode, url = _proxy_mode()
    if mode == "direct":
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    if mode == "explicit":
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": url, "https": url}))
    return urllib.request.build_opener()


def _fetch_klines(symbol: str, interval: str, limit: int,
                  retries: int = 2, base_sleep: float = 0.6) -> list:
    """同步拉币安 K 线，带有限重试。

    重试为什么值得加：这台机器出网要过一个本地代理，实测**时通时不通**
    （同一进程内 pack 先成功、紧接着直接调就失败）。行情图偶发空白比
    多花 1~2 秒糟得多，所以重试 2 次、退避 0.6s/1.2s。

    为什么只重试 2 次：`PACK_TTL=2.8s` 是为了跟上 3s 轮询。重试太多会让
    单次请求耗时超过轮询周期，请求开始堆积，反而更糟。

    错误分两类，别混：
        - 代理/网络层（URLError、超时）→ 值得重试
        - 币安明确拒绝（HTTP 4xx/5xx 的 HTTPError 带状态码）→ 不重试，
          重试只会让封禁更久
    """
    url = ("https://fapi.binance.com/fapi/v1/klines"
           "?symbol=%s&interval=%s&limit=%d" % (symbol, interval, limit))
    last_err = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "ai-trader"})
            with _opener().open(req, timeout=12) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            # 币安回了状态码 = 网络是通的，重试没意义
            raise RuntimeError(
                "币安拒绝请求 HTTP %s（interval=%s）" % (e.code, interval))
        except (urllib.error.URLError, OSError) as e:
            last_err = e
            if attempt < retries:
                time.sleep(base_sleep * (2 ** attempt))
                continue
            reason = str(getattr(e, "reason", e))
            raise RuntimeError(
                "行情拉取失败（已重试 %d 次）：%s；代理=%s。"
                "若代理没起来，设 AI_HTTP_PROXY=off 可强制直连"
                % (retries, reason[:120],
                   (os.environ.get("AI_HTTP_PROXY") or "系统默认")))
    raise RuntimeError("行情拉取失败：%s" % last_err)


def ema_series(closes: list, period: int) -> list:
    """标准 EMA（α = 2/(N+1)），首值用起点收盘价作种子。

    末根是未收盘那根时，它的 EMA 会随实时价跳动——这是**正确**行为：
    EMA 是价格变换，不是模型输出。这与区间演化四条线必须按收盘更新是两回事
    （四条线外推就是造假，EMA 不更新反而是错）。
    """
    a = 2.0 / (period + 1)
    out = []
    e = None
    for v in closes:
        e = v if e is None else v * a + e * (1.0 - a)
        out.append(round(e, 1))
    return out


def _build(symbol: str, interval: str) -> dict:
    raw = _fetch_klines(symbol, interval, EMA_WARMUP_BARS)
    if not raw:
        raise RuntimeError("币安返回空数据")
    closes = [float(r[4]) for r in raw]
    emas = {str(p): ema_series(closes, p) for p in EMA_PERIODS}

    tail = raw[-DISPLAY_BARS:]
    off = len(raw) - len(tail)
    return {
        "symbol": symbol,
        "interval": interval,
        "interval_ms": ALLOWED_INTERVALS[interval],
        "warmup": len(raw),
        # 蜡烛：[open_time, open, close, low, high]（ECharts 要求 开/收/低/高）
        "bars": [[int(r[0]), float(r[1]), float(r[4]), float(r[3]), float(r[2])]
                 for r in tail],
        "ema": {k: v[off:] for k, v in emas.items()},
        "server_time": int(time.time() * 1000),
    }


@live_bp.get("/live")
@login_required
def live_api():
    interval = request.args.get("interval", DEFAULT_INTERVAL)
    if interval not in ALLOWED_INTERVALS:
        return jsonify({"ok": False,
                        "error": "bad interval",
                        "allowed": sorted(ALLOWED_INTERVALS)}), 400
    symbol = _symbol()
    key = symbol + "|" + interval
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < CACHE_TTL:
            return jsonify(hit[1])
    try:
        payload = _build(symbol, interval)
    except (urllib.error.URLError, RuntimeError, ValueError, KeyError) as e:
        # 取不到就把上一次的好数据返回，附带 stale 标记——
        # 图不会突然空掉，而用户能看见"这是旧值"
        logger.warning("准实时行情拉取失败 %s %s: %s", symbol, interval,
                       str(e)[:120])
        with _lock:
            hit = _cache.get(key)
        if hit:
            out = dict(hit[1])
            out["stale"] = True
            return jsonify(out)
        return jsonify({"ok": False, "error": "拉取失败: %s" % str(e)[:120]}), 502
    payload["ok"] = True
    with _lock:
        _cache[key] = (now, payload)
    return jsonify(payload)

# ---------- 组合包：一次给总览页画图需要的全部数据 ----------
# 为什么合成一个端点：总览要 K 线 + 148 条 EMA + 四线 + 现价，
# 分成四个端点意味着四轮往返、四处缓存、四处可能口径不同。
# 数据在同一时刻取，图才对得上。
PACK_TTL = 2.8          # 略小于前端 3s 轮询，保证每次轮询都拿到新的一根
_pack_lock = threading.Lock()
_pack_cache = {}


def _live_ema(symbol: str, interval: str) -> dict:
    """按周期取实时 K 线 + EMA，直接用 _build 的缓存，不重复请求币安。"""
    key = symbol + "|" + interval
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < CACHE_TTL:
            return hit[1]
    payload = _build(symbol, interval)
    payload["ok"] = True
    with _lock:
        _cache[key] = (now, payload)
    return payload


@live_bp.get("/pack")
@login_required
def pack_api():
    """总览页 / 独立看板页共用的数据包。

    四线不算在这里：它们由调用方（页面）自己带 range 数据，
    因为区间演化是 1h 语义，和这里的 3m/15m 不是同一个时间轴。
    K 线/EMA/现价必须是同一次抓取，否则现价和最后一根蜡烛会错位。
    """
    symbol = _symbol()
    interval = request.args.get("interval", "3m")
    if interval not in ALLOWED_INTERVALS:
        return jsonify({"ok": False, "error": "bad interval",
                        "allowed": sorted(ALLOWED_INTERVALS)}), 400
    try:
        live = _live_ema(symbol, interval)
        # 1h 一并给出：总览要把"当前未完成的 1h"叠在 3m 主图上做参考
        h1 = _live_ema(symbol, "1h")
    except (urllib.error.URLError, RuntimeError, ValueError, KeyError) as e:
        with _pack_lock:
            hit = _pack_cache.get(symbol)
        if hit:
            out = dict(hit[1])
            out["stale"] = True
            out["error"] = str(e)[:120]
            return jsonify(out)
        return jsonify({"ok": False, "error": "拉取失败: %s" % str(e)[:120]}), 502

    last = live["bars"][-1]
    prev = live["bars"][-2] if len(live["bars"]) > 1 else None
    out = {
        "ok": True,
        "symbol": symbol,
        "interval": interval,
        "price": last[2],
        "change": (last[2] - prev[2]) if prev else None,
        "change_pct": ((last[2] - prev[2]) / prev[2] * 100.0) if prev else None,
        "live": live,
        # 未收盘的 1h：open_time 是它自己的桶起点，close 是当前实时价
        "h1_now": h1["bars"][-1],
        "server_time": int(time.time() * 1000),
    }
    with _pack_lock:
        _pack_cache[symbol] = (time.time(), out)
    return jsonify(out)
