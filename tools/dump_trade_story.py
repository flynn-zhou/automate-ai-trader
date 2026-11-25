# -*- coding: utf-8 -*-
"""只读：把这笔交易的完整事实链摊开（trades / orders / risk_events / 预期）。

回答用户的问题必须靠数据，不靠回忆：
  1. 系统到底记了几笔仓、entry 各是多少？
  2. 我方挂单的 client_id 与交易所成交价是什么？
  3. 平仓是谁触发的、记在什么价？
"""
import sqlite3
import time

TRADE = "/home/cryptobot/ai-trader/data/market_trade.db"
PEND = "/home/cryptobot/ai-trader/data/market_pending.db"


def ro(path):
    return sqlite3.connect("file:%s?mode=ro" % path, uri=True)


def fmt(ms):
    try:
        return time.strftime("%m-%d %H:%M:%S", time.localtime(int(ms) / 1000.0))
    except (TypeError, ValueError):
        return str(ms)


print("=" * 78)
print("① trades（最近 8 笔）")
print("=" * 78)
c = ro(TRADE)
try:
    cols = [r[1] for r in c.execute("PRAGMA table_info(trades)")]
    print("列：", ", ".join(cols))
    for r in c.execute("select * from trades order by id desc limit 8"):
        d = dict(zip(cols, r))
        print("\n  id=%s %s %s qty=%s entry=%.4f exit=%s"
              % (d.get("id"), fmt(d.get("entry_ts")), d.get("side"),
                 d.get("qty"), float(d.get("entry_price") or 0),
                 d.get("exit_price")))
        print("     开仓 %s → 平仓 %s｜止损 %s 目标 %s"
              % (fmt(d.get("entry_ts")), fmt(d.get("exit_ts")),
                 d.get("stop_price"), d.get("target_price")))
        print("     毛 %s 费 %s 净 %s｜paper=%s｜开仓理由=%s"
              % (d.get("pnl_gross"), d.get("fee"), d.get("pnl_net"),
                 d.get("paper"), (d.get("open_reason") or "")[:40]))
        print("     平仓理由=%s" % (d.get("close_reason") or "")[:70])
finally:
    c.close()

print("\n" + "=" * 78)
print("② orders（最近 10 条，看我方挂单与撤单）")
print("=" * 78)
c = ro(TRADE)
try:
    cols = [r[1] for r in c.execute("PRAGMA table_info(orders)")]
    for r in c.execute("select * from orders order by id desc limit 10"):
        d = dict(zip(cols, r))
        print("  %s %-26s %-9s %-4s %-11s qty=%-8s px=%-9s stop=%-9s %s"
              % (fmt(d.get("ts")), (d.get("client_id") or "")[:26],
                 d.get("status"), d.get("side"), d.get("type"),
                 d.get("qty"), d.get("price"), d.get("stop_price"),
                 (d.get("reason") or "")[:26]))
finally:
    c.close()

print("\n" + "=" * 78)
print("③ risk_events 近 3 小时（去重计数 + 最后 14 条）")
print("=" * 78)
c = ro(TRADE)
try:
    since = int(time.time() * 1000) - 3 * 3600 * 1000
    for kind, n in c.execute("select kind, count(*) from risk_events "
                             "where ts>=? group by kind order by 2 desc",
                             (since,)):
        print("  %-34s %d" % (kind, n))
    print("\n  最后 14 条：")
    for ts, kind, detail in c.execute(
            "select ts, kind, substr(detail,1,88) from risk_events "
            "where ts>=? order by id desc limit 14", (since,)):
        print("  %s %-28s %s" % (fmt(ts), kind, detail))
finally:
    c.close()

print("\n" + "=" * 78)
print("④ pending theses（全部）")
print("=" * 78)
c = ro(PEND)
try:
    cols = [r[1] for r in c.execute("PRAGMA table_info(pending_theses)")]
    for r in c.execute("select * from pending_theses order by rowid desc"):
        d = dict(zip(cols, r))
        print("  %s\n     state=%s dir=%s 挂单 %.2f 止损 %.2f 目标 %.2f"
              % (d.get("thesis_id"), d.get("state"), d.get("direction"),
                 float(d.get("order_price") or 0), float(d.get("stop_price") or 0),
                 float(d.get("target_price") or 0)))
        print("     armed_bar=%s armed_sub=%s next=%s filled_ts=%s "
              "filled_px=%s exit_ts=%s exit_px=%s net_r=%s"
              % (d.get("armed_at_bar"), d.get("armed_at_sub_ts"),
                 d.get("next_index"), fmt(d.get("filled_ts")),
                 d.get("filled_price"), fmt(d.get("exit_ts")),
                 d.get("exit_price"), d.get("net_r")))
finally:
    c.close()
