# -*- coding: utf-8 -*-
"""区间演化（v11 四线）。

本包是从 automate-k-trader 迁移过来的 v11 权威实现：
    engine.py  区间引擎 v9.1（M1–M7，见文件头改动清单）
    api.py     生产桥接层，对外只暴露 current() / history() / parity()

v11 的四条线（唯一命名，禁止混用）：
    S  软边界      soft_up / soft_dn   不参与任何触发判定
    R  反向边界    hard_up / hard_dn   被收盘越过 ⇒ 反转，换家族
    P  顺向边界    lo_b / hi_b         被收盘越过 ⇒ 突破，换段
    K  顺向关键价  key                 每根 min/max，无滞后，只记录
"""
from .engine import build, load_1h, range_at, ts8, fmt, TOL, WARMUP_1H_BARS
from .api import current, history

__all__ = ["build", "load_1h", "range_at", "ts8", "fmt", "TOL",
           "WARMUP_1H_BARS", "current", "history"]
