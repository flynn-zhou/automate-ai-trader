# -*- coding: utf-8 -*-
"""看板的渲染结果必须【真的跑一遍 JS】来验，不能只 grep 源码。

为什么改这里的检测方式（第 118 轮）：
  test_board_field_wiring.py 靠正则读 board.js 源码，判断某个字段
  "是不是条件渲染"。这条判据很脆：一个字段可能写在一行里被条件包着，
  也可能被提到变量里。我第一版就是被它误判过的 ——
  同一行 html: (g.temp_block || g.block_reason || "原因未提供")
  明明整行在 else if (guardBlocked) 里面，正则却看不出这个上下文。

  源码匹配只能回答「这个字段被提到了吗」，
  回答不了「健康状态下屏幕上到底出现了什么」。

所以干脆把真的 board.js 拉起来跑（本机有 node）：
给它两份 state（一份健康、一份各类闸都触发），
看 DOM 里最后出现的是哪些字。这比任何正则都硬。

如果机器上没有 node，就显式 skip —— 不能悄悄当成通过。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOARD_JS = os.path.join(ROOT, "ai_trader", "web", "static", "board.js")

_SHIM = r"""
const els = {};
// 画布上下文垫片：board.js 会画 K 线，这里只需要它不炸。
function mkCtx() {
  const noop = function () {};
  return new Proxy({}, {
    get(t, k) {
      if (k === "canvas") return { width: 300, height: 150 };
      if (k === "measureText") return function () { return { width: 10 }; };
      if (k === "createLinearGradient") return function () { return { addColorStop: noop }; };
      return t[k] !== undefined ? t[k] : noop;
    },
    set(t, k, v) { t[k] = v; return true; },
  });
}
function mkEl(id) {
  const e = {
    id: id, _html: "", _text: "", className: "", style: {},
    get innerHTML() { return this._html; },
    set innerHTML(v) { this._html = String(v); },
    get textContent() { return this._text; },
    set textContent(v) { this._text = String(v); },
    appendChild() {}, addEventListener() {}, setAttribute() {},
    querySelector() { return null; }, querySelectorAll() { return []; },
    getContext() { return mkCtx(); }, remove() {},
    getBoundingClientRect() {
      return { top: 0, left: 0, right: 100, bottom: 100,
               width: 100, height: 100, x: 0, y: 0 };
    },
    focus() {}, blur() {}, scrollIntoView() {},
    parentElement: null, parentNode: null, children: [],
  };
  e.parentElement = e;
  e.parentNode = e;
  els[id] = e;
  return e;
}
global.document = {
  __els: els,
  getElementById(id) { if (!els[id]) mkEl(id); return els[id]; },
  querySelector() { return null; },
  querySelectorAll() { return []; },
  addEventListener() {},
  createElement() { return mkEl("tmp"); },
  body: null,
};
global.window = global;
global.addEventListener = function () {};
global.removeEventListener = function () {};
global.location = { protocol: "http:", host: "127.0.0.1:8090", search: "" };
global.navigator = { userAgent: "node" };
global.requestAnimationFrame = function () { return 0; };
global.setInterval = function () { return 0; };
global.setTimeout = function () { return 0; };
global.fetch = function () {
  return Promise.resolve({ ok: true, status: 200,
    json: function () { return Promise.resolve({}); },
    text: function () { return Promise.resolve(""); } });
};
global.WebSocket = function () {
  return { send() {}, close() {}, addEventListener() {} };
};
global.onerror = null;
"""

_DRIVER = r"""
const fs = require("fs");
const STATE = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
let src = fs.readFileSync(process.argv[3], "utf8");
// board.js 整个包在一个 IIFE 里，外面拿不到里面的函数。
// 所以把导出语句【插到 IIFE 结束前】——这是唯一能拿到 renderTrend 的位置。
// （在 IIFE 外面 eval 一段导出语句是没用的：那是另一个作用域。）
const tag = "})();";
const cut = src.lastIndexOf(tag);
if (cut < 0) { console.error("EVAL_ERR 找不到 IIFE 结束标记"); }
const EXPORT = "\n  global.__BOARD__ = {" +
  "renderTrend: typeof renderTrend === 'function' ? renderTrend : null," +
  "renderPosition: typeof renderPosition === 'function' ? renderPosition : null" +
  "};\n";
