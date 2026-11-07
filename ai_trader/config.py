"""配置加载：YAML + 环境变量覆盖。"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

import yaml

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class MarketCfg:
    symbol: str = "BTCUSDT"
    # 曾经这里有个 tick_bucket_ms=3000，从没被读过——3 秒桶是结构不变量，
    # 由下面的 INTERVALS_MS 和 market/agg.py 的断言决定（"3s 必须是原子周期"）。
    # 留一个改不动的旋钮比没有旋钮更糟：它让人以为自己控制着什么。
    ws_url: str = "wss://fstream.binance.com/ws"
    fapi_base: str = "https://fapi.binance.com"
    rest_poll_ms: int = 3000
    tick_mode: str = "auto"         # auto | ws | rest（auto = WS 有数据走 WS，否则走 REST 逐笔）
    enable_ws: bool = True
    enable_rest_fallback: bool = True
    rest_tick_bootstrap: int = 300  # 冷启动时回补的逐笔条数
    ws_stale_ms: int = 10000        # WS 超过这么久没消息 -> 判定降级
    # 逐笔流名：aggTrade（聚合成交）或 trade（逐笔成交）。
    # 2026-11-02 实测：本环境下永续 @aggTrade 连得上但一条不推
    # （45 秒 0 条；同一时刻 @trade 45 秒 195 条），所以线上用 trade。
    # 两种事件的字段兼容（p/q/T/m），ws.py 都接受。
    ws_stream: str = "aggTrade"
    http_proxy: str = ""            # 生产留空；本机经 clash 填 http://127.0.0.1:4780


@dataclass
class StoreCfg:
    path: str = str(ROOT / "data" / "market.db")
    tick_retention_hours: int = 168


@dataclass
class WebCfg:
    host: str = "0.0.0.0"
    port: int = 8090
    auth_token: str = ""


@dataclass
class TradingCfg:
    mode: str = "paper"              # paper | live
    symbol: str = "BTCUSDT"
    paper_balance: float = 100.0
    # 单笔风险 = 本金 × risk_pct，向下取整。100U × 5% = 5U → 可连亏 20 次才归零。
    # 这里曾是 35.0（3 条命），2026-11 按用户裁决改为 5。别再改回去：
    # 35% 单笔风险在 22x 下等于把「爆仓」当常规止损用。
    risk_pct: float = 5.0
    # 杠杆硬上限。22x 下爆仓距离 = 1/22 - 0.5% = 4.045%；
    # 安全不变量要求 爆仓距离 >= 2.5 × 止损距离 ⇒ 止损上限 1.618%。
    # 实际杠杆仍是「结果」（qty×price/equity），这里只做上限。
    max_leverage: float = 22.0
    daily_loss_pct: float = 50.0
    min_stop_pct: float = 0.40       # 目标 1.6R=0.64% 必须 >= 3x 往返成本(0.11%)
    max_stop_pct: float = 3.0
    stop_buffer_pct: float = 0.05
    rr_target: float = 1.6           # 赔率必须 >1：手续费按名义收，赔率<1 时胜率再高也填不平
    # 费率必须分市场：币安现货 taker 是 0.10%，永续 VIP0 约 0.04-0.05%。
    #
    # 以前只有一个 taker_fee=0.0005（永续的费率），而线上跑的是现货：
    #   纸面每笔按 0.05% 收，现实收 0.10% —— 少收一半。
    # 后果不是"数字差一点"，是【纸面样本和回测说的不是同一件事】：
    #   所有测试和 tools/replay_engine 都显式设 0.001，
    #   回测也按 0.001 算，只有线上纸面在用 0.0005。
    # 而成本敏感性实测：每边 0.05% -> 累计 +1298%，0.10% -> +1032%，
    # 差 266 个百分点——正好是这两种费率的距离。
    #
    # 所以费率不再是一个数，而是一个市场一个数，用 taker_fee_for() 取。
    taker_fee: float = 0.0005        # 永续（perp）
    spot_taker_fee: float = 0.0010   # 现货：币安 BNB 未抵扣的 taker 费率
    slippage_bps: float = 1.0

    def taker_fee_for(self, is_spot: bool) -> float:
        """按市场取 taker 费率。现货和永续差一倍，不能混用。

        注意：实盘成交回报里带了真实 commission 时，以交易所给的为准；
        这个函数只用于【估算】（纸面撮合、缺回报时的兜底、成本闸门）。
        """
        return self.spot_taker_fee if is_spot else self.taker_fee
    api_key: str = ""                # 实盘凭证，只从环境变量读，不落配置文件
    api_secret: str = ""
    # spot | perp。只做多的策略必须走现货：永续的资金费长期正费率（多头付钱）
    # 年化 11.6%，实测六年下来现货 1x 的卡玛 0.61 明显优于永续 1.5x 的 0.43。
    exec_market: str = "spot"
    cost_multiple: float = 3.0
    # ---- 策略选择 ----
    # scalp = 3s 微型动量（已被回测证伪：扣成本后负期望，仅保留作对照）
    # trend = 4h SMA 只做多趋势跟随（6 年样本外验证：+666%/回撤 30-47%）
    # 默认值必须是安全的那一边。scalp 是回测里已被否决的负期望策略
    # （胜率低且成本吃掉全部边际），把它当默认意味着：
    # 任何没显式配置策略的环境（手动脚本、新机器、漏了 env 文件）
    # 都会静默跑一个已知亏钱的策略，而且不会报错。
    # 这已经坑过一次：日志里同时存在 scalp 和 trend 的记录，
    # 原因是手动执行读不到 systemd 的 EnvironmentFile。
    strategy: str = "trend"
    sma_lookback: int = 200          # 4h SMA 周期
    # 现货 1x。加杠杆在永续上是负收益：资金费按名义线性收（年化 11.6%），
    # 杠杆等比例放大它，实测六年卡玛 1x=0.61 反而高于 1.5x=0.43、2x=0.40。
    trend_leverage: float = 1.0
    trend_stop_pct: float = 25.0     # 熔断止损：只有极端情况才用（正常靠 SMA 交叉平仓）
    trend_min_notional: float = 55.0 # 币安合约最小名义，留 10% 余量
    # ---- 唐奇安通道（AI_STRATEGY=donchian）----
    # 回测依据见 donchian.py 的模块 docstring。默认值必须是实测最优的那一组，
    # 不能是"也还行"的邻居：(150/75) 每笔 +2.42%、复利 +978%，
    # (100/50) 只有 +0.87%、复利腰斩且滚动窗 5/7。
    donchian_entry: int = 150
    donchian_exit: int = 75
    # 主周期 1h。5m/15m 上同类策略扣成本后全负，4h 上笔数太少——
    # 这个值不是随口挑的，换算成别的周期等于换了一个没验证过的策略。
    donchian_interval_ms: int = 3600000
    # 做空 / 硬停损 / 持仓上限三项都【刻意不做】，而且【刻意不留配置开关】。
    # 理由写在 donchian.py 模块 docstring，数据是：
    #   双向   笔数 x2、每笔腰斩、回撤 28.4% -> 51.4%
    #   2ATR  笔数 128->189、每笔 2.42%->1.39%、100U 终值 689->559
    #   240h  每笔 2.42%->1.68%
    # 不留开关不是偷懒：一个从没被读取的配置项会让运维以为"这个能调"，
    # 而实际调了没效果——tests/test_no_dead_config.py 就是钉这条的。
    # 滞回带半宽（%）。0 = 现行规则：收盘 > SMA 就进、收盘 < SMA 就出。
    # >0 时：收盘 > SMA*(1+h) 才进、收盘 < SMA*(1-h) 才出。
    #
    # 为什么要有：一次往返手续费 0.2%（现货 taker 实测 0.100%/边）。
    # 收盘只比均线高 0.03% 的那种交叉，赚的钱大概率不够付手续费，
    # 却照样触发一次完整往返。这不是"更保守"，是让信号门槛长得像成本。
    #
    # 实测（服务器 6.84 年，h=0.2%）：笔数 213->147，净 CAGR 41.70%->45.06%，
    # 最大回撤 47.8%->46.1%，毛收益只动 +0.62pp（说明没改逻辑、只少做了擦边单）。
    # 按项目自己的时代尺子：三个时代全部"收益与回撤同时改善"（3/3），
    # 而当年被否决的"连续 N 根确认"是 0/3。
    # 默认值必须是被采纳的那一侧——这条规矩上面 strategy 已经立过了，
    # 而这里以前写着 0.0，正好是实测更差的那档。后果：
    # 任何没配 AI_TREND_HYST 的环境（新机器、重建 env、手动脚本）
    # 都会静默跑 h=0，CAGR 少 3.36pp、手续费多 21.7%，且没有任何提示。
    # 标定值单点放在 wait_stats.CALIBRATED_HYST_PCT，由
    # tests/test_deploy_env_completeness.py 保证两边不分叉。
    trend_hysteresis_pct: float = 0.2

    max_consecutive_losses: int = 3
    cooldown_min: int = 30
    qty_step: float = 0.001
    min_notional: float = 50.0
    max_hold_bars: int = 600          # 仅在没有 σ 数据时的兜底
    max_hold_hours: float = 24.0      # 持仓硬上限（小时），远超则强平
    reach_min_bars: int = 60          # 可达性门：目标至少要能在这个根数内被行情够到         # 3s 桶数 = 30 分钟
    breakout_vol_z: float = 1.0
    micro_move_pct: float = 0.05      # （保留）绝对位移下限
    micro_vol_x: float = 0.9          # 近 6 根量能相对基准倍数
    micro_sigma: float = 3.0          # 位移以自身 σ 为单位：3σ 才算异动
    stop_sigma_mult: float = 4.0      # 止损至少放在 4σ 之外
    fakeout_pierce_pct: float = 0.03
    min_atr_pct: float = 0.0005
    max_atr_pct: float = 0.35
    trail_after_r: float = 1.5
    breakeven_r: float = 1.0
    account_mmr: float = 0.004       # 维持保证金率（首档）
    # 这里曾有个 initial_leverage=150，scalp 时代的遗留，从没被读过。
    # 趋势策略实际用的是 trend_leverage（现货 1x）。删掉它是为了避免
    # 有人把"150 倍账户侧杠杆"当成生效中的设置。


@dataclass
class AppCfg:
    env: str = "dev"
    log_level: str = "INFO"
    market: MarketCfg = field(default_factory=MarketCfg)
    store: StoreCfg = field(default_factory=StoreCfg)
    web: WebCfg = field(default_factory=WebCfg)
    trading: TradingCfg = field(default_factory=TradingCfg)


def _apply(obj, data) -> None:
    for k, v in (data or {}).items():
        if not hasattr(obj, k):
            continue
        cur = getattr(obj, k)
        if hasattr(cur, "__dataclass_fields__") and isinstance(v, dict):
            _apply(cur, v)
        else:
            setattr(obj, k, v)


def _pick(env: str, default: str, allowed=()) -> str:
    """读一个【枚举型】环境变量，并在拼错时大声失败。

    为什么需要（第 100 轮）：
      以前是 os.environ.get(env, default)。这行有个隐蔽后果：
      环境变量一旦被设成【拼错】或【空字符串】，default 根本不生效，
      而错值会一路流进业务逻辑，走另一条分支，且不报错。

      实测过三种真实事故：
        AI_STRATEGY=''      -> 掉进 scalp 分支（回测已证伪的负期望策略）
        AI_STRATEGY='Trand' -> 同上
        AI_EXEC_MARKET='Spot' -> main.py:73 判 'spot' 失败，去建【永续】执行器，
                                而永续在 100U 上 1x 根本开不出仓（见 DECISION_MARKET）

      三种都是「服务活着、日志干净、策略错了」。所以这里把它变成启动即失败。

    规则：
      - 空/全空白 -> 用默认值（env 文件里写 `AI_STRATEGY=` 这种事真的会发生），
        默认值必须是安全的那一边；
      - 大小写、首尾空白 -> 自动归一（' Trend ' 当作 'trend'）；
      - 其余不认识的值 -> ValueError，宁可起不来也不要跑错策略。
    """
    raw = os.environ.get(env)
    if raw is None:
        val = default
    else:
        val = str(raw).strip().lower()
        if not val:
            logger.warning("%s 是空字符串，按默认值 %r 运行", env, default)
            val = default
        elif allowed and val not in allowed:
            raise ValueError(
                "%s=%r 不是合法取值（只接受 %s）。"
                "拼错会被静默当成另一条策略/另一个市场来跑，所以这里直接拒绝启动。"
                % (env, raw, "/".join(allowed)))
    return val


def load(path: str = "") -> AppCfg:
    cfg = AppCfg()
    p = Path(path) if path else (ROOT / "config" / "config.yaml")
    if p.exists():
        with open(p, "r", encoding="utf-8") as f:
            _apply(cfg, yaml.safe_load(f) or {})
    if not cfg.store.path:
        cfg.store.path = str(ROOT / "data" / "market.db")
    cfg.market.symbol = os.environ.get("AI_SYMBOL", cfg.market.symbol).upper()
    cfg.market.http_proxy = os.environ.get("AI_HTTP_PROXY", cfg.market.http_proxy)
    cfg.market.ws_stream = os.environ.get("AI_WS_STREAM", cfg.market.ws_stream)
    cfg.store.path = os.environ.get("AI_DB_PATH", cfg.store.path)
    cfg.web.port = int(os.environ.get("AI_WEB_PORT", cfg.web.port))
    cfg.web.auth_token = os.environ.get("AI_WEB_TOKEN", cfg.web.auth_token)
    cfg.trading.mode = os.environ.get("AI_TRADE_MODE", cfg.trading.mode)
    cfg.trading.paper_balance = float(os.environ.get("AI_PAPER_BALANCE", cfg.trading.paper_balance))
    # donchian 是 2026-11-02 在 7.15 年 1h K 线上唯一通过全部五道关的策略
    # （逐年/滚动/样本外/参数邻域/100U 真实账户）。加进白名单时必须一起加，
    # 否则配了 AI_STRATEGY=donchian 会直接拒绝启动——这是刻意的：
    # 拼错策略名被静默当成另一个策略跑，比启动失败危险得多。
    cfg.trading.strategy = _pick("AI_STRATEGY", cfg.trading.strategy,
                                  ("trend", "scalp", "donchian"))
    cfg.trading.donchian_entry = int(
        os.environ.get("AI_DONCHIAN_ENTRY", cfg.trading.donchian_entry))
    cfg.trading.donchian_exit = int(
        os.environ.get("AI_DONCHIAN_EXIT", cfg.trading.donchian_exit))
    cfg.trading.api_key = os.environ.get("AI_API_KEY", cfg.trading.api_key)
    cfg.trading.api_secret = os.environ.get("AI_API_SECRET", cfg.trading.api_secret)
    cfg.trading.exec_market = _pick("AI_EXEC_MARKET", cfg.trading.exec_market,
                                    ("spot", "perp"))
    cfg.trading.trend_hysteresis_pct = float(
        os.environ.get("AI_TREND_HYST", cfg.trading.trend_hysteresis_pct))
    cfg.log_level = os.environ.get("AI_LOG_LEVEL", cfg.log_level)
    Path(cfg.store.path).parent.mkdir(parents=True, exist_ok=True)
    return cfg


# 1h 加在 4h 之前：唐奇安策略的主周期是 1h（2026-11-02 五道关验证通过）。
# 加它的直接后果是本地聚合器多维护一个桶，看板 /api/bars?interval=1h 也能查。
# ★ 3s 仍是原子周期，只是列表变长——agg.py 的断言管的就是"3s 必须排第一"。
INTERVALS_MS: List[int] = [3000, 15000, 60000, 180000, 900000, 3600000, 14400000]
INTERVAL_NAMES = {3000: "3s", 15000: "15s", 60000: "1m", 180000: "3m",
                  900000: "15m", 3600000: "1h", 14400000: "4h"}
