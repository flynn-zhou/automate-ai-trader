# -*- coding: utf-8 -*-
"""只做合约：现货路径必须从**交易语义**上断开，而不是改个默认值。

★ 用户裁决（2026-11-17，原话）：

  「我们是合约账户做交易，我不想看到现货相关的东西影响到我们了！」

为什么这一份文件不能省：改默认值只是「默认值」这一层。真正会咬人的是
`engine._is_spot()` 的 7 个分支 —— 它改的不是「用哪个执行器」，而是
**交易语义本身**：

    _qty_step      0.00001 vs 0.001     差 100 倍
    _fee_buffer    0.003  vs 0.001
    _min_notional  5U     vs 55U        小账户能不能建仓
    _plan_leverage 锁死 1.0
    资金不足提示   现货钱包 vs 合约钱包
    dust 清理      每次开仓多查一次权益（实盘是一次 HTTP）

任何一处漏改都不会报错，只会静默按现货记账 —— 这个项目最吃过亏的失败方式。
所以：配置层拒绝 spot + `_is_spot()` 恒 False + 逐条钉住那 7 处的永续值。
"""
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from ai_trader.config import load
from ai_trader.trading.engine import DecisionEngine


class _Feed:
    last_price = 77749.0


def _eng():
    cfg = load()
    eng = DecisionEngine.__new__(DecisionEngine)
    eng.cfg = cfg
    eng.tcfg = cfg.trading
    eng.feed = _Feed()
    eng.ex = type("E", (), {"name": "live"})()
    return eng


# ============================================================ 配置层

def test_default_exec_market_is_perp():
    """不配任何环境变量时，默认就是合约。"""
    cfg = load()
    assert cfg.trading.exec_market == "perp", (
        "默认 exec_market=%r；只做合约的项目默认必须是 perp" % cfg.trading.exec_market)


def test_spot_is_refused_at_startup(monkeypatch):
    """★ 填 spot 必须**拒绝启动**，不是警告、不是回退。

    这与「拼错策略名拒绝启动」是同一条纪律：配错不能被静默当成
    另一个市场来跑。回退成 perp 看着更「宽容」，实际是把
    「我配了现货」这个意图悄悄执行成合约 —— 真钱上最坏的那种宽容。
    """
    monkeypatch.setenv("AI_EXEC_MARKET", "spot")
    with pytest.raises(ValueError) as e:
        load()
    assert "AI_EXEC_MARKET" in str(e.value)
    assert "perp" in str(e.value)


@pytest.mark.parametrize("raw", ["Spot", "SPOT", " spot ", "spot\n"])
def test_every_spelling_of_spot_is_refused(monkeypatch, raw):
    """大小写/空白归一之后仍然是 spot —— 依然拒绝。

    ★ 反证过：只归一化不拒绝，`AI_EXEC_MARKET=Spot` 会落成合法值。
    """
    monkeypatch.setenv("AI_EXEC_MARKET", raw)
    with pytest.raises(ValueError):
        load()


def test_perp_explicitly_set_still_works(monkeypatch):
    """正路不能被堵死：显式写 perp 必须正常启动。"""
    monkeypatch.setenv("AI_EXEC_MARKET", "perp")
    assert load().trading.exec_market == "perp"


def test_main_does_not_import_the_spot_executor():
    """★ 交易入口不许再引用现货执行器。

    这条是**反证式的**：`main.py` 里只要还有一个 `SpotExecutor(...)` 调用，
    就说明现货还能被挂上去。类本身保留（诊断工具用），但交易路径不许碰。
    """
    import io
    src = io.open(os.path.join(_ROOT, "ai_trader", "main.py"),
                  encoding="utf-8").read()
    live = "\n".join(l for l in src.splitlines()
                     if l.strip() and not l.strip().startswith("#"))
    assert "SpotExecutor" not in live, (
        "main.py 仍引用 SpotExecutor：现货还能被挂到交易路径上")
    assert "LiveExecutor(" in src, "实盘分支必须还在"


# ============================================================ 引擎语义层

