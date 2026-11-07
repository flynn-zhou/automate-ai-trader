# -*- coding: utf-8 -*-
"""把系统从剥头皮切换到趋势策略：平掉旧仓、对齐执行器状态、改配置。

为什么需要专门的脚本：
1. 旧策略可能还有未平仓单，引擎重启会把它在趋势模式下"接管"，
   而趋势模式的仓位算法完全不同（1.5x 而不是 47x），状态会错乱。
2. executor_state 必须和真实权益对齐，否则重启后权益显示 100.0（这条路踩过坑）。
3. 切换必须幂等：重复跑不会把已平的仓再平一次。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/market_trade.db")
    ap.add_argument("--market-db", default="data/market.db")
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()

    c = sqlite3.connect(a.db)
    c.row_factory = sqlite3.Row
    now_ms = int(time.time() * 1000)

    # 1) 取最新价（K 线在 market.db，交易表在 market_trade.db，别搞混）
    px = None
    if os.path.exists(a.market_db):
        m = sqlite3.connect(a.market_db)
        try:
            r = m.execute("SELECT c FROM bars WHERE interval_ms=3000"
                          " ORDER BY open_time DESC LIMIT 1").fetchone()
            if r:
                px = float(r[0])
        finally:
            m.close()
    if not px:
        print("拿不到最新价，中止"); return
    print("最新价 %.2f" % px)

    # 2) 平掉未平仓位
    rows = c.execute("SELECT * FROM trades WHERE exit_ts IS NULL").fetchall()
    closed = []
    for t in rows:
        d = 1 if t["side"] == "long" else -1
        gross = (px - t["entry_price"]) * t["qty"] * d
        fee = (t["entry_price"] + px) * t["qty"] * 0.0005
        net = gross - fee
        rc = "策略切换：剥头皮仓位强制平仓"
        if not a.dry:
            c.execute("UPDATE trades SET exit_ts=?, exit_price=?, pnl_gross=?, fee=?,"
                      " pnl_net=?, close_reason=? WHERE id=?",
                      (now_ms, px, gross, fee, net, rc, t["id"]))
        closed.append((t["id"], t["side"], t["qty"], net))
        print("  平仓 id=%s %s %.3f -> 净 %+.3fU" % (t["id"], t["side"], t["qty"], net))

    # 3) 重算执行器状态：balance = 上一笔 balance + 本次平仓净额
    st = c.execute("SELECT balance FROM executor_state ORDER BY ts DESC LIMIT 1").fetchone()
    bal = float(st["balance"]) if st else 100.0
    for _, _, _, net in closed:
        bal += net
    bal = max(0.0, round(bal, 6))
    if not a.dry:
        # executor_state 是单行表（CHECK id=1），只能 UPDATE/REPLACE，不能裸 INSERT
        c.execute("INSERT OR REPLACE INTO executor_state(id,ts,balance,pos_qty,pos_entry)"
                  " VALUES(1,?,?,0,0)", (now_ms, bal))
        c.commit()
    print("执行器余额对齐为 %.4fU（未平仓 0）" % bal)

    # 4) 剩余未平仓检查
    left = c.execute("SELECT COUNT(*) FROM trades WHERE exit_ts IS NULL").fetchone()[0]
    print("剩余未平仓：%d" % left)
    c.close()
    if a.dry:
        print("（dry run，未写入）")


if __name__ == "__main__":
    main()
