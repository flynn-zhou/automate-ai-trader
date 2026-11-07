# -*- coding: utf-8 -*-
"""打开 AI_WEB_TOKEN 之后，看板自己必须能带着令牌跑（第 129 轮）。

要守的不变量
------------
1. 页面从地址栏 ?token=xxx 拿到令牌后，每一次 /api/ 取数都要带上它。
2. WebSocket 连接也要带上它。
3. 没给令牌时不乱加参数（不能污染正常 URL）。

为什么这条必须和「加上鉴权」一起做
--------------------------------
只加上鉴权、不给前端携带方式 = 用户打开页面看到一片空白，
而且【没有任何报错】——每个 fetch 都 401，`.catch(function(){})` 把它们全吞了。

这正是第 121 轮那个形状：**结构性不可用的安全措施，比没有措施更糟**。
所以这两件事必须一次做完，而且都要有测试。
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
TOK = "tok-abc-123"

_SHIM = r"""
const els = {};
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
global.location = { protocol: "http:", hostname: "board.local", port: "8090",
                    host: "board.local:8090", search: process.argv[2] || "" };
global.navigator = { userAgent: "node" };
global.requestAnimationFrame = function () { return 0; };
global.setInterval = function () { return 0; };
global.setTimeout = function () { return 0; };
global.__FETCHED__ = [];
global.__WSURLS__ = [];
global.fetch = function (u) {
  global.__FETCHED__.push(String(u));
  return Promise.resolve({ ok: true, status: 200,
    json: function () { return Promise.resolve({}); },
    text: function () { return Promise.resolve(""); } });
};
global.WebSocket = function (u) {
  global.__WSURLS__.push(String(u));
  return { send() {}, close() {}, addEventListener() {} };
};
global.onerror = null;
"""

_DRIVER = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[3], "utf8");
const tag = "})();";
const cut = src.lastIndexOf(tag);
const EXPORT = "\n  global.__BOARD__ = { pollTrade: typeof pollTrade === 'function' ? pollTrade : null };\n";
const patched = cut < 0 ? src : (src.slice(0, cut) + EXPORT + src.slice(cut));
let evalErr = null;
try { eval(patched); } catch (e) { evalErr = e.message; }
const B = global.__BOARD__ || {};
if (typeof B.pollTrade === "function") {
  try { B.pollTrade(); } catch (e) { evalErr = (evalErr || "") + " pollTrade:" + e.message; }
}
console.log(JSON.stringify({
  fetched: global.__FETCHED__,
  ws: global.__WSURLS__,
  err: evalErr,
}));
"""


def _run(search=""):
    exe = shutil.which("node")
    if not exe:
        pytest.skip("本机没有 node，无法真正执行 board.js")
    tmp = tempfile.mkdtemp()
    driver_p = os.path.join(tmp, "driver.js")
    with open(driver_p, "w", encoding="utf-8") as f:
        f.write(_SHIM + _DRIVER)
    r = subprocess.run([exe, driver_p, search, BOARD_JS],
                       capture_output=True, timeout=60)
    out = (r.stdout or b"").decode("utf-8", "replace")
    err = (r.stderr or b"").decode("utf-8", "replace")
    last = [ln for ln in out.strip().splitlines() if ln.strip().startswith("{")]
    assert last, u"driver 没有输出：stdout=%r stderr=%r" % (out[:300], err[:300])
    return json.loads(last[-1])


def test_every_api_call_carries_the_token_from_the_address_bar():
    """★ 核心：地址栏带 ?token= 之后，每一次取数都要带上它。

    不带的话：每个 fetch 都 401，而 board.js 里的
    `.catch(function(){})` 会把九个失败全部吞掉 ——
    用户看到的是一片空白，且没有任何错误提示。
    """
    out = _run("?token=" + TOK)
    assert not out["err"], u"board.js 执行时报错：%s" % out["err"]
    apis = [u for u in out["fetched"] if "/api/" in u]
    assert apis, u"board.js 一次 /api/ 都没取（探针本身失效）"
    bad = [u for u in apis if ("token=" + TOK) not in u]
    assert not bad, (
        u"这些取数没带令牌，打开鉴权后它们会全部 401：%r" % (bad,))


def test_the_websocket_also_carries_the_token():
    """★ WebSocket 也要带：ws_server.py 的鉴权读的是 query 里的 token。"""
    out = _run("?token=" + TOK)
    assert out["ws"], u"完全没有建立 WebSocket（探针本身失效）"
    bad = [u for u in out["ws"] if ("token=" + TOK) not in u]
    assert not bad, (
        u"WebSocket 没带令牌：%r —— 打开鉴权后连不上，看板永远停在「连接中…」"
        % (bad,))


def test_no_token_means_no_token_parameter():
    """★ 反向：没给令牌时不能往 URL 里乱塞参数。"""
    out = _run("")
    assert not out["err"], u"board.js 执行时报错：%s" % out["err"]
    dirty = [u for u in out["fetched"] + out["ws"] if "token=" in u]
    assert not dirty, (
        u"没设令牌却往 URL 里塞了 token=：%r" % (dirty,))