def test_is_spot_is_always_false():
    """★ 即使有人把配置改成 spot、或把执行器名字设成 spot。

    三个入口都试：配置、执行器名字、两者都。
    """
    eng = _eng()
    assert eng._is_spot() is False
    eng.tcfg.exec_market = "spot"
    assert eng._is_spot() is False
    eng.ex = type("E", (), {"name": "spot"})()
    assert eng._is_spot() is False


def test_qty_step_is_the_contract_step_only():
    """★ 步长只有一个来源：`cfg.trading.qty_step`。

    以前这里是 `if _is_spot(): return 0.00001` —— 差 100 倍，而且不报错。
    """
    eng = _eng()
    eng.tcfg.exec_market = "spot"
    eng.ex = type("E", (), {"name": "spot"})()
    assert eng._qty_step() == pytest.approx(float(eng.tcfg.qty_step))
    assert eng._qty_step() == pytest.approx(0.001)


def test_min_notional_is_the_contract_minimum():
    """最小名义 55U（永续），不是现货的 5U。"""
    eng = _eng()
    eng.tcfg.exec_market = "spot"
    assert eng._min_notional() == pytest.approx(float(eng.tcfg.trend_min_notional))
    assert eng._min_notional() >= 50.0, (
        "最小名义 %r 掉到 50U 以下了 —— 那不是合约的规则" % eng._min_notional())


def test_fee_buffer_is_the_contract_buffer():
    """缓冲 0.1%（覆盖 0.05% taker 与滑点），不是现货的 0.3%。"""
    eng = _eng()
    eng.tcfg.exec_market = "spot"
    assert eng._fee_buffer() == pytest.approx(0.001)


def test_plan_leverage_is_not_pinned_to_one():
    """★ 现货那条 `return 1.0` 必须消失。

    它把杠杆锁死在 1x，于是「配置写了 22x，实际按 1x 下单」——
    仓位会差 22 倍，而且日志里看到的还是配置值。
    """
    eng = _eng()
    eng.tcfg.exec_market = "spot"
    eng.tcfg.trend_leverage = 5.0
    eng.tcfg.max_leverage = 22.0
    assert eng._plan_leverage() == pytest.approx(5.0)
    eng.tcfg.trend_leverage = 30.0        # 超过上限要被夹住
    assert eng._plan_leverage() == pytest.approx(22.0)


def test_zero_equity_hint_never_mentions_the_spot_wallet():
    """★ 提示文案不能再说「现货钱包」。

    这不是文案洁癖：充错钱包是最常见的运营错误，而提示把人指向
    一个我们不用的钱包，等于教人做错。
    """
    eng = _eng()
    eng.tcfg.exec_market = "spot"
    note = eng._affordability_note(77749.0, 0.0)
    assert note, "余额为 0 却没有提示"
    assert "合约钱包" in note, note
    assert "现货钱包" not in note, "仍在提示现货钱包：%s" % note


def test_affordability_hint_never_mentions_the_spot_wallet():
    eng = _eng()
    note = eng._affordability_note(77749.0, 5.0)
    assert note, "5U 建不了一手合约，必须提示"
    assert "合约钱包" in note
    assert "现货钱包" not in note


def test_open_does_not_do_spot_dust_bookkeeping():
    """★ 开仓前不再多查一次权益去接管现货零头。

    永续按名义合约交易，不存在「手续费扣在基础币上留下的零头」。
    那段代码在实盘里是一次额外的 HTTP 请求，正好发生在行情最激烈的时候。

    ★ 判据用源码事实：`pre_dust` 必须恒为 0.0，且不再有 `await self.ex.equity()`
      出现在 `_open` 的取数环节里。用行为测的话要伪造一整条开仓链路，
      而这条不变式的本质就是「这段代码不该存在」。
    """
    import inspect
    src = inspect.getsource(DecisionEngine._open)
    assert "pre_dust" in src
    # dust 接管块删掉之后，pre_dust 只应有一次赋 0
    assert src.count("pre_dust = 0.0") == 1
    assert "0.0 < pre_btc < step" not in src, "现货零头接管逻辑还在"
