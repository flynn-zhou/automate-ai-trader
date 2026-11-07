# -*- coding: utf-8 -*-
"""K 线看板的"静默错误"防线。

这里锁的几件事，错了都**不会报错**，只会画出一张看起来正常但错误的图：
一、ECharts 蜡烛图数据顺序必须是 [开, 收, 低, 高]。
   而 range 库的 bars_1h 行是 (ts, ts8, o, h, l, c, v) —— high 在 low 前面。
   照抄成 [o, c, h, l] 会把每根蜡烛的上下影线画颠倒，图上完全看不出异常。
二、yAxis 在右侧时 grid.right 必须留够（约 72px）。
   留 18px 会把 "80232.7" 裁成 "70" 这种两位数 —— 用户就是这样发现的。
三、现价横向虚线（markLine）不能丢。
四、井字带不能用 clip()：实测 clip 在这条路径上只保留一小部分
   （807x29 的带只画出 494 像素），必须直接算线。
五、总览页和行情页必须共用同一份图表代码 —— 各写一遍就必然改一处忘一处，
   这正是"两个页面表现不一致"的成因。

为什么用"读源码断言"而不是渲染测试：
Flask 的 test_client 不会执行 JS，渲染测试对图表配置是盲的。
"""
from __future__ import annotations

import io
import os

TPL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "ai_trader", "web", "templates")
STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "ai_trader", "web", "static")


def _read(name, base=None):
    return io.open(os.path.join(base or TPL, name), encoding="utf-8").read()


def _flat(name, base=None):
    return _read(name, base).replace(" ", "").replace("\n", "")


def test_shared_chart_fragment_is_used_by_both_pages():
    """两个页面必须 include 同一份图表片段，不许各写一遍。"""
    for name in ("dash_home.html", "dash_live.html"):
        src = _read(name)
        assert '{% include "_chart_body.html" %}' in src, \
            "%s 没用共用图表主体" % name
        assert '{% include "_chart_js.html" %}' in src, \
            "%s 没用共用图表脚本" % name
        # 也不许自己再 init 一次 echarts（会变成两张图/一次多余请求）
        assert "echarts.init" not in src, "%s 自己又 init 了一遍图表" % name


def test_live_candlestick_mapping_is_correct():
    """后端给 [ts, o, c, l, h]，前端映射后要还原成 [o, c, l, h]。"""
    src = _flat("_chart_js.html")
    assert "o:b[1],c:b[2],l:b[3],h:b[4]" in src, "字段映射错了"
    assert "[b.o,b.c,b.l,b.h]" in src


def test_range_candlestick_is_open_close_low_high():
    """range 页：high/low 不能颠倒。"""
    src = _read("dash_range.html")
    assert "[b[2], b[5], b[4], b[3]]" in src, \
        "range 蜡烛图必须是 [开, 收, 低, 高]（b[3] 是 high、b[4] 是 low）"
    assert "[b[2], b[5], b[3], b[4]]" not in src, "高低位写反了"


def test_range_has_ema20():
    """区间演化必须显示 EMA20（用户明确要求）。"""
    src = _read("dash_range.html")
    assert "EMA20" in src
    assert "ema(closes, 20)" in src, "EMA20 没有真正计算"


def test_range_tail_updates_all_arrays_together():
    """未完成那根必须同时推进 cats、ohlc、closes 三个数组。

    只推其中一个，时间轴就和蜡烛/EMA 错位一格 —— 而图上不会报错，
    只是所有线整体偏一根，极难发现。
    """
    src = _read("dash_range.html")
    block = src[src.index("if (d.tail &&"):src.index("// 最新 K 与价格刻度之间")]
    assert "cats.push(" in block, "tail 没推进 cats"
    assert "ohlc.push(" in block, "tail 没推进 ohlc"
    assert "closes.push(" in block, "tail 没推进 closes"


