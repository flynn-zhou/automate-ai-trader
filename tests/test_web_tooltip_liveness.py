# -*- coding: utf-8 -*-
"""总览图 tooltip 必须活得过轮询。

这个 bug 修过两次，第一次是**假修**，所以这里用行为测试钉，不用源码断言。

## 两次修法

**第一次**（假修）：加了数据签名短路，但签名里带了
`bars[最后一根].close`。实时图上最后一根是**未收盘**的那根，
它的收盘价就是此刻现价，**每次轮询都在变** ⇒ 短路从不触发 ⇒
每 3 秒照样 `setOption(opt, true)` ⇒ tooltip 照样被销毁。
用户反馈"只有 2 秒"，就是这么来的。

**第二次**（现在）：签名只取「轴的身份」（周期 + 根数 + 最后一根时间）。
轴没变就只 `setOption({series})` 合并，**不传** tooltip / dataZoom / grid。
ECharts 不会重建这两个组件，悬停能一直挂着。

## 为什么不用源码断言

第一次修复的源码断言是绿的（`if (newsig === sig)` 确实存在、确实在
setOption 之前）。**源码里有短路 ≠ 短路会触发**。
所以这条测试模拟真实轮询序列，数「破坏性重建」发生了几次。

行为验证在 `tools/check_live_tooltip.js` 里（Node 跑真模板）。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

NODE = shutil.which("node")
TOOL = os.path.join(ROOT, "tools", "check_live_tooltip.js")

pytestmark = [
    pytest.mark.skipif(NODE is None, reason="没有 node"),
    pytest.mark.skipif(not os.path.exists(TOOL), reason="没有 check_live_tooltip.js"),
]

EMAS = (20, 50, 144, 169, 200)


def _pack_payload(n=60, base_ts=1_800_000_000_000, step=180_000):
    """造一份形状正确的 pack 返回（不联网，数字是编的）。

    不联网是刻意的：这条测试验的是**前端会不会重建图表**，
    和价格真不真、币安通不通没有关系。让它依赖网络只会变成随机红。
    """
    bars, closes = [], []
    for i in range(n):
        o = 70000.0 + i * 5
        c = o + 3.0
        bars.append([base_ts + i * step, round(o, 1), round(c, 1),
                     round(o - 4, 1), round(c + 6, 1)])
        closes.append(c)
    ema = {str(p): [round(v, 1) for v in closes] for p in EMAS}
    last = bars[-1]
    return {
        "ok": True, "symbol": "BTCUSDT", "interval": "3m",
        "interval_ms": step, "price": last[2],
        "change": 3.0, "change_pct": 0.004,
        "live": {"symbol": "BTCUSDT", "interval": "3m", "interval_ms": step,
                 "warmup": 1500, "bars": bars, "ema": ema,
                 "server_time": base_ts + n * step},
        "h1_now": last,
        "server_time": base_ts + n * step,
    }


def _rendered_chart_script(tmp_path, monkeypatch):
    """渲染 /admin/，把带 renderLive 的那个内联 script 取出来。"""
    os.environ.setdefault("AI_SECRET_KEY", "test-secret")
    os.environ.setdefault("AI_WEB_PASS", "s3cret")
    os.environ.setdefault("AI_WEB_USER", "admin")

    from ai_trader.config import AppCfg
    from ai_trader.range.store import RangeStore
    from ai_trader.settings import SettingsStore
    from ai_trader.web.app import create_app

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

    app = create_app(AppCfg(), _Feed(),
                     range_db=RangeStore(str(tmp_path / "r.db")),
                     settings=SettingsStore(str(tmp_path / "s.db")))
    c = app.test_client()
    c.post("/login", data={"username": "admin", "password": "s3cret"})
    r = c.get("/admin/")
    assert r.status_code == 200
    html = r.get_data(as_text=True)

    blocks = re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", html, re.S | re.I)
    hits = [b for b in blocks if "renderLive" in b]
    assert hits, "页面上找不到带 renderLive 的脚本块"
    assert "POLL_MS" in hits[0]
    # 确认 Jinja 已渲染（否则 Node 会撞上 {{
    assert "{{" not in hits[0], "脚本里还有没渲染的 Jinja"
    return hits[0]


def test_tooltip_survives_polling(tmp_path, monkeypatch):
    src = _rendered_chart_script(tmp_path, monkeypatch)
    js = tmp_path / "chart_script.js"
    js.write_text(src, encoding="utf-8")

    pl = tmp_path / "pack.json"
    pl.write_text(json.dumps(_pack_payload(), ensure_ascii=False),
                  encoding="utf-8")

    r = subprocess.run([NODE, TOOL, str(pl), str(js)],
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    assert r.returncode == 0, (
        "tooltip 活不过轮询：\n%s\n%s"
        % ((r.stdout or "")[-1200:], (r.stderr or "")[-2500:]))
    assert "通过" in (r.stdout or "")


def test_tooltip_harness_is_not_a_rubber_stamp(tmp_path, monkeypatch):
    """反证：把收盘价塞回签名，检查必须红。

    这条是这条测试的意义所在 —— 第一次修复的源码断言是绿的，
    但功能没生效。只有"短路真的会触发"才算修好。
    """
    src = _rendered_chart_script(tmp_path, monkeypatch)

    # 把签名改回"带收盘价"的旧写法（等价于第一次那个假修）
    broken = src.replace(
        'var axisSig = cur + "|" + lb.length + "|" + (lastBar ? lastBar[0] : "");',
        'var axisSig = cur + "|" + lb.length + "|" + '
        '(lastBar ? lastBar[0] + "@" + lastBar[2] : "");')
    assert broken != src, "没找到签名那一行，反证做不成（模板被改了？）"

    js = tmp_path / "chart_broken.js"
    js.write_text(broken, encoding="utf-8")
    pl = tmp_path / "pack.json"
    pl.write_text(json.dumps(_pack_payload(), ensure_ascii=False),
                  encoding="utf-8")

    r = subprocess.run([NODE, TOOL, str(pl), str(js)],
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    assert r.returncode != 0, "把收盘价塞回签名竟然也没被发现 —— 检查是橡皮图章"
    assert "破坏性重建" in (r.stderr or ""), (r.stderr or "")[-800:]
