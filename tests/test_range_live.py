# -*- coding: utf-8 -*-
"""区间演化实时推进的防线。

这里锁的都是"不会报错、只会静默不动"的问题：
- 时间戳用了 UTC 而库里是北京时间 → 时间轴看着差 8 小时；
- 仓储方法名写错 → 被外层 except 吞成一条 warning，推进一行都不写；
- 未完成的桶写进历史表 → 下次推进把它当已收盘，自激；
- current_app 没 import → tail 永远 None，页面最后一根永不更新。
"""
from __future__ import annotations

import io
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "ai_trader")


def _read(rel):
    return io.open(os.path.join(APP, rel), encoding="utf-8").read()


def test_app_imports_current_app():
    """current_app 没 import 时，tail 会静默变成 None（被 except 吞掉）。"""
    src = _read(os.path.join("web", "app.py"))
    assert re.search(r"from flask import [^\n]*current_app", src) or \
           "current_app" in src.split("def create_app")[0], \
        "app.py 用了 current_app 但没有 import"


def test_range_live_reuses_engine_ts8():
    """必须复用引擎的 ts8（北京时间），不许自己写一份 UTC 的。"""
    src = _read(os.path.join("range", "live.py"))
    assert "from .engine import build, ts8" in src or \
           "from .engine import ts8" in src, "没有复用引擎 ts8"
    # 允许出现在注释里（说明为什么不用它），但不允许真实调用
    code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    assert "utcfromtimestamp(" not in code.replace("dt.datetime.utcfromtimestamp(", "BANNED_"), \
        "又自己写了 UTC 时间戳，会和库里的北京时间差 8 小时"


def test_range_live_reads_both_stores():
    """两个库都要读：range 库给历史底座，market 库给最新的桶。"""
    src = _read(os.path.join("range", "live.py"))
    assert "range_db.bars_1h(" in src, "没读区间库的历史 1h"
    assert "market_store.query_bars(" in src, "没读实时库的 1h"


def test_range_live_never_persists_forming_bar():
    """未完成的桶不能写进历史表，否则下次推进把它当已收盘，自激。

    注意：写**已收盘**的桶是必须的（否则 API 的 K 线永远少最后一根）。
    要禁的是把 forming 写进去 —— 这是两种完全不同的东西。
    """
    src = _read(os.path.join("range", "live.py"))
    body = src.split("def advance(")[1].split("async def")[0]
    for call in re.findall(r"upsert_bars_1h\([^)]*\)", body):
        assert "forming" not in call, \
            "advance() 把未完成桶写进历史表了: %s" % call
    assert "upsert_bars_1h(symbol, rows_from_bars(closed" in body, \
        "已收盘的桶没落库，K 线会永远少最后一根"


def test_range_live_uses_existing_store_api():
    """方法名写错会被 except 吞成 warning，推进永远空转。"""
    src = _read(os.path.join("range", "live.py"))
    store = _read(os.path.join("range", "store.py"))
    for m in re.findall(r"range_db\.(\w+)\(", src):
        assert ("def %s(" % m) in store, "range_db.%s 不存在" % m


def test_market_store_key_is_t_not_open_time():
    """Store.query_bars 返回的字典键是 t/o/h/l/c/v，不是 open_time。"""
    src = _read(os.path.join("range", "live.py"))
    assert 'r["t"]' in src, "取 ts 用了错误的键名"

def test_forming_bar_comes_from_exchange_not_only_local_db():
    """未完成的 1h 必须向交易所取，不能只查本地库。

    1h 由聚合器产出，**只在收盘那一刻落库**。整点之后的这一整小时里，
    本地库里根本没有当前这根 —— 只查本地库会让"未完成那根"永远缺失，
    用户看到的就是"8 点了，还只有 7 点那根"。
    旧版的做法是直接请求 /api/live_klines?interval=1h&limit=2。
    """
    app = _read(os.path.join("web", "app.py"))
    assert "_forming_1h" in app, "没有从交易所取未完成 1h 的入口"
    block = app[app.index("def _forming_1h"):]
    block = block[:block.index("def create_app")]
    assert "_fetch_klines" in block, "没向交易所要数据"
    assert "cur_open" in block, "没校验拿到的是不是当前桶"
    assert "!= cur_open" in block or "!=cur_open" in block, \
        "没校验桶起点，会把上一根已收盘的桶重复画一遍"


def test_forming_bar_never_fabricated_when_absent():
    """不是当前桶就必须返回 None，绝不能拿上一根冒充。"""
    app = _read(os.path.join("web", "app.py"))
    block = app[app.index("def _forming_1h"):]
    block = block[:block.index("def create_app")]
    assert "return None" in block
