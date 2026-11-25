# -*- coding: utf-8 -*-
"""惊喜K线阈值必须能**在 Web 上改**，而且只有一个事实来源。

★ 用户要求（2026-11-17，原话）：

  「500 会调整的，放在 web 配置里面，我经常看到的就是 500，700，1200
   这种 5m 的大K线！」

为什么这份测试的重点不是「有没有这个字段」，而是「有没有第二份数字」
----------------------------------------------------------------------
这个项目反复出事的模式是**同一件事两个来源**。这次的风险很具体：

  1. `PendingParams.surprise_pts = 500.0`（状态机手里的默认值）
  2. `TradingCfg.surprise_pts`（配置里的默认值）
  3. `app_settings['strategy.surprise_pts']`（Web 库里存的值）

三处各有一个数时，会出现「页面上写着 1200，状态机还在用 500」——
不报错、不告警，只是规则没生效。而这条规则的作用是「惊喜K线立即走」，
失效的后果是**该走的行情没走**。

所以：字面量只允许在 `config.DEFAULT_SURPRISE_PTS` 出现一次，
另外两处必须引用它；运行时唯一权威是库里的值，经 `apply_to` 写进
活着的 `TradingCfg`（引擎每次决策现读，改完下一根 5m 就生效）。
"""
from __future__ import annotations

import inspect

import pytest

from ai_trader import config as C
from ai_trader.settings import K_SURPRISE, SettingsStore
from ai_trader.trading.pending_plan import PendingParams


# ---------------------------------------------------------------- 单一来源

def test_there_is_exactly_one_literal():
    """★ 500 这个字面量只允许出现在 `config.DEFAULT_SURPRISE_PTS`。

    另两处默认值必须**引用**它。把它们各写一个 500 也能「跑通」，
    但改一处忘另一处时没有任何东西会红——这正是要防的那种静默分叉。
    """
    assert C.DEFAULT_SURPRISE_PTS == 500.0
    assert C.TradingCfg().surprise_pts == C.DEFAULT_SURPRISE_PTS
    assert PendingParams().surprise_pts == C.DEFAULT_SURPRISE_PTS


def test_pending_params_reads_the_config_constant():
    """源码级反证：`pending_plan` 里不许再出现裸的 `surprise_pts = 500`。"""
    src = inspect.getsource(PendingParams)
    assert "DEFAULT_SURPRISE_PTS" in src, (
        "PendingParams.surprise_pts 没有引用 config 的常量 —— 又变成第二个来源了")
    assert "= 500.0" not in src, "PendingParams 里还留着一个裸的 500.0"


def test_settings_defaults_match_the_config_constant():
    """库空时的回落值必须与 config 一致（不一致就等于有两套默认值）。"""
    s = SettingsStore(":memory:")
    try:
        assert s.strategy()[K_SURPRISE] == C.DEFAULT_SURPRISE_PTS
    finally:
        s.close()


def test_seeded_value_comes_from_the_trading_cfg():
    """首次播种必须从 env/yaml 的 `TradingCfg` 取值，不能拿 DEFAULTS 顶。

    ★ 反证过：如果播种写死 DEFAULTS，那么 `AI_SURPRISE_PTS`（或将来任何
      注入途径）会在首启时被静默丢掉——用户以为自己配的是 1200，系统按 500 跑。
    """
    s = SettingsStore(":memory:")
    try:
        cfg = C.AppCfg()
        cfg.trading.surprise_pts = 1200.0
        assert s.seed_from(cfg.trading) is True
        assert s.strategy()[K_SURPRISE] == 1200.0
        # 已有记录时不再覆盖（库是唯一权威）
        assert s.seed_from(cfg.trading) is False
    finally:
        s.close()


# ---------------------------------------------------------------- 运行时生效

def test_apply_to_writes_the_live_trading_cfg():
    """★ 改库之后必须落到**活着的** TradingCfg 上。

    引擎保存的是 `self.tcfg = cfg.trading` 这个引用，并且每次决策现读字段；
    所以这里能直接验证「下一次决策就生效」，不需要重启、不需要重新构造引擎。
    """
    s = SettingsStore(":memory:")
    try:
        live = C.AppCfg().trading
        assert live.surprise_pts == 500.0
        assert s.set_surprise(1200.0)[0] is True
        applied = s.apply_to(live)
        assert applied[K_SURPRISE] == 1200.0
        assert live.surprise_pts == 1200.0, "库里改成 1200，活着的配置还是旧值"
    finally:
        s.close()


def test_the_state_machine_consumes_the_live_value():
    """端到端：`to_thesis` 必须把 `PendingParams` 的值原样交给状态机。

    这条把「配置 -> PendingParams -> Thesis」三跳连起来。中间任何一跳丢了
    这个字段，都不会报错——状态机会退回自己的默认 0.0（= 不启用）。
    """
    import ai_trader.trading.pending_plan as P
    src = inspect.getsource(P)
    assert "surprise_pts=params.surprise_pts" in src, (
        "to_thesis 没有把 surprise_pts 传下去，状态机会退回默认值（等于关掉这条规则）")
    # 反方向：换个阈值构造，字段必须真的变（PendingParams 是 frozen）
    t = P.PendingParams(surprise_pts=1200.0)
    assert t.surprise_pts == 1200.0


# ---------------------------------------------------------------- 校验范围

@pytest.mark.parametrize("good", [1.0, 50.0, 500.0, 700.0, 1200.0, 10000.0])
def test_reasonable_thresholds_are_accepted(good):
    s = SettingsStore(":memory:")
    try:
        assert s.set_surprise(good)[0] is True, "%r 是合法阈值却被拒" % (good,)
        assert s.strategy()[K_SURPRISE] == good
    finally:
        s.close()


