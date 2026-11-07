# -*- coding: utf-8 -*-
"""核对纸面权益的三个数：本金、已实现、权益。三者必须自洽。

100(本金) - 20.46(已实现净利) = 79.54，而权益显示 71.77，差 7.77U。
如果存在未平仓，差额应等于浮亏；如果空仓，就是对不上。
"""
import os
import sqlite3

P = os.environ.get("AI_TRADE_DB", "data/market_trade.db")
c = sqlite3.connect(P)
c.row_factory = sqlite3.Row

print("=" * 64)
print("纸面权益三数自洽性")
print("=" * 64)

# 1) 执行器状态
row = c.execute("SELECT * FROM executor_state ORDER BY ts DESC LIMIT 1").fetchone()
if row:
    print("executor_state:")
    for k in row.keys():
        print("   %-12s %s" % (k, row[k]))
else:
    print("executor_state: 无记录")

# 2) 未平仓交易
ot = c.execute("SELECT * FROM trades WHERE exit_ts IS NULL").fetchall()
print()
print("未平仓 trades: %d 笔" % len(ot))
for r in ot:
    print("   id=%s side=%s qty=%s entry=%s entry_ts=%s"
          % (r["id"], r["side"], r["qty"], r["entry_price"], r["entry_ts"]))

# 3) 全部 trades
rows = c.execute("SELECT id, side, qty, entry_price, exit_price, pnl_gross,"
                 " fee, pnl_net, entry_ts, exit_ts FROM trades ORDER BY id").fetchall()
print()
print("全部 trades: %d 笔" % len(rows))
tot_net = 0.0
for r in rows:
    state = "已平" if r["exit_ts"] else "未平!!"
    tot_net += (r["pnl_net"] or 0)
    print("   #%s %-5s qty=%-10s %s->%s 净利=%s  %s"
          % (r["id"], r["side"], r["qty"], r["entry_price"],
             r["exit_price"], r["pnl_net"], state))
print()
print("已实现净利合计: %.6f" % tot_net)

# 4) 最新权益
eq = c.execute("SELECT ts, equity, balance, unrealized FROM equity ORDER BY ts DESC LIMIT 1").fetchone()
print()
print("最新权益记录: ts=%s equity=%s balance=%s unrealized=%s"
      % (eq["ts"], eq["equity"], eq["balance"], eq["unrealized"]))

print()
print("=" * 64)
print("自洽性检查")
print("=" * 64)
bal = eq["balance"] if eq["balance"] is not None else None
if bal is not None:
    expect = 100.0 + tot_net
    print("本金 100 + 已实现 %.4f = %.4f" % (tot_net, expect))
    print("执行器 balance      = %s" % bal)
    print("差额                = %.6f" % (bal - expect))
    if abs(bal - expect) > 1e-6:
        print("  -> balance 和已实现净利对不上")
c.close()
