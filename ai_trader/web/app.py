"""Flask 数据端点 + 静态看板。

和 k-trader 的关键差别：不用 Jinja 渲染页面、不用请求-响应轮询，页面本身是一条
原生 WebSocket 长连接，服务端把 3s 桶实时推过去。Flask 只负责历史和健康快照。
"""
from __future__ import annotations

import logging
import os
import time
from typing import Optional

from flask import (Flask, current_app, jsonify, request,
                   send_from_directory)

from ..config import INTERVAL_NAMES, INTERVALS_MS, AppCfg
from ..feed import Feed

logger = logging.getLogger(__name__)

STATIC_DIR = __import__("pathlib").Path(__file__).resolve().parent / "static"


def _forming_1h(symbol):
    """当前还没收盘的那根 1h，直接向币安要。

    为什么不能只查本地库：1h 由聚合器产出，**只在收盘那一刻落库**。
    所以整点之后的这一整小时里，本地库里根本没有当前这根 ——
    只查本地库会让"未完成那根"永远缺失（用户看到的"8 点还只有 7 点"）。

    返回值是引擎要的 6 元组 (ts, o, h, l, c, v)；不是当前桶就返回 None，
    绝不拿上一根冒充（那会把已收盘的桶画两遍）。
    """
    if not symbol:
        return None
    try:
        from .live import _fetch_klines
        rows = _fetch_klines(symbol, "1h", 2)
        if not rows:
            return None
        r = rows[-1]
        ts = int(r[0])
        cur_open = (int(time.time() * 1000) // 3_600_000) * 3_600_000
        if ts != cur_open:
            return None
        return (ts, float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]))
    except Exception as e:
        logger.warning("取未收盘 1h 失败: %s", str(e)[:120])
        return None


