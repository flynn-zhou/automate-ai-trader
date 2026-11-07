"""v11 区间演化的生产桥接层。

用途：把 range_engine_v9 的批次引擎包装成生产可直接调用的接口。
生产侧 RangeOriginTracker._accumulate_extremes 目前用的是旧语义
(main_lower/main_upper = 起源以来极值，单调不减、窗口无界)，与本模块的四条线不同。

对外只暴露两个函数，避免生产侧依赖引擎内部元组结构：
    current(bars)  -> dict | None   最后一根的区间状态
    history(bars)  -> list[dict]    逐根状态
"""
import os, sys

from . import engine as E

FIELDS = ("ts", "soft_up", "soft_dn", "hard_up", "hard_dn", "key", "family", "segment", "direction", "tag")


def _row(s):
    ts, lo_b, hi_b, _ss, d, hard, soft, key, tag, fam, seg = s
    if d == "DOWN":
        su, sd, hu, hd = soft, lo_b, hi_b, None
    elif d == "UP":
        su, sd, hu, hd = hi_b, soft, None, lo_b
    else:
        su = sd = hu = hd = None
    # 顺向边界 P：下跌区间是下沿 lo_b，上涨区间是上沿 hi_b。
    # v11 纪律要求 P 与 K 分开命名——K 在 key 里，绝不能再把 P 叫成「关键价」。
    p = lo_b if d == "DOWN" else (hi_b if d == "UP" else None)
    return dict(ts=ts, ts_h=E.ts8(ts), soft_up=su, soft_dn=sd, hard_up=hu,
                hard_dn=hd, key=key, p=p, family=fam, segment=seg,
                direction=d, tag=tag,
                label=("#%d.%d" % (fam, seg)) if fam else "")


def history(bars):
    """逐根返回 v11 区间状态（四条线 + 家族/段/方向）。"""
    _ev, st = E.build(bars)
    return [_row(s) for s in st]


def current(bars):
    """返回最后一根的区间状态；尚未成形则返回 None。"""
    h = history(bars)
    for r in reversed(h):
        if r["family"] and r["segment"]:
            return r
    return None


def as_of(when, bars=None, soft_overrides=None):
    """给指定时间，返回**当时**的区间状态（v11 四线 + 家族/段/方向/P/K）。

    这是「按时间点回看区间」的功能型入口：不关心现在的区间是什么，
    只回答「2026-10-23 00:00 那一刻，区间画在哪、是哪一段」。

    when : "YYYY-MM-DD HH:MM"（北京时区，与页面显示一致）或 UTC 毫秒整数
    bars : 可选，复用已加载的 1h bars（批量查询时避免重复读盘）
    返回 : dict | None —— 早于第一根数据时返回 None（不是空 dict，免得调用方
           把「没有数据」误读成「区间未成形」）
    """
    if bars is None:
        bars = E.load_1h(verbose=False)
    t = E.lab(when) if isinstance(when, str) else int(when)
    _ev, st = E.build(bars, soft_overrides)
    best = None
    for s in st:
        if s[0] <= t:
            best = s
        else:
            break
    if best is None:
        return None
    row = _row(best)
    # 顺带把那根 K 线本身带上：用户要的是「那根 1h K 线对应的区间」，
    # 只给四条线而不给价格，读者无法判断区间相对价格的位置。
    for b in bars:
        if b[0] == best[0]:
            row["bar"] = {"open": b[1], "high": b[2], "low": b[3], "close": b[4],
                          "volume": b[5] if len(b) > 5 else None}
            break
    else:
        row["bar"] = None
    row["requested"] = when if isinstance(when, str) else E.ts8(int(when))
    return row


def as_of_rows(when_list, bars=None, soft_overrides=None):
    """批量版 as_of：一次加载 bars，逐时间点返回，供页面表格/API 使用。"""
    if bars is None:
        bars = E.load_1h(verbose=False)
    return [as_of(w, bars=bars, soft_overrides=soft_overrides) for w in when_list]


def parity(n_sample=0):
    """自检：本模块输出必须与 range_v11.db 的 range_bars 逐根一致。"""
    import sqlite3
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    db = os.path.join(root, "tests", "fixtures", "range_v11_reference.db")
    con = sqlite3.connect(db)
    db_rows = {r[0]: r for r in con.execute(
        "SELECT ts_h,soft_up,soft_dn,hard_up,hard_dn,key,family,segment,direction FROM range_bars")}
    con.close()
    bad = 0; n = 0
    for r in history(E.load_1h(verbose=False)):
        db = db_rows.get(r["ts_h"])
        if db is None:
            continue
        n += 1
        for i, k in enumerate(("soft_up", "soft_dn", "hard_up", "hard_dn", "key"), start=1):
            a, b = r[k], db[i]
            if (a is None) != (b is None) or (a is not None and abs(a - b) > 0.01):
                bad += 1
                if bad <= 5:
                    print("  X %s %s 桥接=%s DB=%s" % (r["ts_h"], k, a, b))
    print("parity: %d 根比对，%d 处不一致" % (n, bad))
    return bad


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    bad = parity()
    bars = E.load_1h(verbose=False)
    c = current(bars)
    print("current():", {k: (round(v, 1) if isinstance(v, float) else v) for k, v in c.items()})
    sys.exit(1 if bad else 0)