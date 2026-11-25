# -*- coding: utf-8 -*-
"""新鲜度闸的测试。

它守的是一个**已经真实发生过**的故障：kbar.db 静默停摆 6.5 天，
没人知道。所以这里重点不是「函数能跑」，而是：
  - 落后 6 天必须判 stale
  - **没有数据必须判 stale**（最坏情况不能算「没问题」）
  - 落后但没超阈值不能瞎报警
  - health.json 必须原子写（Web 永远读到完整文件）
  - 告警失败不能把整个检查搞挂
"""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = os.path.join(ROOT, "tools", "kbar_freshness.py")

T0 = 1_700_000_000_000 - (1_700_000_000_000 % 3_600_000)
HOUR = 3_600_000


def _load():
    spec = importlib.util.spec_from_file_location("kbar_freshness", SPEC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_db(path, ts_5m=None, ts_15m=None):
    con = sqlite3.connect(path)
    try:
        con.execute("CREATE TABLE IF NOT EXISTS kbar_evolution ("
                    " symbol TEXT, interval_ms INTEGER, ts INTEGER,"
                    " PRIMARY KEY (symbol, interval_ms, ts))")
        for iv, ts in ((300000, ts_5m), (900000, ts_15m)):
            if ts:
                con.execute("INSERT OR REPLACE INTO kbar_evolution"
                            " VALUES ('BTCUSDT', ?, ?)", (iv, ts))
        con.commit()
    finally:
        con.close()


def test_fresh_data_is_not_stale(tmp_path):
    mod = _load()
    db = str(tmp_path / "kbar.db")
    now = T0
    _make_db(db, ts_5m=now - 5 * 60_000, ts_15m=now - 15 * 60_000)
    snap = mod.check(db, max_lag_h=2.0, now_ms=now)
    assert snap["ok"] is True
    assert snap["layers"]["5m"]["stale"] is False
    assert snap["layers"]["15m"]["stale"] is False


def test_the_actual_incident_six_days_behind_is_stale(tmp_path):
    """复现 2026-11-10→11-16 那次：落后 6.5 天必须红。"""
    mod = _load()
    db = str(tmp_path / "kbar.db")
    now = T0
    six_and_half_days = int(6.5 * 24) * HOUR
    _make_db(db, ts_5m=now - six_and_half_days, ts_15m=now - six_and_half_days)
    snap = mod.check(db, max_lag_h=2.0, now_ms=now)
    assert snap["ok"] is False
    assert snap["layers"]["5m"]["stale"] is True
    assert snap["layers"]["15m"]["stale"] is True
    assert snap["layers"]["5m"]["lag_hours"] == pytest.approx(156.0, abs=0.5)


def test_missing_data_counts_as_stale_not_healthy(tmp_path):
    """**空库是最坏情况，绝不能被当成「没问题」。**

    这是这类闸最典型的自欺：`max(ts)` 返回 NULL → lag 算不出来 → 跳过检查 → 绿灯。
    一个从来没跑起来的层会一直显示健康。
    """
    mod = _load()
    db = str(tmp_path / "kbar.db")
    _make_db(db)                                  # 建表但没有行
    snap = mod.check(db, max_lag_h=2.0, now_ms=T0)
    assert snap["ok"] is False
    for lab in ("5m", "15m"):
        assert snap["layers"][lab]["stale"] is True
        assert snap["layers"][lab]["lag_hours"] is None


def test_missing_db_file_is_stale(tmp_path):
    mod = _load()
    snap = mod.check(str(tmp_path / "nope.db"), now_ms=T0)
    assert snap["ok"] is False


def test_threshold_is_respected(tmp_path):
    mod = _load()
    db = str(tmp_path / "kbar.db")
    now = T0
    _make_db(db, ts_5m=now - 3 * HOUR, ts_15m=now - 3 * HOUR)
    assert mod.check(db, max_lag_h=2.0, now_ms=now)["ok"] is False
    assert mod.check(db, max_lag_h=4.0, now_ms=now)["ok"] is True


def test_health_json_is_written_atomically(tmp_path):
    mod = _load()
    db = str(tmp_path / "kbar.db")
    _make_db(db, ts_5m=T0, ts_15m=T0)
    out = str(tmp_path / "health.json")
    snap = mod.check(db, now_ms=T0)
    mod.write_health(snap, out)
    with open(out, encoding="utf-8") as f:
        got = json.load(f)
    assert got["ok"] is True
    assert got["layers"]["5m"]["max_ts"] == T0
    # 不留临时文件
    assert not os.path.exists(out + ".tmp")


def test_alert_writes_into_the_main_log(tmp_path, monkeypatch):
    """告警必须进**运维平时 grep 的那个文件**，否则等于没发。"""
    mod = _load()
    monkeypatch.setattr(mod, "ROOT", str(tmp_path))
    (tmp_path / "logs").mkdir()
    snap = mod.check(str(tmp_path / "missing.db"), now_ms=T0)
    sent = mod.alert(snap, webhook="")            # 没 webhook
    assert sent is False
    log = tmp_path / "logs" / "ai-trader.log"
    assert log.exists()
    text = log.read_text(encoding="utf-8")
    assert "kbar_freshness" in text
    assert "WARNING" in text


def test_alert_never_raises_when_the_webhook_is_broken(tmp_path, monkeypatch):
    """告警通道本身故障时，检查流程不能被它带崩。"""
    mod = _load()
    monkeypatch.setattr(mod, "ROOT", str(tmp_path))
    snap = mod.check(str(tmp_path / "missing.db"), now_ms=T0)

    class _Boom:
        def __call__(self, *a, **k):
            raise RuntimeError("飞书挂了")

    monkeypatch.setattr("ai_trader.guard.feishu_notifier",
                        lambda url: _Boom())
    assert mod.alert(snap, webhook="https://example.invalid/hook") is False


def test_deploy_units_exist_and_match_the_runner():
    """timer/service 必须指向真实存在的脚本，且节奏是 **5 分钟**一次。

    ★ 节奏从每小时改成 5 分钟不是运维偏好：用户定了「入场全部在 5m K」，
      演化层晚一小时就绪 = 挂单晚一小时 = 策略失效。
      这条测试把节奏钉住，免得以后有人为了省 CPU 悄悄改回去。
    """
    svc = os.path.join(ROOT, "deploy", "ai-trader-kbar.service")
    tmr = os.path.join(ROOT, "deploy", "ai-trader-kbar.timer")
    for p in (svc, tmr):
        assert os.path.exists(p), "缺 %s" % p
    svc_text = open(svc, encoding="utf-8").read()
    tmr_text = open(tmr, encoding="utf-8").read()
    assert "kbar_tick.sh" in svc_text, "service 没指向 kbar_tick.sh"
    assert os.path.exists(os.path.join(ROOT, "tools", "kbar_tick.sh"))
    assert "OnCalendar" in tmr_text
    assert "Persistent=true" in tmr_text, "关机期间错过的也要补跑"
    assert "WantedBy=timers.target" in tmr_text

    # 5 分钟节奏：分钟位必须是 */5 形式，不能退回整点
    assert ":00/5:" in tmr_text or "*/5" in tmr_text, (
        "kbar timer 必须 5 分钟一次（入场在 5m 上，晚一小时就是策略失效）：\n%s"
        % tmr_text)
    # 5 分钟节奏下，单次超时不能还是 1800s，否则会叠起来
    assert "TimeoutStartSec=600" in svc_text, svc_text


def test_tick_script_feeds_5m_from_1m_as_a_fallback():
    """★ kbar_tick.sh 的 5m 那一轮必须带 --from-1m。

    真实事故：老库里 market.db 没有原生 5m（5m 到 2026-11-09 才进
    config.INTERVALS_MS），脚本只传 --interval-ms 300000，于是那一轮永远拿空输入。
    配合当时「空输入算成功」的 ingest，5m 演化层停在 11-01、比 15m 少 7 天，
    而每小时任务全部报成功。两边都堵上之后，这条测试守住脚本这一半。
    """
    p = os.path.join(ROOT, "tools", "kbar_tick.sh")
    text = open(p, encoding="utf-8").read()
    flat = text.replace('"', "")            # 去引号，免得为 shell 的写法分心
    assert "--interval-ms $IV --from-1m" in flat, (
        "5m 那一轮没有 --from-1m 回退：老库会拿空输入。\n%s" % text)
    assert 'if [ "$IV" = "300000" ]' in text, "回退必须只作用在 5m 上"
    # 轻量路径必须只到 facts，否则每 5 分钟做一次全表重算
    assert "--until facts" in text, "5 分钟路径必须只跑 lines+facts"
    # 全量层要留在整点，不能被 5 分钟节奏拖着跑
    assert "kbar_full_hour" in text, "缺「整点才跑全量层」的判断"