def create_app(cfg: AppCfg, feed: Feed, engine=None, tdb=None, journal=None,
               guard=None, range_db=None, settings=None, store=None) -> Flask:
    app = Flask(__name__, static_folder=None)
    app.config["CFG"] = cfg
    app.config["FEED"] = feed
    app.config["ENGINE"] = engine
    app.config["TDB"] = tdb
    app.config["JOURNAL"] = journal
    app.config["GUARD"] = guard
    app.config["MARKET_STORE"] = store or getattr(feed, "store", None)

    # 空令牌 = 全裸。这在生产上真实发生过：/etc/ai-trader/env 里
    # AI_WEB_TOKEN= 后面是空的，整个看板和所有接口匿名可访问，
    # 而且**没有任何提示**——.env 里那行长得跟配好了一样。
    # 更严重的是 /api/config/sizing 能改本金/风险/杠杆：
    # 匿名 POST 一次就能把 5% 风险改成 100%。
    # 所以这里不是"允许但记一笔"，而是每次启动都把这件事喊出来。
    if not cfg.web.auth_token:
        logger.warning("=" * 62)
        logger.warning("AI_WEB_TOKEN 为空：所有 /api/* 匿名可访问，包括")
        logger.warning("  /api/config/sizing （可改本金/风险/杠杆）")
        logger.warning("  /api/guard/reset    （可清除熔断闸）")
        logger.warning("要立刻关掉这个口子：在 env 里设 AI_WEB_TOKEN=<随机串> 后重启。")
        logger.warning("=" * 62)

    def _check_token() -> bool:
        if not cfg.web.auth_token:
            return True
        tok = request.headers.get("X-Auth-Token") or request.args.get("token")
        return tok == cfg.web.auth_token

    @app.before_request
    def _require_token_for_every_api():
        """所有 /api/* 一律要令牌（第 129 轮）。

        ★ 为什么不是每个路由自己调一次 _check_token()：
          原来只有三个只读端点调了它；/api/guard/reset、/api/trade/state、
          /api/journal 等九个端点一次都没调。而 reset() 清的是
          halted_hard / halt_kind / halt_ts / temp_reasons / daily_halt_day ——
          第 122/123/127 轮做的那些闸，一个匿名 POST 就能全开。

          这是这一族的老形状：防护写好了，但没接到所有入口上
          （同第 124 轮 _close 只包了一半 try、第 125 轮哨兵只读一个表）。
          逐路由挂检查，下一个新端点照样会漏；挂在 before_request 上，
          新端点自动受保护——把「记得挂」这件事从人身上拿掉。

        / 和 /static/* 保持公开：页面本身没有秘密，令牌由用户从地址栏带进来。
        """
        if not request.path.startswith("/api/"):
            return None
        if not _check_token():
            return jsonify({"error": "unauthorized"}), 401
        return None

    @app.get("/")
    def index():
        return send_from_directory(str(STATIC_DIR), "board.html")

    @app.get("/static/<path:p>")
    def static_files(p: str):
        return send_from_directory(str(STATIC_DIR), p)

    @app.get("/api/health")
    def health():
        snap = feed.snapshot()
        return jsonify({
            "ok": True,
            "src": snap["src"],
            "symbol": snap["symbol"],
            "price": snap["price"],
            "ws_connected": snap["ws_connected"],
            # 连上但长时间无数据 = 第三种状态，不要只报 connected
            "ws_stream": snap.get("ws_stream", ""),
            "ws_silent": snap.get("ws_silent", False),
            # 被丢弃的畸形成交数：过滤必须可观测
            "ws_dropped": snap.get("ws_dropped", 0),
            "ws_idle_ms": snap["ws_idle_ms"],
            "rest_latency_ms": snap["rest_latency_ms"],
            "uptime_s": snap["uptime_s"],
            "ticks_total": snap["ticks_total"],
            "rest_ticks": snap["rest_ticks"],
            "subscribers": feed.hub.subscribers,
        })

    @app.get("/api/bars")
    def bars():
        if not _check_token():
            return jsonify({"error": "unauthorized"}), 401
        name = request.args.get("interval", "3m")
        limit = min(int(request.args.get("limit", 500)), 2000)
        before = int(request.args.get("before", 0))
        iv = next((k for k, v in INTERVAL_NAMES.items() if v == name), None)
        if iv is None:
            return jsonify({"error": "bad interval", "allowed": list(INTERVAL_NAMES.values())}), 400
        rows = feed.store.query_bars(iv, limit=limit, before=before)
        return jsonify({"interval": name, "interval_ms": iv, "bars": rows})

    @app.get("/api/events")
    def events():
        if not _check_token():
            return jsonify({"error": "unauthorized"}), 401
        return jsonify({"events": feed.store.recent_events(int(request.args.get("limit", 50)))})

    @app.get("/api/snapshot")
    def snapshot():
        if not _check_token():
            return jsonify({"error": "unauthorized"}), 401
        return jsonify(feed.snapshot())

    @app.get("/api/intervals")
    def intervals():
        return jsonify({"intervals": [{"ms": iv, "name": INTERVAL_NAMES[iv]} for iv in INTERVALS_MS]})

    # ---------------- 交易审计 ----------------
    @app.get("/api/trade/state")
    def trade_state():
        if engine is None:
            return jsonify({"error": "engine disabled"}), 404
        st = engine.state()
        if tdb is not None:
            # 按【当前部署的策略】统计：大屏头条必须是"现在在跑的策略"的战绩。
            # 以前这里不过滤，于是 4 笔已废弃剥头皮的亏损被当成系统战绩显示，
            # 而真正在跑的趋势策略 0 笔——看的人会以为系统在亏钱。
            # 同时保留 by_strategy 分解，整体和分策略都能查。
            st["stats"] = tdb.stats(cfg.trading.strategy)
        return jsonify(st)

    @app.get("/api/trade/decisions")
    def trade_decisions():
        if tdb is None:
            return jsonify({"error": "no trade db"}), 404
        return jsonify({"decisions": tdb.recent_decisions(int(request.args.get("limit", 60)))})

    @app.get("/api/trade/trades")
    def trade_trades():
        if tdb is None:
            return jsonify({"error": "no trade db"}), 404
        return jsonify({"trades": tdb.closed_trades(int(request.args.get("limit", 100)))})

    @app.get("/api/trade/equity")
    def trade_equity():
        if tdb is None:
            return jsonify({"error": "no trade db"}), 404
        return jsonify({"curve": tdb.equity_curve(int(request.args.get("limit", 1000)))})

    @app.get("/api/guard")
    def guard_state():
        if guard is None:
            return jsonify({"error": "no guard"}), 404
        ok, why = guard.can_open()
        st = guard.snapshot()
        st["can_open"] = ok
        st["block_reason"] = why
        return jsonify(st)

    @app.post("/api/guard/reset")
    def guard_reset():
        if guard is None:
            return jsonify({"error": "no guard"}), 404
        guard.reset("人工复位（看板按钮）")
        return jsonify({"ok": True})

    @app.get("/api/journal")
    def journal_list():
        if journal is None:
            return jsonify({"entries": []})
        return jsonify({"entries": journal.recent(int(request.args.get("limit", 60)))})

    # ---------------- 区间演化（v11 四线）----------------
    #
    # 页面公开、数据要令牌——与 /api/* 的既有约定一致（见 before_request）。
    @app.get("/range")
    def range_page():
        return send_from_directory(str(STATIC_DIR), "range.html")

    @app.get("/config")
    def config_page():
        return send_from_directory(str(STATIC_DIR), "config.html")

    def _range_symbol() -> str:
        return request.args.get("symbol") or cfg.trading.symbol

    @app.get("/api/range/evo")
    def range_evo():
        """一次给齐画图三件套：K 线、四线、事件。

        分三个端点会让前端要发三次请求并自己对齐时间轴，
        而这三者本来就必须落在同一个 ts 上才有意义。
        """
        if range_db is None:
            return jsonify({"error": "range_db 未注入"}), 503
        sym = _range_symbol()
        # 畸形参数必须 400，不能 500。
        # 曾因为在 query string 里拼错（?symbol=X&days=7?token=Y）就抛
        # ValueError -> 500，运维看到 500 会去查服务，而问题只在参数上。
        try:
            days = int(request.args.get("days", 14))
        except (TypeError, ValueError):
            return jsonify({"error": "days 必须是整数，收到 %r"
                            % request.args.get("days")}), 400
        days = max(1, min(days, 120))
        states = range_db.states(sym)
        bars = range_db.bars_1h(sym)
        events = range_db.events(sym)
        # 未完成的那根 1h：库里没有（故意不写，见 range/live.py），
        # 这里现算一次附在尾部，页面才能看到"跳动的最后一根"。
        tail = None
        try:
            from ..range.live import compute as _rcompute
            mk = current_app.config.get("MARKET_STORE")
            _s, _e, tail = _rcompute(range_db, mk, sym,
                                     forming=_forming_1h(sym))
        except Exception as e:
            logger.warning("算未完成 1h 失败: %s", str(e)[:120])
            tail = None
        if not states and not bars:
            return jsonify({"ok": True, "symbol": sym, "empty": True,
                            "bars": [], "states": [], "events": [],
                            "hint": "该品种暂无区间数据（先跑回填）"})
        # 只回传窗口内的数据；窗口按最后一根的 ts 往前推
        last = max([r[0] for r in states] + [r[0] for r in bars])
        since = last - days * 86400 * 1000
        return jsonify({
            "ok": True, "symbol": sym, "empty": False,
            "bars": [r for r in bars if r[0] >= since],
            "states": [r for r in states if r[0] >= since],
            "events": [r for r in events if r[0] >= since],
            "tail": tail,
        })

    @app.get("/api/range/asof")
    def range_asof():
        """给指定时间，回答「当时区间是什么」。

        when 支持 "YYYY-MM-DD HH:MM"（北京时区）或 UTC 毫秒。
        这是功能型入口：不看现在，只看那一刻。
        """
        if range_db is None:
            return jsonify({"error": "range_db 未注入"}), 503
        sym = _range_symbol()
        when = (request.args.get("when") or "").strip()
        if not when:
            return jsonify({"error": "缺少 when 参数（如 2026-10-23 00:00）"}), 400
        try:
            if when.isdigit():
                ts = int(when)
            else:
                from ..range import engine as _E
                ts = _E.lab(when)
        except Exception as e:
            return jsonify({"error": "时间格式无法解析: %s (%s)" % (when, e)}), 400
        got = range_db.state_at_dict(sym, ts)
        if got is None:
            return jsonify({"ok": False, "error": "该时间点之前没有区间数据",
                            "when": when}), 404
        return jsonify({"ok": True, "requested": when, "symbol": sym, "state": got})

    # ---------------- 仓位参数配置（实时生效）----------------
    @app.get("/api/config/sizing")
    def config_sizing_get():
        if settings is None:
            return jsonify({"error": "settings 未注入"}), 503
        s = settings.sizing()
        # 带参数时是「试算」：页面调参过程中实时预览四行派生，
        # 但仍由 Python 侧计算——绝不在 JS 里重写一遍公式（两套公式必然分叉）。
        try:
            if request.args.get("capital") is not None:
                p = settings.preview(float(request.args["capital"]),
                                     float(request.args["risk_pct"]),
                                     float(request.args["leverage"]))
            else:
                p = settings.preview(s["sizing.capital"], s["sizing.risk_pct"],
                                     s["sizing.leverage"])
        except (TypeError, ValueError, KeyError) as e:
            p = {"ok": False, "error": "试算参数不完整: %s" % e}
        return jsonify({"ok": True, "sizing": s, "derived": p,
                        "live": {"risk_pct": cfg.trading.risk_pct,
                                 "max_leverage": cfg.trading.max_leverage,
                                 "paper_balance": cfg.trading.paper_balance}})

    @app.post("/api/config/sizing")
    def config_sizing_post():
        """改三个数：本金 / 每笔风险% / 杠杆。

        校验委托 settings.validate（最终落到 SizingConfig.__post_init__），
        非法值一律 400 并原样回传原因，不静默夹取——静默夹取会让用户
        以为自己设的是 A，实际生效的是 B。
        """
        if settings is None:
            return jsonify({"error": "settings 未注入"}), 503
        body = request.get_json(silent=True) or {}
        try:
            capital = float(body.get("capital"))
            risk_pct = float(body.get("risk_pct"))
            leverage = float(body.get("leverage"))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "capital / risk_pct / leverage 必须是数字"}), 400

        ok, why = settings.set_sizing(capital, risk_pct, leverage)
        if not ok:
            return jsonify({"ok": False, "error": why}), 400
        applied = settings.apply_to(cfg.trading)
        logger.info("仓位参数已改并生效: %s", applied)
        p = settings.preview(capital, risk_pct, leverage)
        return jsonify({"ok": True, "sizing": applied, "derived": p,
                        "live": {"risk_pct": cfg.trading.risk_pct,
                                 "max_leverage": cfg.trading.max_leverage,
                                 "paper_balance": cfg.trading.paper_balance}})

    # ---------- 后台（带登录）----------
    # 与公开的 /、/range、/config 并存，互不影响：
    # 公开页靠 ?token=，后台靠 session。换掉根路径会让运维正在用的页面当场失效，
    # 所以先加、再切，两步走。
    from .auth import auth_bp
    from .dashboard import dash_bp
    from .live import live_bp

    app.secret_key = (os.environ.get("AI_SECRET_KEY")
                      or cfg.web.auth_token
                      or "ai-trader-dev-only-secret")
    if not os.environ.get("AI_SECRET_KEY") and not cfg.web.auth_token:
        logger.warning("AI_SECRET_KEY 与 AI_WEB_TOKEN 都为空，session 用开发默认密钥——"
                       "任何人都能伪造登录态，生产必须设置其中之一")

    app.config["CFG"] = cfg
    app.config["FEED"] = feed
    app.config["TDB"] = tdb
    app.config["GUARD"] = guard
    app.config["SETTINGS"] = settings
    app.config["RANGE_DB"] = range_db

    app.register_blueprint(auth_bp)
    app.register_blueprint(dash_bp)
    app.register_blueprint(live_bp)

    def _sizing_view(s):
        """把 4 个仓位参数算成页面要显示的全部派生量。

        必须是服务端算：页面显示的数和闸门实际用的数，
        一定要出自同一份 sizing.derive()。前端重算一遍迟早分叉。
        """
        try:
            from ..trading.sizing import SizingConfig, derive
            c = SizingConfig(capital=float(s.get("sizing.capital", 0) or 0),
                             risk_pct=float(s.get("sizing.risk_pct", 0) or 0),
                             leverage=float(s.get("sizing.leverage", 0) or 0))
            d = derive(c)
            return {"capital": c.capital, "risk_pct": c.risk_pct,
                    "leverage": c.leverage,
                    "risk_per_trade": d.risk_per_trade,
                    "liq_distance_pct": d.liq_distance_pct,
                    "max_stop_distance_pct": d.max_stop_distance_pct,
                    "margin_cap_amount": d.margin_cap_amount,
                    "lives": d.lives}
        except Exception:
            # 参数非法时页面不能瞎算，也不该 500。给一组 0 并让模板显示出来。
            logger.warning("仓位派生计算失败", exc_info=True)
            return {"capital": 0.0, "risk_pct": 0.0, "leverage": 0.0,
                    "risk_per_trade": 0.0, "liq_distance_pct": 0.0,
                    "max_stop_distance_pct": 0.0, "margin_cap_amount": 0.0,
                    "lives": 0}

    @app.context_processor
    def _inject_globals():
        """模板里统一可用的几个值，避免每个视图重复传。"""
        s = {}
        try:
            if settings is not None:
                s = settings.sizing()
        except Exception:
            s = {}
        return {
            "cfg_token": getattr(cfg.web, "auth_token", "") or "",
            "shop_capital": s.get("sizing.capital", 0.0),
            "shop_risk": s.get("sizing.risk_pct", 0.0),
            "shop_lev": s.get("sizing.leverage", 0.0),
            "shop_lives": int(s["sizing.capital"] // max(1.0, s["sizing.capital"]
                            * s["sizing.risk_pct"] / 100.0))
                          if s.get("sizing.capital") else 0,
            "sz": _sizing_view(s),
            "shop_mode": cfg.trading.mode,
            "shop_strategy": cfg.trading.strategy,
            "shop_market": cfg.trading.exec_market,
        }

    return app