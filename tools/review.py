# -*- coding: utf-8 -*-
"""样本复盘：把截止目前的成交拆成胜率/赔率/成本/期望，并给出该不该调参的判定。

用法：
    python tools/review.py                 # 打印 + 落一条 result 日志
    python tools/review.py --no-journal
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ai_trader.journal import Journal  # noqa: E402

MIN_SAMPLE = 50


def rows(c, sql, a=()):
    try:
        return c.execute(sql, a).fetchall()
    except Exception:
        return []


def analyse(db: str) -> dict:
    c = sqlite3.connect(db)
    # 注意：列名是 fee 不是 fee_usdt。这里曾经写错列名被 rows() 静默吞掉，
    # 导致"明明有平仓样本却报还没有样本"——先修这里再谈复盘。
    closed = c.execute("""SELECT side, qty, entry_price, exit_price, pnl_gross, pnl_net,
                                 fee, close_reason, entry_ts, exit_ts
                          FROM trades WHERE exit_ts IS NOT NULL ORDER BY id""").fetchall()
    out = {"n": len(closed), "closed": closed}
    if not closed:
        out["verdict"] = "样本不足，继续等"
        out["ready"] = False
        return out

    n = len(closed)
    wins = [r for r in closed if (r[5] or 0) > 0]
    losses = [r for r in closed if (r[5] or 0) <= 0]
    gross = sum((r[4] or 0) for r in closed)
    net = sum((r[5] or 0) for r in closed)
    fees = sum((r[6] or 0) for r in closed)
    avg_win = (sum((r[5] or 0) for r in wins) / len(wins)) if wins else 0.0
    avg_loss = (abs(sum((r[5] or 0) for r in losses)) / len(losses)) if losses else 0.0
    by_reason = {}
    for r in closed:
        k = r[7] or "?"
        d = by_reason.setdefault(k, {"n": 0, "net": 0.0})
        d["n"] += 1
        d["net"] += (r[5] or 0)

    out.update({
        "win_rate": round(len(wins) / n, 4),
        "odds": round(avg_win / avg_loss, 3) if avg_loss else None,
        "net": round(net, 4),
        "gross": round(gross, 4),
        "fees": round(fees, 4),
        "fee_share": round(fees / abs(gross), 3) if gross else None,
        "avg_win": round(avg_win, 4),
        "avg_loss": round(avg_loss, 4),
        "by_close_reason": by_reason,
        "expectancy": round(net / n, 4),
        "ready": n >= MIN_SAMPLE,
    })

    # 盈亏平衡所需胜率：w*avg_win = (1-w)*avg_loss
    if avg_win + avg_loss > 0:
        out["breakeven_win_rate"] = round(avg_loss / (avg_win + avg_loss), 4)
        out["edge"] = round(out["win_rate"] - out["breakeven_win_rate"], 4)

    if not out["ready"]:
        out["verdict"] = "样本 %d 笔 < %d，禁止据此调参（先用噪音解释噪音）" % (n, MIN_SAMPLE)
    elif out.get("edge", 0) > 0 and net > 0:
        out["verdict"] = "有正边际，保持参数不动，继续攒样本"
    else:
        out["verdict"] = "无边际或为负，需要复盘：先看 close_reason 分布，再决定改信号还是改止损"
    c.close()
    return out


def render(a: dict) -> str:
    if not a["n"]:
        return "还没有平仓样本。"
    lines = [
        "样本 %d 笔｜胜率 %.1f%%｜赔率 %s｜期望 %.4fU/笔" % (
            a["n"], a["win_rate"] * 100,
            ("%.2f" % a["odds"]) if a["odds"] else "—", a["expectancy"]),
        "毛盈亏 %.3fU｜手续费 %.3fU（占毛 %.0f%%）｜净盈亏 %.3fU" % (
            a["gross"], a["fees"], (a["fee_share"] or 0) * 100, a["net"]),
        "平均赢 %.3fU / 平均输 %.3fU｜盈亏平衡胜率 %s%%｜边际 %s" % (
            a["avg_win"], a["avg_loss"],
            ("%.1f" % (a["breakeven_win_rate"] * 100)) if a.get("breakeven_win_rate") else "—",
            ("%+.1f%%" % (a["edge"] * 100)) if a.get("edge") is not None else "—"),
        "平仓原因：" + "，".join("%s %d笔 %.2fU" % (k, v["n"], v["net"])
                                for k, v in sorted(a["by_close_reason"].items())),
        "判定：" + a["verdict"],
    ]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="")
    ap.add_argument("--no-journal", action="store_true")
    a = ap.parse_args()
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    db = a.db or os.path.join(root, "data", "market_trade.db")
    if not os.path.exists(db):
        print("db not found:", db)
        return
    res = analyse(db)
    txt = render(res)
    print(txt)
    if not a.no_journal and res["n"]:
        j = Journal(db)
        j.start()
        j.add("result", "样本复盘（%d 笔）" % res["n"], txt,
              json.dumps({k: v for k, v in res.items() if k != "closed"},
                         ensure_ascii=False, default=str))
        j.stop()


if __name__ == "__main__":
    main()
