# -*- coding: utf-8 -*-
"""挂单的 LineBook 必须装齐区间演化的六条线（2026-11-19）。

事故形状：`main.py::kbar_lines_for` 里有一张**硬编码的 4 键名单**

    (SLOW_LINE_KEY, FAST_LINE_KEY, "range_soft_up@1h", "range_soft_dn@1h")

而 `kbar_line` 里其实还有 range_p@1h(44k 行)、range_key@1h(45k 行)、
range_hard_up@1h(20k 行)、range_hard_dn@1h(20k 行)——一条都没装。

后果不是报错，是**静默**：`LineBook.at("range_p@1h", ts)` 对每一根都返回
None，「这条线没装」和「这根上确实没有值」长得一模一样。于是：

  1. S5 想用区间演化的腿（P 顺向边界 / R 反向边界 / K 关键价）永远取不到值；
  2. 产生了一个**假测量**——「1h 的 R 落在止损内 0/11」当时被当成几何结论，
     实际是因为 R 一直是 None，所以这个数字恒等于 0。

这类缺陷是本仓库的老熟人：**同一件事，两个来源**。写入侧用
`RANGE_LINE_KINDS` 这个常量决定写哪几条，读取侧另写一份名单——
两边一旦不同步，不报错、不告警，只是「线消失了」。

所以修法是**从同一个常量推导**，而不是把名单补成 8 个键。
这条测试钉的就是「读的名单等于写的名单」，而不是硬编码那六个字符串。
"""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.kbar.lines import RANGE_LINE_KINDS
from ai_trader.trading import thesis as T

#: 一根"已收盘"的 1h 的 ts 与就绪时刻
TS = 1794000000000
READY = TS + 3_600_000


def _mkdb(tmp, rows):
    """造一个最小 kbar_line 表。"""
    path = os.path.join(tmp, "kbar.db")
    con = sqlite3.connect(path)
    con.execute("create table kbar_line ("
                "line_key text, ts integer, line_price real,"
                " line_ready_ms integer)")
    con.executemany("insert into kbar_line values (?,?,?,?)", rows)
    con.commit()
    con.close()
    return path


def _cfg(tmp):
    from ai_trader.config import AppCfg
    cfg = AppCfg()
    cfg.store.path = os.path.join(tmp, "market.db")
    cfg.trading.strategy = "pending"
    return cfg


def _rows(price_map):
    """price_map: line_key -> 价格。"""
    return [(k, TS, v, READY) for k, v in price_map.items()]


def _all_keys():
    """读取侧应当装载的全部线：两条均线 + 六条区间线（由常量推导）。"""
    return [T.SLOW_LINE_KEY, T.FAST_LINE_KEY] + [
        "%s@1h" % k for k in RANGE_LINE_KINDS]


def test_every_range_line_the_writer_emits_is_loaded():
    """★★ 核心：写的名单 == 读的名单。

    不硬编码六个键——用 RANGE_LINE_KINDS 推导，
    以后加第七第八条线时这条测试会自动跟着要求它被装载。
    """
    from ai_trader.main import kbar_lines_for

    prices = {}
    for i, k in enumerate(_all_keys()):
        prices[k] = 100.0 + i
    with tempfile.TemporaryDirectory() as tmp:
        _mkdb(tmp, _rows(prices))
        book = kbar_lines_for(_cfg(tmp))()

    for k in _all_keys():
        pt = book.at(k, READY)
        assert pt is not None, (
            u"线 %s 没被装进 LineBook —— 写侧在产出它，读侧看不到，"
            u"而调用方只会拿到 None（= 这条线在这根上不存在）" % k)
        assert pt.price == prices[k], (k, pt.price)


def test_the_four_originally_loaded_lines_still_work():
    """反向控制：原来那 4 条必须不回归（它们是 ema 失效判定的全部依据）。"""
    from ai_trader.main import kbar_lines_for

    prices = {T.SLOW_LINE_KEY: 101.0, T.FAST_LINE_KEY: 102.0,
              "range_soft_up@1h": 103.0, "range_soft_dn@1h": 104.0}
    with tempfile.TemporaryDirectory() as tmp:
        _mkdb(tmp, _rows(prices))
        book = kbar_lines_for(_cfg(tmp))()

    for k, v in prices.items():
        pt = book.at(k, READY)
        assert pt is not None and pt.price == v, (k, pt)


def test_book_is_rebuilt_only_when_the_table_changes():
    """缓存：同样的表内容 → 同一个 book 对象；有新行 → 必须重建。

    LineBook 是表内容的纯函数。没有缓存的话每根新 5m 都要重建一次
    （实测 0.9 秒 / 108900 点，装齐后 1.95 秒 / 240000 点）。
    """
    from ai_trader.main import kbar_lines_for

    prices = {k: 100.0 + i for i, k in enumerate(_all_keys())}
    with tempfile.TemporaryDirectory() as tmp:
        path = _mkdb(tmp, _rows(prices))
        build = kbar_lines_for(_cfg(tmp))
        b1 = build()
        b2 = build()
        assert b1 is b2, u"表没变却重建了 LineBook（每根新 5m 都在白花 2 秒）"

        # 补一根新行 → 签名变 → 必须看到新内容
        con = sqlite3.connect(path)
        con.execute("insert into kbar_line values (?,?,?,?)",
                    ("range_p@1h", TS + 3_600_000, 999.0,
                     TS + 7_200_000))
        con.commit()
        con.close()

        b3 = build()
        assert b3 is not b1, u"表变了却还是老 book：会拿昨天的线判今天"
        assert b3.at("range_p@1h", TS + 7_200_000).price == 999.0


def test_cache_signature_notices_a_backfill_that_keeps_max_ts(tmp_path):
    """补历史（max_ts 不变、行数变）也必须被发现。

    只看 max(ts) 是不够的：回补老 K 线时最大 ts 一点不变，
    签名单看时间戳就会一直吃老缓存。
    """
    from ai_trader.main import kbar_lines_for

    prices = {"range_p@1h": 100.0}
    tmp = str(tmp_path)
    path = _mkdb(tmp, _rows(prices))
    build = kbar_lines_for(_cfg(tmp))
    b1 = build()

    con = sqlite3.connect(path)
    # 同一时刻、更老的一根：max(ts) 不变，只有 rowid 前进
    con.execute("insert into kbar_line values (?,?,?,?)",
                ("range_key@1h", TS - 3_600_000, 55.0, TS))
    con.commit()
    con.close()

    b2 = build()
    assert b2 is not b1, u"补历史没有被发现（只看 max(ts) 的漏洞）"
    assert b2.at("range_key@1h", TS).price == 55.0
