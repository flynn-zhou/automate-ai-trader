# -*- coding: utf-8 -*-
"""部署模板里缺了代码要用的环境变量 —— 新机器会静默跑错配置。

起因（2026-11-02）：
  线上 env 有 10 个键，而 deploy/install_server.sh 里那段 heredoc 只写了 8 个，
  env.server.example 更少。少掉的两个正好是：
    AI_STRATEGY=trend      （第 55 轮修过：默认值必须是被采纳的策略）
    AI_TREND_HYST=0.2      （第 69 轮实测：CAGR 41.70% -> 45.06%，手续费少 21.7%）

  于是「照着仓库装一台新机器」得到的配置，和线上跑的配置不是同一个，
  而且不会有任何报错——这正是本项目最反复出现的那类缺陷：静默降级。

不变量：
  代码里 os.environ.get("AI_XXX") 读到的每一个变量，
  都必须在部署模板里出现（机密项除外，它们只能人工填写）。
"""

import io
import json
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# 机密项：只能人工填，模板里必须以注释或空值形式留位，不算缺失
SECRETS = {"AI_API_KEY", "AI_API_SECRET"}


def code_read_vars():
    """扫描 ai_trader/ 下所有读 AI_* 环境变量的地方。

    两种写法都要认（第 100 轮补第二种）：
      1) os.environ.get("AI_XXX", ...)   —— 直接读
      2) _pick("AI_XXX", ...)            —— 枚举校验读

    为什么必须认第二种：_pick 内部是 os.environ.get(env)，env 是变量名，
    正则扫不到字面量。第 100 轮把 AI_STRATEGY / AI_EXEC_MARKET 换成 _pick 之后，
    这个测试立刻红了——正好证明它有效（它发现「代码要读但模板可能没写」的变量少了两个）。
    正确的处理是让扫描器认识新的读取形式，而不是放宽断言。
    """
    found = set()
    pats = [
        re.compile(r'os\.environ\.get\(\s*["\'](AI_[A-Z0-9_]+)["\']'),
        re.compile(r'_pick\(\s*["\'](AI_[A-Z0-9_]+)["\']'),
    ]
    for dirpath, _dirs, files in os.walk(os.path.join(ROOT, "ai_trader")):
        if "__pycache__" in dirpath:
            continue
        for fn in files:
            if not fn.endswith(".py"):
                continue
            s = io.open(os.path.join(dirpath, fn), encoding="utf-8").read()
            for pat in pats:
                found.update(pat.findall(s))
    return found


def install_env_keys():
    """从 install_server.sh 的 heredoc 里取出实际写入的键。"""
    p = os.path.join(ROOT, "deploy", "install_server.sh")
    s = io.open(p, encoding="utf-8").read()
    m = re.search(r"cat > /etc/ai-trader/env <<'EOF'\n(.*?)\nEOF", s, re.S)
    assert m, "install_server.sh 里的 env heredoc 结构变了，测试需要同步更新"
    keys = set()
    for ln in m.group(1).splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#") or "=" not in ln:
            continue
        keys.add(ln.split("=", 1)[0].strip())
    return keys


def example_env_keys():
    """env.server.example 里的键（含留空的）。"""
    p = os.path.join(ROOT, "deploy", "env.server.example")
    s = io.open(p, encoding="utf-8").read()
    keys = set()
    for ln in s.splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#") or "=" not in ln:
            continue
        keys.add(ln.split("=", 1)[0].strip())
    return keys


def test_code_read_vars_nonempty():
    """自检：扫描本身不能是空的，否则下面的断言永远绿。"""
    got = code_read_vars()
    assert len(got) >= 10, "只扫到 %d 个变量，扫描逻辑本身有问题：%r" % (len(got), got)
    assert "AI_TREND_HYST" in got and "AI_STRATEGY" in got


def test_install_template_covers_every_variable():
    need = code_read_vars() - SECRETS
    have = install_env_keys()
    missing = sorted(need - have)
    assert not missing, (
        u"deploy/install_server.sh 会写出一个缺少 %r 的 env 文件。\n"
        u"照着它装一台新机器，这些变量就会落到 config.py 的默认值——"
        u"而默认值不一定是生产用的值（hyst 默认 0.0，生产 0.2）。" % (missing,))


def test_example_template_covers_every_variable():
    need = code_read_vars() - SECRETS
    have = example_env_keys()
    missing = sorted(need - have)
    assert not missing, (
        u"deploy/env.server.example 缺 %r —— 它是文档里让人照抄的模板，"
        u"缺一个就等于教人配错。" % (missing,))


def test_default_hysteresis_is_the_adopted_value():
    """默认值必须是被采纳的那一侧。

    项目自己已经为 strategy 立过这条规矩（config.py 里写着
    「默认值必须是安全的那一边」），hyst 却没跟上：
    实测 h=0.2% 比 h=0 的 CAGR 高 3.36pp、手续费少 21.7%，
    而默认值是 0.0。任何没配 AI_TREND_HYST 的环境都静默跑更差的那一档。
    """
    from ai_trader.config import TradingCfg
    from ai_trader.trading.wait_stats import CALIBRATED_HYST_PCT

    d = TradingCfg()
    assert abs(d.trend_hysteresis_pct - CALIBRATED_HYST_PCT) < 1e-9, (
        u"TradingCfg.trend_hysteresis_pct 默认 %.4f，而标定值是 %.4f。"
        u"漏配 AI_TREND_HYST 的环境会退回实测更差的那一档。"
        % (d.trend_hysteresis_pct, CALIBRATED_HYST_PCT))
