# -*- coding: utf-8 -*-
"""总览 / 区间演化的两个前端 bug，以及"给总览加 5m"的那条边界。

## 这两个 bug 是同一个根因

总览图 `_chart_js.html` 每 3 秒做一次 `chart.setOption(opt, true)`：

  - 第二个参数 `true` = `notMerge`，**整个 option 被替换**。
    替换会连带销毁 dataZoom 状态和 tooltip 实例。
  - 而 `dataZoom[0]` 的 `start`/`end` 写的是 `Math.max(0, 100-12000/n)`，
    是"尾部 120 根"这个**算出来的默认值**，不是用户当前拖到的位置。

于是每个轮询周期都发生一次：
  用户拖到第 300 根 → 下一轮 setOption → 窗口被重置回尾部 → 画面跳回最新。

同理，鼠标一移开 tooltip 就消失得快：tooltip 对象本身就是被销毁重建的那个。
用户估的"大概 3 秒"是对的 —— 3 秒正是 `POLL_MS`。

所以修法必须是**两条**，缺一不可：
  1. 数据没变化就根本不重建（`sig` 短路）—— 悬停期间一次都不重建，这是根治；
  2. 真要重建时把用户的窗口还回去（`zwin`），并把 `hideDelay` 调到 300ms。
     只做第 2 条，鼠标仍会在重建时被打断；只做第 1 条，
     数据一更新就跳回尾部（新 K 线来时）。

`dash_range.html` 本来就已经保存了 dataZoom（`prevOpt.dataZoom[0]`），
所以它**不跳**；但它同样每 3 秒重建，所以 tooltip 一样短命 —— 两处都要补。

## 为什么这里只做源码级断言

这一层是浏览器里的 ECharts 行为，Python 里没有可跑的替身。
硬造一个 DOM/ECharts 的假实现，测的就变成那个假实现，不是真页面。
所以这里的定位很明确：**钉住"修复还在"，不冒充"行为已验证"**。
真正的行为验证靠人打开页面看（`.iv` 按钮 / 拖动 / 悬停），
以及 `test_inline_js_parses` 这条把白屏类的错挡在提交前。

数字类的性质（窗口百分比公式）反过来是可以算的，见
`test_default_zoom_window_means_last_120_bars`。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

TPL = os.path.join(ROOT, "ai_trader", "web", "templates")


def _read(name):
    p = os.path.join(TPL, name)
    assert os.path.exists(p), "模板不见了：%s" % p
    with open(p, encoding="utf-8") as f:
        return f.read()


# ------------------------------------------------------- 总览：缩放不许跳
def test_overview_remembers_user_zoom():
    src = _read("_chart_js.html")
    assert "zwin" in src, "总览图没有保存用户缩放窗口 —— 拖到历史会跳回最新"
    # dataZoom 的 start/end 必须优先取 zwin，而不是无条件用算出来的默认值
    m = re.search(r"dataZoom:\s*\[(.*?)\]\s*,?\s*\n\s*series", src, re.S)
    assert m, "找不到 dataZoom 配置块"
    blk = m.group(1)
    assert "zwin.start" in blk and "zwin.end" in blk, \
        "dataZoom 没有用 zwin 恢复窗口：\n%s" % blk[:400]
    assert "chart.on(\"datazoom\"" in src, "没有监听 datazoom 事件，窗口记不下来"


def test_overview_splits_axis_and_data_signatures():
    """签名必须分两段：轴身份 / 最后一根 ohlc。

    ⚠ 这条测试的历史：第一版查的是 `if (newsig === sig)` 这个字面串，
    它是绿的，但功能没生效 —— 因为签名里带了未收盘那根的**收盘价**，
    而那个值每轮都在变，短路永远不触发。
    所以这里不再查"有没有短路"，改查"签名是否只由轴身份决定"。
    真正验行为的是 `test_web_tooltip_liveness.py`。
    """
    src = _read("_chart_js.html")
    assert "axisSig" in src and "dataSig" in src, "签名没有分成轴/数据两段"
    m = re.search(r'var axisSig\s*=\s*([^;]+);', src)
    assert m, "找不到 axisSig 的赋值"
    expr = m.group(1)
    assert "cur" in expr and "length" in expr, "axisSig 没包含周期/根数：%s" % expr
    # 轴身份**不许**含收盘价（lb[...][2] / lastBar[2]）——那正是假修的根源
    assert "[2]" not in expr, (
        "axisSig 里出现了下标 [2]（收盘价）。未收盘那根的收盘价每轮都在变，"
        "放进签名等于短路永不触发，tooltip 会每 3 秒被杀一次：%s" % expr)


def test_overview_incremental_update_stays_minimal():
    """增量更新只许带 series，不许带 tooltip / dataZoom / grid。

    带了就等于让 ECharts 去更新这几个组件；tooltip 每次更新都会重绑，
    悬停就断。这是"数据在动"和"悬停不被打断"能同时成立的唯一办法。
    """
    src = _read("_chart_js.html")
    m = re.search(r"if \(axisChanged\)\s*\{(.*?)\}\s*else\s*\{(.*?)\n    \}",
                  src, re.S)
    assert m, "找不到 axisChanged 的两条分支"
    full_branch, merge_branch = m.group(1), m.group(2)
    assert ", true)" in full_branch, "轴变了没有走全量重建（notMerge）"

    # 先剥掉注释再查关键字：注释里必然会提到 tooltip/dataZoom
    # （那段注释就是在解释"为什么不许带它们"）。
    code_only = "\n".join(
        ln for ln in merge_branch.split("\n")
        if not ln.strip().startswith("//"))
    assert "series: patch" in code_only, "增量分支没有只传 series"
    for bad in ("tooltip", "dataZoom", "grid:", "xAxis"):
        assert bad not in code_only, (
            "增量分支里出现了 %r —— 会把对应组件重建，悬停就断" % bad)
    # series 必须带 id，否则合并时可能按索引错配
    assert 'id: "k"' in src, "K 线 series 没有 id"
    assert 'id: "EMA" + per' in src, "EMA series 没有 id"


def test_overview_tooltip_hide_delay():
    src = _read("_chart_js.html")
    assert "hideDelay" in src, "总览图 tooltip 没设 hideDelay（默认 100ms，一移开就没）"
    m = re.search(r"hideDelay:\s*(\d+)", src)
    assert m and int(m.group(1)) >= 300, \
        "hideDelay 太小：%s" % (m.group(1) if m else "缺失")


def test_range_page_tooltip_hide_delay():
    src = _read("dash_range.html")
    assert "hideDelay" in src, "区间演化 tooltip 没设 hideDelay"
    m = re.search(r"hideDelay:\s*(\d+)", src)
    assert m and int(m.group(1)) >= 300


def test_range_page_does_not_rebuild_when_data_unchanged():
    src = _read("dash_range.html")
    assert "lastSig" in src, "区间演化没有数据签名短路"
    m = re.search(r"if \(sigNow === lastSig\)", src)
    assert m, "没有 `if (sigNow === lastSig)` 短路"
    assert m.start() < src.index("chart.setOption"), "短路写在 setOption 之后"
    # 换品种必须能强制重绘，否则屏幕上留着上一个品种
    assert "forceLoad" in src
    assert "lastSig = null" in src


def test_default_zoom_window_means_last_120_bars():
    """默认窗口是"尾部 120 根"。

    `100 - 12000/n` 就是 `100 - 100*120/n`（差一个 100 倍，靠 n 的取值碰对了），
    所以它本身**不是** bug —— 这里把这条算式钉住，免得后来的人
    以为它是错的顺手改掉，反而把默认视野改坏。
    """
    for n in (200, 800, 1500, 3000):
        start_old = max(0, 100 - 12000 / n)
        start_new = max(0, 100 - 100 * 120 / n)
        assert abs(start_old - start_new) < 1e-9


# --------------------------------------------------- 区间演化：本来就存了窗口
def test_range_page_still_preserves_zoom():
    """回归保护：区间演化原来就有这个能力，别在改 tooltip 时弄丢。"""
    src = _read("dash_range.html")
    assert "getOption" in src, "区间演化丢了「从当前 option 取窗口」的逻辑"
    assert "z && z[0]" in src or "z ? z[0]" in src, "窗口没被用回 dataZoom"


# ---------------------------------------------------------------- 5m 周期
def test_5m_is_allowed_for_the_live_readonly_endpoint():
    from ai_trader.web.live import ALLOWED_INTERVALS
    assert ALLOWED_INTERVALS.get("5m") == 300_000


def test_5m_is_now_a_native_aggregator_interval():
    """5m 在 2026-11-09 由用户确认加入 `INTERVALS_MS`（原生周期）。

    ⚠ 这条测试**原来钉的是相反的结论**（"5m 不许进 INTERVALS_MS"），
    当时加它是因为"总览加个 5m 按钮"很容易被顺手做成"让生产开始产 5m"，
    那是两件事。现在用户明确决定要后者，所以护栏方向反过来：
    钉住 5m **在**列表里，同时继续钉住那条真正不能破的不变量 ——
    **3s 必须是原子周期**（`agg.py` 的断言靠它）。

    把方向存下来，是因为下一个看代码的人会问"为什么 5m 能进来而别的不能"：
    答案不是"5m 特殊"，而是"它被显式评估过了，且 3s 的原子性没动"。
    """
    from ai_trader.config import INTERVAL_NAMES, INTERVALS_MS

    assert 300_000 in INTERVALS_MS, "5m 被从聚合周期里拿掉了？"
    assert INTERVAL_NAMES[300_000] == "5m"
    # 真正的不变量：3s 必须是第一个（agg.py L60 断言 3000 in intervals）
    assert INTERVALS_MS[0] == 3000, "3s 必须是原子周期且排第一"
    # 必须严格递增，否则 agg 建桶会撞车
    assert INTERVALS_MS == sorted(set(INTERVALS_MS)), "INTERVALS_MS 不是严格递增"
    # 每个周期都要有名字（前端按名字查毫秒）
    assert set(INTERVALS_MS) == set(INTERVAL_NAMES), \
        "INTERVALS_MS 与 INTERVAL_NAMES 不同步"


def test_overview_has_5m_button_and_source_switch():
    src = _read("dash_home.html")
    assert 'data-iv="5m"' in src, "总览没有 5m 按钮"
    assert 'id="src-live"' in src and 'id="src-evo"' in src, "没有数据源切换"


def _classes_in(block):
    """块里所有 class="..." 的**分词集合**（按空格切开）。"""
    out = set()
    for m in re.finditer(r'class="([^"]*)"', block):
        for tok in m.group(1).split():
            out.add(tok)
    return out


def test_evo_buttons_do_not_collide_with_live_buttons():
    """演化面板的周期按钮必须**不能**带 `.iv` 这个 class。

    `_chart_js.html` 用 `document.querySelectorAll(".iv")` 绑定点击，
    演化按钮如果也带 `iv`，点它会去轮询实时接口 —— 而且因为它在另一个
    隐藏面板里，表现是"点了没反应"，最难查的那种。

    ⚠ 这条必须按 **class 分词**判断，不能查子串：
      `class="iv evo-iv"` 里含 "evo-iv"，查子串会漏掉它。
      （第一版就是这么写的，实测把 `iv` 加回去仍然全绿 —— 假测试。）
    """
    home = _read("dash_home.html")
    evo = _read("_evo_js.html")

    live_blk = re.search(r'id="iv-live".*?</span>', home, re.S)
    assert live_blk, "找不到 #iv-live 块"
    live_cls = _classes_in(live_blk.group(0))
    assert "iv" in live_cls, "实时周期按钮没有 .iv，_chart_js 绑不上"
    assert "evo-iv" not in live_cls, "实时按钮混进了 .evo-iv"

    evo_blk = re.search(r'id="iv-evo".*?</span>', home, re.S)
    assert evo_blk, "找不到 #iv-evo 块"
    evo_cls = _classes_in(evo_blk.group(0))
    assert "evo-iv" in evo_cls, "演化周期按钮没有 .evo-iv"
    assert "iv" not in evo_cls, (
        "演化按钮带上了 .iv —— 它会被 _chart_js 的点击绑定抓走，"
        "点下去会去轮询实时接口")

    # _evo_js 必须按 .evo-iv 绑定（否则两边绑定的集合对不上）
    assert 'querySelectorAll(".evo-iv")' in evo
    # _chart_js 只按 .iv 绑定
    assert 'querySelectorAll(".iv")' in _read("_chart_js.html")


def test_evo_pane_is_lazy_initialised():
    """演化图要等面板可见再 init。

    在 `display:none` 的容器上 echarts.init 拿到 0 宽高，画出来是空白，
    只能再 resize 一次救回来 —— 表现是"第一次点过去图是空的"。
    """
    evo = _read("_evo_js.html")
    assert "EvoPane" in evo and "show:" in evo
    assert "chart.resize()" in evo
    home = _read("dash_home.html")
    assert "window.EvoPane.show()" in home


def test_evo_pane_does_not_poll():
    """回看模式不轮询：kbar.db 是历史层，轮询只会白烧 CPU 并让 tooltip 反复重建。"""
    evo = _read("_evo_js.html")
    assert "setInterval" not in evo, "_evo_js 里出现了 setInterval —— 回看视图不该轮询"


def test_evo_separates_then_and_post_in_the_ui():
    """版面上必须把"当时能看到的"和"事后才知道的"分开，且事后那块要标注。"""
    evo = _read("_evo_js.html")
    assert "当时能看到的" in evo
    assert "事后才知道的" in evo
    # 结论色带只画 label，不许把 status（事后）画进去
    band = re.search(r"var band = bars\.map\(.*?\);", evo, re.S)
    assert band, "找不到结论色带的构造"
    assert "status" not in band.group(0), \
        "结论色带里用了 status（存活结局是事后信息），这会让图本身变成前视入口"


# ------------------------------------------------------- 白屏护栏（node）
@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node")
def test_inline_js_parses():
    """模板里一个逗号写错就整块白屏，而 Python 测试一行都不会红。

    这条把三个页面渲染出来的内联脚本逐个交给 `node --check`。
    """
    import tempfile
    from ai_trader.web.app import create_app
    from ai_trader.config import AppCfg
    from ai_trader.range.store import RangeStore
    from ai_trader.settings import SettingsStore

    class _Hub:
        subscribers = 0

    class _Feed:
        hub = _Hub()
        store = None

        def snapshot(self):
            return {"src": "WS", "symbol": "BTCUSDT", "price": 1.0,
                    "ws_connected": True, "ws_silent": False, "ws_dropped": 0,
                    "ws_idle_ms": 1, "rest_latency_ms": 1, "uptime_s": 1,
                    "ticks_total": 1, "rest_ticks": 0, "ws_stream": "trade"}

    tmp = tempfile.mkdtemp()
    os.environ.setdefault("AI_SECRET_KEY", "test-secret")
    os.environ.setdefault("AI_WEB_PASS", "s3cret")
    os.environ.setdefault("AI_WEB_USER", "admin")
    app = create_app(AppCfg(), _Feed(),
                     range_db=RangeStore(os.path.join(tmp, "r.db")),
                     settings=SettingsStore(os.path.join(tmp, "s.db")))
    c = app.test_client()
    c.post("/login", data={"username": "admin", "password": "s3cret"})

    n_checked = 0
    for page in ("/admin/", "/admin/range", "/admin/live"):
        r = c.get(page)
        assert r.status_code == 200, page
        html = r.get_data(as_text=True)
        for i, body in enumerate(re.findall(
                r"<script(?:\s[^>]*)?>(.*?)</script>", html, re.S | re.I)):
            if not body.strip():
                continue
            p = os.path.join(tmp, "chk_%d_%d.js" % (len(page), i))
            with open(p, "w", encoding="utf-8") as f:
                f.write(body)
            rr = subprocess.run(["node", "--check", p],
                                capture_output=True, text=True)
            assert rr.returncode == 0, (
                "%s 的第 %d 个内联脚本语法错误（页面会白屏）：\n%s"
                % (page, i, (rr.stderr or "")[:800]))
            n_checked += 1
    assert n_checked >= 4, "只检查到 %d 个脚本，页面上应该有更多" % n_checked
