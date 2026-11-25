# -*- coding: utf-8 -*-
"""演化面板的**运行时**检查：在 Node 里跑真模板 + 真数据。

`test_web_chart_fixes.py` 里的那些是源码级断言（"修复还在不在"）。
这一条更进一步：把 `_evo_js.html` 里的 JS 原样拿到 Node 里，
用一个最小的 document/echarts/fetch 替身执行，喂**真实接口返回**，
然后检查它产出的 option 和快照 HTML。

为什么值得这么绕：这一层最可能的故障是运行时的（白屏、点了没反应、
副图错位），而 Python 测试对此完全无感 —— 它们一行都不会红。
白屏和"点了没反应"都是那种"测试全绿、用户一打开就废"的故障。

替身只记录 setOption 的参数、留出 click 回调，不模拟 ECharts 的画法。
所以它证明的是「逻辑跑得通」，不是「画得好看」—— 后者只能靠人看。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

DB = os.path.join(ROOT, "data", "kbar.db")
NODE = shutil.which("node")
HARNESS = os.path.join(ROOT, "tools", "check_evo_ui.js")

pytestmark = [
    pytest.mark.skipif(not os.path.exists(DB), reason="没有 data/kbar.db"),
    pytest.mark.skipif(NODE is None, reason="没有 node"),
    pytest.mark.skipif(not os.path.exists(HARNESS), reason="没有 check_evo_ui.js"),
]


@pytest.fixture()
def client(monkeypatch):
    from flask import Flask
    from ai_trader.web.auth import auth_bp
    from ai_trader.web.kbar_api import kbar_bp

    monkeypatch.setenv("AI_SECRET_KEY", "test-secret")
    a = Flask(__name__)
    a.secret_key = "test-secret"
    a.config["KBAR_DB"] = DB
    a.register_blueprint(auth_bp)
    a.register_blueprint(kbar_bp)
    c = a.test_client()
    with c.session_transaction() as s:
        s["logged_in"] = True
    return c


def _payload(c, iv):
    """取三个端点的真实返回，拼成 harness 要的形状。"""
    meta = c.get("/admin/api/kbar/meta?interval=" + iv).get_json()
    evo = c.get("/admin/api/kbar/evo?interval=%s&limit=300" % iv).get_json()
    assert evo["ok"] and not evo["empty"], "该周期没数据，先回填"
    mid = evo["bars"][len(evo["bars"]) // 2][0]
    snap = c.get("/admin/api/kbar/snapshot?interval=%s&ts=%d" % (iv, mid)
                 ).get_json()
    assert snap["ok"]
    return {"meta": meta, "evo": evo, "snap": snap}


@pytest.mark.parametrize("iv", ["15m", "5m"])
def test_evo_panel_runs_on_real_data(client, tmp_path, iv):
    payload = _payload(client, iv)
    p = tmp_path / "payload.json"
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    r = subprocess.run([NODE, HARNESS, str(p)],
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    assert r.returncode == 0, (
        "演化面板在 Node 里跑不通（%s）：\n%s\n%s"
        % (iv, (r.stdout or "")[-1500:], (r.stderr or "")[-2500:]))
    assert "全部通过" in (r.stdout or "")


def test_evo_panel_rejects_post_hoc_in_the_band(client, tmp_path):
    """红线反证：把 status 混进色带，harness 必须红。

    这是这个工具存在的**主要理由** —— 存活结局是事后信息，
    一旦被画进结论色带（那是对"当时发生了什么"的陈述），
    回看页面就变成了偷看答案的入口，而且看不出任何异常。
    """
    payload = _payload(client, "15m")
    # 把色带里一个色块改成不在 7 词调色板里的颜色，模拟"混进了别的东西"
    payload["evo"]["label"][0] = "已破"
    p = tmp_path / "payload_bad.json"
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    r = subprocess.run([NODE, HARNESS, str(p)],
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    assert r.returncode != 0, "色带混进非调色板内容也没被发现"
    assert "调色板外" in (r.stderr or ""), (r.stderr or "")[-800:]


def test_evo_harness_catches_missing_labels(client, tmp_path):
    """反证：`label` 比 `bars` 短必须被抓到。

    这是真实会发生的一种错位（不是假想的）：后端某个 JOIN 少给了几行，
    前端 `d.label[i]` 变成 undefined，色块退到兜底灰 `#2a2e37`，
    既不在 7 词图例里、也不报错 —— 表现是"有几根颜色不对"，
    看起来像配色问题，实际是数据缺了。

    ⚠ 反过来的方向（砍 `bars` 而留着 `label`）**不会**错位：
      模板里主图的类别和色带都是从 `bars` 派生的，天生同长。
      第一版这条测试就是那么写的，结果无论如何都绿 —— 方向选错了。
      真正会错位的是平行数组之间长短不一，所以这里砍 `label`。
    """
    payload = _payload(client, "15m")
    payload["evo"]["label"] = payload["evo"]["label"][:-5]
    p = tmp_path / "payload_mis.json"
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    r = subprocess.run([NODE, HARNESS, str(p)],
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    assert r.returncode != 0, "label 少了几根也没被发现"
    assert "调色板外" in (r.stderr or ""), (r.stderr or "")[-800:]


# ---------------------------------------------- 结论筛选（点图例里的「插针」）

def _run_with_mutated_template(tmp_path, payload, mutate):
    """把模板改一处再跑 harness。返回 CompletedProcess。"""
    tpl = os.path.join(ROOT, "ai_trader", "web", "templates", "_evo_js.html")
    src = open(tpl, encoding="utf-8").read()
    broken = mutate(src)
    assert broken != src, "mutation 没改到任何东西（模板被改了？）"
    bt = tmp_path / "evo_broken.html"
    bt.write_text(broken, encoding="utf-8")

    p = tmp_path / "payload_filter.json"
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return subprocess.run([NODE, HARNESS, str(p), str(bt)],
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def test_evo_filter_is_not_a_rubber_stamp(client, tmp_path):
    """反证：把 CONTEXT 改成 0，检查必须红。

    为什么这条重要：`focusMask` 的掩码是 harness 用**它自己算出来的 ctx**
    去逐根比对的。如果 ctx 能被悄悄改成 0，"保留上下文"这个语义就没了
    （只留插针那几根，前后反应全被掐掉），而那正是这个功能存在的理由。
    """
    payload = _payload(client, "15m")

    def mut(src):
        return src.replace("var CONTEXT = 3;", "var CONTEXT = 0;")

    r = _run_with_mutated_template(tmp_path, payload, mut)
    assert r.returncode != 0, "CONTEXT 被改成 0 竟然没被发现"
    assert "CONTEXT 不合理" in (r.stderr or ""), (r.stderr or "")[-800:]


def test_evo_filter_must_be_exported(client, tmp_path):
    """反证：把 `_focusMask` 的导出删掉，检查必须红。

    抽成纯函数就是为了让 Node 能直接验算法；导出没了，检查会静默退化成
    "跳过"，那条测试就成了装饰 —— 所以必须显式要求它存在。
    """
    payload = _payload(client, "15m")

    def mut(src):
        return src.replace("_focusMask: focusMask,", "")

    r = _run_with_mutated_template(tmp_path, payload, mut)
    assert r.returncode != 0, "筛选算法没导出竟然没被发现"
    assert "_focusMask" in (r.stderr or ""), (r.stderr or "")[-800:]


def test_evo_filter_dim_is_actually_wired(client, tmp_path):
    """反证：把数据点的 opacity 去掉，检查必须红。

    focusMask 只算"该不该亮"，真正让它变暗的是 itemStyle 上的 opacity。
    算法对、接线断，是这个项目反复出现的失败模式（"测试通过但功能没接线"）。
    """
    payload = _payload(client, "15m")

    def mut(src):
        return src.replace("return { value: v, itemStyle: { opacity: DIM } };",
                           "return { value: v };")

    r = _run_with_mutated_template(tmp_path, payload, mut)
    assert r.returncode != 0, "opacity 没接上竟然没被发现"
    assert "opacity" in (r.stderr or ""), (r.stderr or "")[-800:]