@pytest.mark.parametrize("bad", [0, -1, -500, "abc", None, "", float("nan"),
                                 float("inf"), 10001, 999999])
def test_bad_thresholds_are_refused_with_a_reason(bad):
    """★ 非法值必须**拒绝并说明理由**，不许静默夹取。

    为什么 0 必须拒绝：阈值 0 意味着「每一根 5m 都算惊喜」——止盈会把
    正常目标提前砍掉，策略悄悄变成另一个没验证过的东西，而页面上
    写的是 0，看起来像「关掉」。

    为什么超大值也要拒绝：它不是「更保守」，它是把这条规则取消掉。
    一个显示了却不生效的旋钮比没有旋钮更糟。
    """
    s = SettingsStore(":memory:")
    try:
        ok, why = s.set_surprise(bad)
        assert ok is False, "%r 居然被接受" % (bad,)
        assert why, "拒绝时没有给出理由"
        # 拒绝之后库里的值不能被改坏
        assert s.strategy()[K_SURPRISE] == C.DEFAULT_SURPRISE_PTS
    finally:
        s.close()


# ---------------------------------------------------------------- Web 层

@pytest.fixture()
def client(tmp_path):
    from ai_trader.web.app import create_app

    cfg = C.AppCfg()
    cfg.store.path = str(tmp_path / "market.db")
    settings = SettingsStore(str(tmp_path / "settings.db"))
    app = create_app(cfg, feed=_NullFeed(), tdb=None, settings=settings,
                     guard=None, range_db=None)
    app.config["TESTING"] = True
    settings.seed_from(cfg.trading)
    settings.apply_to(cfg.trading)
    with app.test_client() as c:
        yield c, cfg, settings


class _NullFeed:
    last_price = 78000.0

    def on_bar(self, *_a, **_k):
        pass

    def add_hook(self, *_a, **_k):
        pass

    def start(self):
        pass

    async def stop(self):
        pass


def test_get_exposes_the_threshold_and_the_live_value(client):
    c, cfg, _s = client
    d = c.get("/api/config/sizing").get_json()
    assert d["ok"] is True
    assert d["strategy"][K_SURPRISE] == 500.0
    # live 里的值是引擎真正在用的那个字段，不是库的拷贝
    assert d["live"]["surprise_pts"] == cfg.trading.surprise_pts


def test_post_updates_it_and_takes_effect_immediately(client):
    """★ 页面按钮按下去之后，引擎下一次决策用的就是新值。"""
    c, cfg, _s = client
    r = c.post("/api/config/sizing", json={"capital": 50.0, "risk_pct": 5.0,
                                           "leverage": 22.0,
                                           "surprise_pts": 1200.0})
    assert r.status_code == 200, r.get_data(as_text=True)
    d = r.get_json()
    assert d["strategy"][K_SURPRISE] == 1200.0
    assert d["live"]["surprise_pts"] == 1200.0
    assert cfg.trading.surprise_pts == 1200.0, "只写了库，没落到活着的配置上"


def test_post_without_the_field_does_not_reset_it(client):
    """★ 只传三个仓位参数时**不许动阈值**。

    反证过：如果这里默认取 500，那么任何只会传三个数的调用方
    （旧页面、脚本、curl）都会把用户在页面上设好的 1200 悄悄改回 500。
    """
    c, cfg, _s = client
    c.post("/api/config/sizing", json={"capital": 50.0, "risk_pct": 5.0,
                                       "leverage": 22.0, "surprise_pts": 1200.0})
    assert cfg.trading.surprise_pts == 1200.0
    r = c.post("/api/config/sizing", json={"capital": 50.0, "risk_pct": 5.0,
                                           "leverage": 22.0})
    assert r.status_code == 200
    assert cfg.trading.surprise_pts == 1200.0, "没传阈值的请求把它重置了"


def test_post_with_a_bad_threshold_is_rejected_loudly(client):
    c, cfg, _s = client
    before = cfg.trading.surprise_pts
    r = c.post("/api/config/sizing", json={"capital": 50.0, "risk_pct": 5.0,
                                           "leverage": 22.0,
                                           "surprise_pts": 0})
    assert r.status_code == 400, "非法阈值必须 400，不能静默夹取"
    assert "惊喜K线" in r.get_json()["error"]
    assert cfg.trading.surprise_pts == before, "非法值居然生效了"


def test_the_page_renders_the_input_with_typical_values(client):
    """页面必须真的给出输入框和 500/700/1200 这三个常用值。

    只测 API 是不够的：用户是「在 web 配置里面」改它。如果模板漏了
    这个输入框，API 再正确也没人点得到。
    """
    from ai_trader.web.app import create_app
    from ai_trader.config import AppCfg

    c, cfg, settings = client
    app = create_app(AppCfg(), feed=_NullFeed(), tdb=None, settings=settings,
                     guard=None, range_db=None)
    app.config["TESTING"] = True
    with app.test_client() as cli:
        with cli.session_transaction() as sess:
            sess["logged_in"] = True
            sess["user"] = "t"
        # dash_bp 的 url_prefix 是 /admin
        html = cli.get("/admin/config").get_data(as_text=True)
    assert 'name="surprise_pts"' in html, "配置页没有惊喜K线的输入框"
    assert 'value="500"' in html and 'value="700"' in html and 'value="1200"' in html, (
        "常用值 500 / 700 / 1200 没有出现在页面上")
