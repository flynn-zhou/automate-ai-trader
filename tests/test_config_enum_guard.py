# -*- coding: utf-8 -*-
"""枚举型环境变量拼错时必须【启动即失败】，不能静默切成另一条策略。

背景（第 100 轮）：
  覆盖率审计发现 engine._step 里 485-550 行（整个剥头皮分支）从未被执行，
  顺着查下去发现了更糟的东西：config 里是

      cfg.trading.strategy = os.environ.get("AI_STRATEGY", cfg.trading.strategy)

  os.environ.get 的第二个参数只在【变量未定义】时生效。
  变量被设成空串或拼错，default 根本不参与，错值直接流进业务逻辑：

      AI_STRATEGY=''       -> engine.py:480 判 != 'trend' -> 掉进 scalp 分支
      AI_STRATEGY='Trand'  -> 同上
      AI_EXEC_MARKET='Spot'-> main.py:73 判 != 'spot' -> 去建【永续】执行器

  前两种会跑一个回测已证伪的负期望策略；
  第三种会跑一个在 100U 上 1x 根本开不出仓的市场（见 DECISION_MARKET.md）。
  三种都是「服务活着、日志干净、策略错了」。

  而且这不是假想：服务器上已经存在 AI_FEISHU_WEBHOOK=（空值）这种写法，
  说明 env 文件里留空值是这个部署的常态。
"""
import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_with(**env):
    """在受控环境变量下重新加载配置；不污染其他测试。"""
    import ai_trader.config as c
    keys = ["AI_STRATEGY", "AI_EXEC_MARKET"]
    old = {k: os.environ.get(k) for k in keys}
    try:
        for k in keys:
            os.environ.pop(k, None)
        for k, v in env.items():
            os.environ[k] = v
        importlib.reload(c)
        return c.load()
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        importlib.reload(c)


def test_misspelled_strategy_refuses_to_start():
    """拼错策略名必须炸，而不是静默掉进剥头皮分支。"""
    with pytest.raises(ValueError) as ei:
        _load_with(AI_STRATEGY="Trand")
    msg = str(ei.value)
    assert "AI_STRATEGY" in msg, "报错信息没提变量名，排障时找不到源头：%r" % msg
    assert "trend" in msg and "scalp" in msg, "报错没列出合法值：%r" % msg


def test_misspelled_exec_market_refuses_to_start():
    """拼错市场名会去建【永续】执行器——那在 100U 上开不出仓，必须炸。"""
    with pytest.raises(ValueError) as ei:
        _load_with(AI_EXEC_MARKET="Spoot")
    assert "AI_EXEC_MARKET" in str(ei.value)


def test_empty_strategy_falls_back_to_the_safe_side():
    """空值（env 文件里留空是常态）按安全默认值跑，不是炸掉。

    选这个行为而不是抛异常：AI_STRATEGY= 这种空洞是配置文件产物，不是意图。
    但默认值必须是被采纳的那一侧 —— trend。
    """
    cfg = _load_with(AI_STRATEGY="")
    assert cfg.trading.strategy == "trend", (
        "空 AI_STRATEGY 落到了 %r。它必须是 trend："
        "落到 scalp 等于静默跑一个已证伪的负期望策略" % cfg.trading.strategy)


def test_empty_exec_market_falls_back_to_spot():
    """空值落到现货：永续在 100U 上 1x 开不出仓。"""
    cfg = _load_with(AI_EXEC_MARKET="")
    assert cfg.trading.exec_market == "spot", (
        "空 AI_EXEC_MARKET 落到了 %r，必须是 spot" % cfg.trading.exec_market)


def test_case_and_whitespace_are_normalised():
    """大小写和首尾空格自动归一，这一类事故从源头消掉。"""
    assert _load_with(AI_STRATEGY="  TrEnD  ").trading.strategy == "trend"
    assert _load_with(AI_EXEC_MARKET=" SPOT ").trading.exec_market == "spot"
    assert _load_with(AI_EXEC_MARKET="Perp").trading.exec_market == "perp"


def test_deliberate_scalp_is_still_allowed():
    """显式选择 scalp 仍然允许——这条测试防的是「把校验做成把合法值也挡掉」。

    回测否决的是「拿它当默认」，不是「永远不许跑」。它要留着做对照。
    """
    assert _load_with(AI_STRATEGY="scalp").trading.strategy == "scalp"
    assert _load_with(AI_EXEC_MARKET="perp").trading.exec_market == "perp"


def test_defaults_are_the_adopted_side():
    """没配任何环境变量时，默认必须是采纳的那一侧。"""
    cfg = _load_with()
    assert cfg.trading.strategy == "trend"
    assert cfg.trading.exec_market == "spot"