const patched = cut < 0 ? src : (src.slice(0, cut) + EXPORT + src.slice(cut));
try { eval(patched); } catch (e) { console.error("EVAL_ERR " + e.message); }
const B = global.__BOARD__ || {};
let called = false;
// 只调【因果链】那一个渲染函数就够了。
// 不调 applyState：它还会去画 K 线、拉 REST，而这里要验的是
// 「安全闸那几步到底画没画出来」，跟画布无关。
const errors = [];
if (typeof B.renderTrend === "function") {
  try { B.renderTrend(STATE); called = true; }
  catch (e) { errors.push("renderTrend: " + e.message); }
} else {
  errors.push("renderTrend 没导出");
}
if (typeof B.renderPosition === "function") {
  try { B.renderPosition(STATE); }
  catch (e) { errors.push("renderPosition: " + e.message); }
}
const seen = [];
const doc = global.document;
if (doc && doc.__els) {
  for (const k of Object.keys(doc.__els)) {
    const e = doc.__els[k];
    if (e._html) seen.push(e._html);
    if (e._text) seen.push(e._text);
  }
}
console.log(JSON.stringify({ called: called, seen: seen, errors: errors }));
"""


def _render_with_errors(state):
    """同 _render，但把渲染过程中的异常也带回来。

    只收集文本是不够的：一个渲染函数可能抛了异常，
    但在此之前已经往某个元素写过半截 HTML，
    于是"有输出"和"渲染成功"是两件事。
    """
    out = _render(state)
    out.setdefault("errors", [])
    return out


def _render(state):
    """用真的 board.js 渲染一份 state，返回屏幕上出现的全部文本。"""
    exe = shutil.which("node")
    if not exe:
        pytest.skip("本机没有 node，无法真正执行 board.js")
    tmp = tempfile.mkdtemp()
    state_p = os.path.join(tmp, "state.json")
    driver_p = os.path.join(tmp, "driver.js")
    with open(state_p, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    with open(driver_p, "w", encoding="utf-8") as f:
        f.write(_SHIM + _DRIVER)
    r = subprocess.run([exe, driver_p, state_p, BOARD_JS],
                       capture_output=True, timeout=60)
    out = (r.stdout or b"").decode("utf-8", "replace")
    err = (r.stderr or b"").decode("utf-8", "replace")
    assert "EVAL_ERR" not in err, u"board.js 语法/初始化就挂了：%s" % err[:400]
    last = [ln for ln in out.strip().splitlines() if ln.strip().startswith("{")]
    assert last, u"driver 没有任何输出：stdout=%r stderr=%r" % (out[:300], err[:300])
    return json.loads(last[-1])


def _base_state():
    return {
        "equity": 100.0, "decisions": 1, "block_stats": {},
        "mode": "paper",
        "pos": None,
        "risk": {"day_pnl": 0.0, "consecutive_losses": 0,
                 "cooldown_until": 0, "trades_today": 0},
        "executor": {"name": "paper", "halted": False, "reason": "",
                     "close_fail_count": 0},
        "guard": {"can_open": True, "block_reason": "",
                  "halted_hard": False, "halt_reason": "", "temp_block": "",
                  "drawdown_pct": 0.0, "peak_equity": 100.0, "equity": 100.0,
                  "day_start_equity": 100.0,
                  "rules": {"max_drawdown_pct": 60.0}},
        "trend": {"ready": True, "mode": "trend", "above": False,
                  "bars": 419, "price": 78000.0, "dist_pct": -2.5,
                  "sma": 80000.0, "trigger": 80160.0, "need_pct": 5.0,
                  "hyst_pct": 0.2, "lookback": 200, "pos": None,
                  "next_bar_h": 2.0, "target_leverage": 1.0,
                  "plan_notional": 71.0, "plan_qty": 0.00091},
        "last_error": "", "last_error_ts": 0,
    }


def test_the_board_js_actually_runs():
    """先证明 board.js 能被真的执行——否则后面全是假绿。"""
    out = _render(_base_state())
    assert out["called"], u"没有找到可以喂 state 的入口函数"


def test_a_healthy_board_shows_no_alarm():
    """★ 一切正常时，屏幕上不能出现任何警报。

    这条只有真跑 JS 才验得准：正则看不出"这一行在不在分支里"。
    """
    out = _render(_base_state())
    blob = " ".join(out["seen"])
    # 只查【警报句式】，不查单个词。
    # 第一版查的是「硬停」「熔断」，结果被健康路径上的
    # 「安全闸放行，回撤 0.0%（回撤熔断线 60%）」误伤——
    # 那句说的是【阈值】，不是【状态】。查词不管上下文，就会这样。
    for phrase in (u"<b>硬停", u"止损通道异常", u"安全闸拦截", u"执行器已熔断"):
        assert phrase not in blob, (
            u"健康状态下屏幕上出现了警报「%s」：%s" % (phrase, blob[:600]))


def test_a_hard_halt_says_it_will_not_recover_by_itself():
    """硬停必须说出来「要人工复位」。

    临时闸会自己好，硬停不会。这一条不说清楚，
    用户会一直等一个永远不会恢复的状态。
    """
    st = _base_state()
    # halt_reason 里【不能自己带"人工/复位"字样】。
    # 我第一版写的是「等待人工复盘」，于是断言被【我自己的测试数据】满足了，
    # 把 board.js 里的"（需人工复位 /api/guard/reset）"整句删掉，
    # 9 条测试照样全绿（变异存活，但原因是我把答案写进了题目）。
    st["guard"].update({"can_open": False,
                        "halted_hard": True,
                        "halt_reason": u"从峰值 100.00U 回撤 61.0%（>60%）",
                        "drawdown_pct": 61.0})
    blob = " ".join(_render(st)["seen"])
    assert u"硬停" in blob, u"硬停状态没有显示：%s" % blob[:500]
    assert (u"人工" in blob) or (u"复位" in blob) or ("reset" in blob), (
        u"没有告诉用户「要保持人工复位」——他会一直等它自己好：%s" % blob[:500])


def test_a_broken_stop_channel_is_visible():
    """止损失效必须出现在屏幕上（第 116 轮的后端字段）。"""
    st = _base_state()
    st["executor"]["close_fail_count"] = 6
    st["guard"].update({"can_open": False,
                        "temp_block": u"止损通道失效：平仓已连续失败 6 次，"
                                      u"仓位保护不住，禁止开新仓"})
    blob = " ".join(_render(st)["seen"])
    assert u"止损通道异常" in blob, u"止损失效没有显示：%s" % blob[:500]
    assert u"6" in blob


def test_a_transient_block_shows_the_backend_reason_verbatim():
    """临时闸显示的是后端原话，不是前端自己编的一句。"""
    reason = u"数据源 DOWN 已 33s，禁止开仓"
    st = _base_state()
    st["guard"].update({"can_open": False, "temp_block": reason,
                        "block_reason": reason})
    blob = " ".join(_render(st)["seen"])
    assert reason in blob, u"临时闸原因没有原样显示：%s" % blob[:500]
