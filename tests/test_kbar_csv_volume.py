# -*- coding: utf-8 -*-
"""`load_csv_bars` 必须能读量能列。

## 这条测试补的是一个生产事故

第一版 `load_csv_bars` 只读 `ts,o,h,l,c`，硬编码 `"v": None`。
注释写着「只有结构，没有量」—— 那在当时那份 CSV 上是事实，
但写成**硬性**约定就成了缺陷：

`hist.db.k2` 里 5m 有 752,126 根、15m 有 250,710 根，从 2019 年起，
**每一根都有 v**。导成 CSV 时被那一行 `"v": None` 直接丢掉了。

后果不是报错，是整类结论静默失效（生产实测）：
  - 5m 表 1 的 `vol_rel`：0 / 51,299 有值
  - `恐慌盘` 一次都不出现（它的必要条件就是量能）
  - 5 个量能类维度全是空

`check_kbar_pipeline.py` 当时报的是「恐慌盘是死条件」——
那是**症状**，根因在这一行。修掉之后 15m 的 vol_rel 从 7.8% → 99.9%，
恐慌盘 0 → 58 根。

这条测试钉住「有 v 列就必须读」和「没有 v 列不许报错」两件事。
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ai_trader.kbar import build as B            # noqa: E402


def _write(tmp_path, text, name="bars.csv"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_csv_with_volume_reads_volume(tmp_path):
    """有 v 列 → 必须读出来，不能硬编码成 None。"""
    p = _write(tmp_path,
               "ts,o,h,l,c,v\n"
               "1000,1.0,2.0,1.0,1.5,42.5\n"
               "2000,1.0,2.0,1.0,1.6,43.5\n")
    rows = B.load_csv_bars(p)
    assert len(rows) == 2
    assert rows[0]["v"] == pytest.approx(42.5)
    assert rows[1]["v"] == pytest.approx(43.5)


def test_csv_without_volume_still_works(tmp_path):
    """旧的 5 列 CSV 必须照常能读（不能因为加量能就要求所有源都有）。"""
    p = _write(tmp_path,
               "ts,o,h,l,c\n"
               "1000,1.0,2.0,1.0,1.5\n")
    rows = B.load_csv_bars(p)
    assert len(rows) == 1
    assert rows[0]["v"] is None
    assert rows[0]["buy_vol"] is None


def test_csv_blank_volume_is_none_not_zero(tmp_path):
    """空字符串必须是 None，不能变成 0.0。

    0 和「没有这个数」是两件事：`vol_rel` 的分母是中位量，
    把缺失写成 0 会污染中位数，而中位数一旦偏小，
    所有「放量」判断都会误报。
    """
    p = _write(tmp_path,
               "ts,o,h,l,c,v\n"
               "1000,1.0,2.0,1.0,1.5,\n"
               "2000,1.0,2.0,1.0,1.6,43.5\n")
    rows = B.load_csv_bars(p)
    assert rows[0]["v"] is None, "空 v 被当成了 %r" % (rows[0]["v"],)
    assert rows[1]["v"] == pytest.approx(43.5)


def test_csv_sorts_by_ts(tmp_path):
    """乱序 CSV 必须排好 —— 下游所有「前 N 根」的算法都假设有序。"""
    p = _write(tmp_path,
               "ts,o,h,l,c,v\n"
               "3000,1,2,1,1.5,3\n"
               "1000,1,2,1,1.5,1\n"
               "2000,1,2,1,1.5,2\n")
    rows = B.load_csv_bars(p)
    assert [r["ts"] for r in rows] == [1000, 2000, 3000]


def test_csv_buy_volume_is_optional(tmp_path):
    """`buy_vol` 列可有可无；有就读。"""
    p = _write(tmp_path,
               "ts,o,h,l,c,v,buy_vol\n"
               "1000,1,2,1,1.5,42.5,20.1\n")
    rows = B.load_csv_bars(p)
    assert rows[0]["buy_vol"] == pytest.approx(20.1)
