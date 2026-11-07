# -*- coding: utf-8 -*-
"""每个配置字段都必须有读取点 —— 没有读取点的字段就是一个不会生效的旋钮。

起因（2026-11-02）：
  config.py 里有 tick_bucket_ms=3000，config/config.yaml 也写着它，
  load() 的 _apply 会把它真的写进字段 —— 但整个 ai_trader/ 里没有一处读它。
  3 秒桶实际由模块常量 INTERVALS_MS 和 agg.py 的断言决定。

  于是任何人把 tick_bucket_ms 改成 1000，都会得到一个"看起来改好了、
  实际一点的没变"的系统。这类无效旋钮比没有旋钮更糟：它让人以为自己控制着什么。

  同一批还有 initial_leverage=150（scalp 时代遗留，趋势策略用的是 trend_leverage），
  长得像"账户侧杠杆设置"，实际上什么都不做。

这条测试保证：新增配置字段时必须同时接上读取点，否则红。
"""
import ast
import io
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 允许"只在定义处出现"的字段，必须逐条写清为什么。
ALLOW = {
    # 纯数据载体：由 AppCfg 组合，本身不是旋钮
    "market": "AppCfg 的组合字段，不是可调项",
    "store": "同上",
    "web": "同上",
    "trading": "同上",
}


def _config_classes():
    """返回 {类名: [(字段名, 定义行号)]}"""
    p = os.path.join(ROOT, "ai_trader", "config.py")
    tree = ast.parse(io.open(p, encoding="utf-8").read())
    out = {}
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            fs = []
            for st in node.body:
                if isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name):
                    fs.append((st.target.id, st.lineno))
            if fs:
                out[node.name] = fs
    return out


def _runtime_sources():
    """ai_trader/ 下除 config.py 外的全部源码。"""
    out = {}
    for dp, _d, fs in os.walk(os.path.join(ROOT, "ai_trader")):
        if "__pycache__" in dp:
            continue
        for f in fs:
            if f.endswith(".py"):
                p = os.path.join(dp, f)
                if os.path.basename(p) == "config.py":
                    continue
                out[p] = io.open(p, encoding="utf-8").read()
    return out


def _config_source_without_definitions():
    """config.py 去掉字段定义行之后的内容（方法体里 self.x 也算读取）。"""
    p = os.path.join(ROOT, "ai_trader", "config.py")
    lines = io.open(p, encoding="utf-8").read().splitlines()
    defn = set()
    for fs in _config_classes().values():
        for _n, ln in fs:
            defn.add(ln)
    return "\n".join(l for i, l in enumerate(lines, 1) if i not in defn)


def _is_read(name, sources):
    """属性访问 或 getattr 字符串 都算读取。

    必须两种都查：trend_hysteresis_pct 是用
        getattr(cfg, "trend_hysteresis_pct", 0.0)
    读的，只匹配 ".field" 会把它误判成死字段。
    """
    pat_attr = re.compile(r"\." + re.escape(name) + r"\b")
    pat_getattr = re.compile(
        r"getattr\([^)]*[\"']" + re.escape(name) + r"[\"']")
    for text in sources:
        if pat_attr.search(text) or pat_getattr.search(text):
            return True
    return False


def test_no_dead_config_fields():
    classes = _config_classes()
    assert len(classes) >= 4, "只找到 %d 个配置类，解析逻辑有问题" % len(classes)

    sources = list(_runtime_sources().values()) + [_config_source_without_definitions()]
    dead = []
    total = 0
    for cls, fs in classes.items():
        for name, ln in fs:
            total += 1
            if name in ALLOW:
                continue
            if not _is_read(name, sources):
                dead.append("%s.%s (config.py:%d)" % (cls, name, ln))

    assert total >= 50, "只解析到 %d 个字段，明显少于实际" % total
    assert not dead, (
        u"以下配置字段在 ai_trader/ 里没有任何读取点，改它们不会有任何效果：\n  "
        + "\n  ".join(dead)
        + u"\n要么接上读取点，要么删掉——无效旋钮让人以为自己控制着什么。")


def test_yaml_keys_all_exist():
    """config.yaml 里的每个键都必须是真实的 dataclass 字段。

    load()._apply 里有一句 "if not hasattr(obj, k): continue" ——
    键名拼错会被**静默跳过**，配置看起来改了、实际没改。
    这与死字段是同一类病：一个不生效的旋钮。
    """
    import yaml
    from ai_trader.config import AppCfg

    p = os.path.join(ROOT, "config", "config.yaml")
    data = yaml.safe_load(io.open(p, encoding="utf-8").read()) or {}
    cfg = AppCfg()

    def walk(node, obj, path):
        bad = []
        for k, v in (node or {}).items():
            if not hasattr(obj, k):
                bad.append(path + k)
                continue
            cur = getattr(obj, k)
            if hasattr(cur, "__dataclass_fields__") and isinstance(v, dict):
                bad.extend(walk(v, cur, path + k + "."))
        return bad

    bad = walk(data, cfg, "")
    assert not bad, (
        u"config.yaml 里这些键不是任何配置字段，会被静默忽略：%r" % (bad,))


def test_allow_list_is_small():
    """豁免清单不能变成垃圾桶：每加一条豁免就等于放行一个可能的死字段。"""
    assert len(ALLOW) <= 4, "豁免清单膨胀到 %d 条，需要重新审视" % len(ALLOW)


def test_detector_catches_getattr_reads():
    """自检：检测器必须认得 getattr 字符串形式的读取。

    trend_hysteresis_pct 是这么读的。如果检测器只认 ".field"，
    它会被误报成死字段——那正是我第一版犯的错。
    """
    assert _is_read("trend_hysteresis_pct",
                    [u'getattr(cfg, "trend_hysteresis_pct", 0.0)'])
    assert _is_read("spot_taker_fee", [u"return self.spot_taker_fee"])
    assert not _is_read("tick_bucket_ms", [u"x = 3000"])
