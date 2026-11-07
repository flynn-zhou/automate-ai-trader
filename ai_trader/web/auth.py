# -*- coding: utf-8 -*-
"""后台登录：用户名 + 密码 + Flask session。

与 AI_WEB_TOKEN 的分工（这是刻意分成两套的）
--------------------------------------------
    AI_WEB_TOKEN -> 机器用：/api/* 的每次请求都带，无状态，可轮换
    登录 session -> 人用：浏览器输一次，之后靠 cookie，页面之间跳转不再带 token

不能只留一套的原因：只留 token，人每点一个链接都要保证地址栏有 ?token=，
跳转一次丢一次；只留 session，脚本/看板拿不到数据。两套各管一头。

安全默认：AI_WEB_PASS 没配就**登不进去**（而不是默认放行）。
默认口令比没有口令更危险——它给了人"我配过了"的错觉。
"""
from __future__ import annotations

import logging
import os
from functools import wraps

from flask import (Blueprint, redirect, render_template, request, session,
                   url_for)

logger = logging.getLogger(__name__)

auth_bp = Blueprint("auth", __name__)


def credentials() -> tuple:
    """(用户名, 密码)。密码为空 = 后台登录被禁用。"""
    return (os.environ.get("AI_WEB_USER", "admin"),
            os.environ.get("AI_WEB_PASS", ""))


def login_required(view):
    @wraps(view)
    def wrapper(*a, **kw):
        if not session.get("logged_in"):
            return redirect(url_for("auth.login", next=request.path))
        return view(*a, **kw)
    return wrapper


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    user, pwd = credentials()
    if not pwd:
        # 明确告诉人为什么进不去，而不是让他反复试密码
        return render_template("login.html", disabled=True), 503
    if request.method == "POST":
        if (request.form.get("username") == user
                and request.form.get("password") == pwd):
            session["logged_in"] = True
            session.permanent = False
            nxt = request.args.get("next") or url_for("dash.home")
            # 只允许站内跳转，避免 ?next=https://evil 变成开放重定向
            if not nxt.startswith("/"):
                nxt = url_for("dash.home")
            return redirect(nxt)
        logger.warning("后台登录失败 user=%r ip=%s",
                       request.form.get("username"), request.remote_addr)
        return render_template("login.html", error="用户名或密码错误"), 401
    return render_template("login.html")


@auth_bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))