def test_range_pads_right_with_empty_categories():
    """最新 K 与价格刻度之间要有空隙。

    旧版验收过的做法是末尾补空分类，注释原话：
    "避免最新一根贴住网格右缘"。只调 grid.right 是错的 ——
    那只缩窄绘图区，最新一根依旧贴着右缘。
    """
    for name in ("dash_range.html", "_chart_js.html"):
        src = _read(name)
        assert "cats.push('')" in src or "cats.push(\"\")" in src, \
            "%s 没有补右侧空分类" % name
        assert "ohlc.push('-')`".replace("`", "") in src or "ohlc.push(\"-\")" in src, \
            "%s 空占位必须用 '-'，用 null 会让 candlestick 抛异常并静默白屏" % name


def test_range_four_lines_extend_to_live_bar():
    """四线要延续到未收盘那根（表示该水平当前仍生效），且值取最后一根已收盘的。"""
    src = _read("dash_range.html")
    assert "arr[liveIdx] = ls[idx]" in src, "四线没延续到未收盘那根"
    assert "外推" in src, "缺少'绝不按实时价外推'的说明"


def test_both_pages_reserve_axis_width():
    src = _read("_chart_js.html")
    assert "right: 72" in src, "grid.right 没留够，价格标签会被裁断"
    assert 'position: "right"' in src, "yAxis 不在右侧"


def test_both_pages_have_current_price_line():
    src = _read("_chart_js.html")
    assert "markLine" in src, "缺少现价虚线"
    assert "yAxis: last.c" in src, "现价线没接到真实数据"


def test_both_pages_show_live_price_readout():
    for name in ("dash_home.html", "dash_live.html"):
        src = _read(name)
        assert 'id="last-price"' in src, "%s 没有现价显示" % name
        assert 'id="last-delta"' in src, "%s 没有涨跌显示" % name
        assert 'id="refresh"' in src, "%s 没有刷新按钮" % name


def test_refresh_button_is_wired():
    """刷新按钮必须真的重取数据，不能只是画了个按钮。"""
    src = _read("_chart_js.html")
    assert '$("refresh")' in src
    assert "go.onclick" in src


def test_live_hatch_avoids_clip():
    """井字不能用 clip：实测 clip 只保留一小部分（807x29 的带只画出 494 像素）。"""
    src = _read("chartkit.js", STATIC)
    assert "drawHatch" in src
    assert "convertToPixel" in src
    assert ".clip()" not in src, "又用回 clip() 了，这条路径实测会漏画"


def test_hatch_is_not_via_canvas_pattern():
    """这版 echarts.min.js 会静默丢弃 CanvasPattern，传了等于没传。"""
    src = _read("chartkit.js", STATIC)
    assert "createPattern" not in src

def test_range_latest_price_uses_live_bar_not_last_closed():
    """区间演化页显示的最新价必须是未完成那根的收盘，不是最后一根已收盘的收盘。

    未完成那根只进了 cats/ohlc/closes 三个数组，**没有进 raw**，
    所以用 raw[raw.length-1] 拿到的是上一个小时的收盘价 ——
    它和总览的现价天然差一整个小时。这就是"两个页面价格对不上"的根因。
    """
    src = _read("dash_range.html")
    assert "var priceNow = liveBar ? liveBar.ohlc[3]" in src, \
        "最新价没优先取未完成那根"
    assert 'fmt(last[5])' not in src, \
        "还在用最后一根已收盘的收盘价当现价"
    # 现价横线也要用现价，不能用已收盘价
    assert "yAxis: priceNow" in src.replace("yAxis:priceNow", "yAxis: priceNow"), \
        "现价虚线没接到现价"


def test_both_pages_show_same_price_source():
    """两个页面的现价都必须来自同一时刻的实时成交价。

    总览走 /admin/api/pack 的 j.price（币安最后一根 3m 的收盘 = 当前价）；
    区间演化走 tail.ohlc[3]（币安未完成 1h 的收盘 = 当前价）。
    两条链路的源头是同一个交易所接口，不一致就说明其中一条取错了字段。
    """
    live = _read("_chart_js.html")
    rng = _read("dash_range.html")
    assert "j.price" in live, "总览没用 pack 的实时价"
    assert "liveBar.ohlc[3]" in rng, "区间演化没用未完成 1h 的收盘"
