# -*- coding: utf-8 -*-
"""v11 引擎迁移保真度测试（金标准比对）。

为什么要有这个文件
------------------
v11 引擎是从 automate-k-trader 迁移过来的。迁移过程中最容易犯的错不是语法，
而是「看起来跑通了，其实语义变了」——改了路径、改了导入、顺手重排了一个分支，
输出就悄悄偏了，而没有任何测试会红。

这里用**金标准库**锁死：fixtures/range_v11_reference.db 是老项目在生产上
实际跑出来的 1439 根四线状态。迁移后的引擎必须逐根复现它们。

⚠️ CSV 必须与金标准库同源
--------------------------
曾经踩过：库里 1439 行（≈60 天），却喂了一份 100 天的 CSV，结果 373 处不一致。
根因不是代码错，是**预热窗口落点不同**导致早期那些根「在库里还没成形、
在引擎里已出生」。所以夹具 CSV 必须固定，不能用运行时那份最新数据。

口径提醒（与 engine.py 保持一致）
--------------------------------
* soft_up/soft_dn 是软边界 S，hard_up/hard_dn 是反向边界 R，key 是关键价 K
* DOWN 区间的 K 恒为 None；None 与 None 相等，不能当 0 处理
* ts 是 UTC 毫秒，ts_h 是北京时区字符串
"""
from __future__ import annotations

import importlib
import os
import sqlite3

import pytest

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
REF_DB = os.path.join(FIXTURES, "range_v11_reference.db")
REF_CSV = os.path.join(FIXTURES, "range_bars_15m_v11_ref.csv")

LINES = ("soft_up", "soft_dn", "hard_up", "hard_dn", "key")


@pytest.fixture()
def engine_on_reference_csv(monkeypatch):
    """把引擎锁到与金标准库同源的那份 CSV 上。"""
    if not (os.path.exists(REF_DB) and os.path.exists(REF_CSV)):
        pytest.skip("缺少 v11 金标准夹具")
    monkeypatch.setenv("RANGE_BARS_CSV", REF_CSV)
    from ai_trader.range import engine
    importlib.reload(engine)
    yield engine
    # 还原，避免污染同进程内其他测试
    monkeypatch.undo()
    importlib.reload(engine)


def test_engine_reproduces_golden_reference_bit_for_bit(engine_on_reference_csv):
    """核心断言：迁移后的引擎必须逐根复现 1439 根四线状态，0 处不一致。"""
    from ai_trader.range import api

    bars = engine_on_reference_csv.load_1h(verbose=False)
    con = sqlite3.connect(REF_DB)
    ref = {r[0]: r for r in con.execute(
        "SELECT ts_h,soft_up,soft_dn,hard_up,hard_dn,key FROM range_bars")}
    con.close()

    hist = api.history(bars)
    compared = 0
    bad = []
    for row in hist:
        want = ref.get(row["ts_h"])
        if want is None:
            continue
        compared += 1
        for i, k in enumerate(LINES, start=1):
            got_v, want_v = row[k], want[i]
            if (got_v is None) != (want_v is None):
                bad.append((row["ts_h"], k, got_v, want_v))
            elif got_v is not None and abs(got_v - want_v) > 1e-9:
                bad.append((row["ts_h"], k, got_v, want_v))

    assert compared >= 1400, "比对根数过少（%d），夹具或 CSV 有问题" % compared
    assert not bad, "与金标准不一致 %d 处，前 5 条: %s" % (len(bad), bad[:5])


def test_warmup_is_600(engine_on_reference_csv):
    """预热口径必须与老项目一致（实测 5 品种 69 切点全局最大 458，取上界 600）。"""
    assert engine_on_reference_csv.WARMUP_1H_BARS == 600
    assert engine_on_reference_csv.TOL == 30.0


def test_four_lines_are_named_consistently(engine_on_reference_csv):
    """v11 纪律：R/P/K/S 四个量各有唯一名字，不得混用。"""
    from ai_trader.range import api

    bars = engine_on_reference_csv.load_1h(verbose=False)
    hist = api.history(bars)
    assert hist, "引擎没有产出任何状态"
    row = hist[-1]
    for k in ("soft_up", "soft_dn", "hard_up", "hard_dn", "key",
              "family", "segment", "direction"):
        assert k in row, "缺少字段 %s" % k
    assert row["direction"] in ("UP", "DOWN")


def test_none_is_none_not_zero(engine_on_reference_csv):
    """该根没有的边界必须是 None，不能被填 0 或前值（伪造数据）。"""
    from ai_trader.range import api

    hist = api.history(engine_on_reference_csv.load_1h(verbose=False))
    zeros = [r for r in hist
             if any(r[k] == 0 for k in LINES)]
    assert not zeros, "出现了价格为 0 的边界，说明 None 被填成了 0: %s" % zeros[:2]
