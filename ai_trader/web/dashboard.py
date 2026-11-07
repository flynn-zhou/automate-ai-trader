# -*- coding: utf-8 -*-
"""后台：总览 / 区间演化 / 仓位配置（带登录）。

为什么挂在 /admin/* 而不是直接占掉 /
-------------------------------------
现有 /、/range、/config 是免登录的只读快速查看页（靠 ?token=），
运维正靠它看行情。把根路径换掉会让它们当场失效。
所以后台先独立挂在 /admin，两者并存；确认后台够用了再把根路径指过来。
这样"加东西"和"换东西"分两步，出问题能退回。

数据全部从 current_app.config 取（create_app 注入的那几个），
不在这个模块里重新开库——避免又出现"两个连接、两套口径"。
"""
from __future__ import annotations

import logging

from flask import (Blueprint, current_app, redirect, render_template, request,
                   session, url_for)

from .auth import login_required

logger = logging.getLogger(__name__)

dash_bp = Blueprint("dash", __name__, url_prefix="/admin")


def _pack_url():
    """数据包端点的完整路径。用 url_for 而不是手写字符串：
    蓝图前缀改了这里会自动跟上，手写就会静默 404。"""
    return url_for("live.pack_api")


def _cfg(name):
    return current_app.config.get(name)


@dash_bp.get("/")
@login_required
def home():
    """总览：把"现在值不值得看"的几件事摆在一屏。"""
    feed = _cfg("FEED")
    tdb = _cfg("TDB")
    guard = _cfg("GUARD")
    cfd = _cfg("CFG")

    snap = {}
    try:
        snap = feed.snapshot() if feed is not None else {}
    except Exception as e:
        logger.warning("总览取快照失败: %s", str(e)[:100])

    cards = []
    if cfd is not None:
        cards = cfd.sizing_preview() if hasattr(cfd, "sizing_preview") else []
    return render_template("dash_home.html", snap=snap, cards=cards,
                           cfg=cfd, guard=guard, tdb=tdb,
                           pack_url=_pack_url(), default_interval="3m")


@dash_bp.get("/live")
@login_required
def live_page():
    """准实时行情独立页。

    内容已并入总览（导航里不再单独列出），但路由保留：
    旧书签和外部直链不至于 404。两处共用同一份模板片段，不会分叉。
    """
    return render_template("dash_live.html",
                           pack_url=_pack_url(), default_interval="3m")


@dash_bp.get("/range")
@login_required
def range_page():
    token = ""
    cfd = _cfg("CFG")
    if cfd is not None:
        token = getattr(cfd.web, "auth_token", "") or ""
    return render_template("dash_range.html", token=token)


@dash_bp.post("/config")
@login_required
def config_save():
    settings = _cfg("SETTINGS")
    cfd = _cfg("CFG")
    if settings is None or cfd is None:
        return render_template("dash_config.html", error="配置存储未注入",
                               settings={}, derived=None), 503
    try:
        capital = float(request.form.get("capital", ""))
        risk = float(request.form.get("risk_pct", ""))
        lev = float(request.form.get("leverage", ""))
    except (TypeError, ValueError):
        return render_template("dash_config.html",
                               error="三个参数都要填数字",
                               settings=settings.sizing(),
                               derived=settings.preview(
                                   settings.sizing()["sizing.capital"],
                                   settings.sizing()["sizing.risk_pct"],
                                   settings.sizing()["sizing.leverage"])), 400
    ok, why = settings.set_sizing(capital, risk, lev)
    if not ok:
        # 原样把理由显示出来，不静默取整、不静默夹紧
        return render_template("dash_config.html", error=why,
                               settings=settings.sizing(),
                               derived=settings.preview(capital, risk, lev)), 400
    settings.apply_to(cfd.trading)
    logger.info("后台改仓位参数: %.2fU / %.2f%% / %.1fx", capital, risk, lev)
    d = settings.preview(capital, risk, lev)
    return render_template("dash_config.html", saved="已保存并立即生效",
                           settings=settings.sizing(), derived=d)


@dash_bp.get("/config")
@login_required
def config_page():
    settings = _cfg("SETTINGS")
    if settings is None:
        return render_template("dash_config.html", error="配置存储未注入",
                               settings={}, derived=None), 503
    s = settings.sizing()
    d = settings.preview(s["sizing.capital"], s["sizing.risk_pct"],
                         s["sizing.leverage"])
    return render_template("dash_config.html", settings=s, derived=d)