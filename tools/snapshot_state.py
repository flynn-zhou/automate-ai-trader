# -*- coding: utf-8 -*-
"""把当前态势压成一段可交接的文字，写进研究日志并打印。

用法（每隔一段时间跑一次，或部署脚本收尾时跑）：
    python tools/snapshot_state.py --journal
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ai_trader.journal import Journal  # noqa: E402


def q(conn, sql, args=()):
    try:
        return conn.execute(sql, args).fetchall()
    except Exception:
        return []


def build(db_path: str) -> dict:
    c = sqlite3.connect(db_path)
    s = {}
    s["trades"] = q(c, "SELECT COUNT(*), SUM(CASE WHEN pnl_net>0 THEN 1 ELSE 0 END),"
                       " SUM(pnl_net) FROM trades WHERE exit_ts IS NOT NULL")[0]
    s["open_trade"] = q(c, "SELECT side,qty,entry_price,stop_price,target_price,entry_ts"
                           " FROM trades WHERE exit_ts IS NULL ORDER BY id DESC LIMIT 1")
    s["decisions"] = q(c, "SELECT action, COUNT(*) FROM decisions GROUP BY action")
    s["signals"] = q(c, "SELECT signal, COUNT(*) FROM decisions"
                        " WHERE signal IS NOT NULL GROUP BY signal")
    s["equity_last"] = q(c, "SELECT equity FROM equity ORDER BY ts DESC LIMIT 1")
    s["recent_journal"] = q(c, "SELECT kind,title,ts FROM journal ORDER BY id DESC LIMIT 5")
    s["risk_events"] = q(c, "SELECT kind,detail,ts FROM risk_events ORDER BY id DESC LIMIT 8")
    c.close()
    return s


def render(s: dict) -> str:
    lines = []
    t = s["trades"] if s["trades"] else (0, 0, 0)
    lines.append("已平仓 %d 笔，胜 %s，净盈亏 %.3fU" % (t[0] or 0, t[1] or 0, t[2] or 0.0))
    if s["open_trade"]:
        o = s["open_trade"][0]
        lines.append("当前持仓：%s %.3f @ %.2f 止损 %.2f 目标 %.2f（开仓 %s）"
                     % (o[0], o[1], o[2], o[3] or 0, o[4] or 0,
                        time.strftime("%H:%M:%S", time.localtime((o[5] or 0) / 1000))))
    else:
        lines.append("当前空仓")
    lines.append("决策分布：" + "，".join("%s=%d" % (a, n) for a, n in s["decisions"]))
    if s["signals"]:
        lines.append("触发过的信号：" + "，".join("%s=%d" % (a, n) for a, n in s["signals"]))
    if s["equity_last"]:
        lines.append("最新权益：%.4fU" % s["equity_last"][0][0])
    if s["risk_events"]:
        lines.append("最近风控事件：" + "；".join(
            "%s(%s)" % (k, str(d)[:40]) for k, d, _ in s["risk_events"][:5]))
    if s["recent_journal"]:
        lines.append("最近研究日志：" + "；".join(
            "%s:%s" % (k, ti) for k, ti, _ in s["recent_journal"][:5]))
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="")
    ap.add_argument("--journal", action="store_true")
    a = ap.parse_args()
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    db = a.db or os.path.join(root, "data", "market_trade.db")
    if not os.path.exists(db):
        print(json.dumps({"error": "db not found", "db": db}, ensure_ascii=False))
        return
    s = build(db)
    txt = render(s)
    print(txt)
    if a.journal:
        j = Journal(db)
        j.start()
        j.add("note", "态势快照", txt, json.dumps(s, ensure_ascii=False, default=str))
        j.stop()


if __name__ == "__main__":
    main()
