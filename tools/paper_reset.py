# -*- coding: utf-8 -*-
"""把论文纸面账开一个新纪元：余额对齐 100 U，与用户真实账户一致。

为什么要这么做（2026-11-02 实测）：
  线上纸面账对不上：
    4 笔已平仓的 pnl_net 合计 -20.4628 U
    而权益从 100.0 变成 71.77029 U，实际变化 -28.2297 U
    缺口 7.7669 U 说不清去向。

  查清了成因：
  1. 第 4 笔的平仓原因是「策略切换：剥头皮仓位强制平仓」，
     那是 tools/cutover_trend.py 这个一次性脚本干的——它直接改 trades 行、
     直接改 executor_state，【不调用 market_order】，所以 fills/orders 里
     没有第 8 条成交记录。账目无法从成交复原。
  2. 而且同一份数据里，cutover【之前】的余额就已经偏了约 5.36 U
     （按 3 笔已平仓 net 反推应为 81.49，实测 81.08+2.41 的推算对不上），
     说明漂移来自更早的代码版本，已经无法可信复原。

  用【当前代码】跑了三个场景（先涨后跌/先跌后涨/基本不动），
  余额变化与归因盈亏之差精确为 0.000000000 —— 现在这条不变量是成立的。
  所以问题不在现行记账，而在历史遗留。

  既然要「用纸面样本找到真实边际」，样本的起点必须是干净的、
  而且要和用户真实的 100 U 账户一致，否则算出来的收益率没有意义。
  因此开新纪元：余额重置为 100，旧 trades 全部保留（不删历史），
  重置这件事本身写进研究日志，任何人打开看板都能看到为什么。

用法：
    cd /home/cryptobot/ai-trader && .venv/bin/python tools/paper_reset.py --dry
    cd /home/cryptobot/ai-trader && .venv/bin/python tools/paper_reset.py
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

TARGET = 100.0


def snapshot(c):
    st = c.execute("SELECT ts, balance, pos_qty, pos_entry FROM executor_state"
                   " ORDER BY ts DESC LIMIT 1").fetchone()
    n, pnl = c.execute("SELECT COUNT(*), COALESCE(SUM(pnl_net),0) FROM trades"
                       " WHERE exit_ts IS NOT NULL").fetchone()
    eq = c.execute("SELECT equity FROM equity ORDER BY ts DESC LIMIT 1").fetchone()
    fills = c.execute("SELECT COUNT(*) FROM fills").fetchone()[0]
    return {"executor_state": st, "closed_trades": n, "sum_pnl_net": pnl,
            "last_equity": eq[0] if eq else None, "fills": fills}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(ROOT, "data", "market_trade.db"))
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()

    c = sqlite3.connect(a.db)
    before = snapshot(c)
    print("=== 重置前 ===")
    for k, v in before.items():
        print("  %-16s %s" % (k, v))
    print()

    start_eq = before["last_equity"] if before["last_equity"] is not None else TARGET
    gap = (TARGET - start_eq) - (-before["sum_pnl_net"])
    print("  旧纪元: 起点 100.00 -> 终点 %.5f，归因合计 %+.4f，缺口 %+.4f U"
          % (start_eq, before["sum_pnl_net"], gap))
    print()

    if a.dry:
        print("（dry run，未写入）")
        c.close()
        return 0

    # 先备份，重置动作必须可回退
    bak = a.db + ".pre_reset_%d" % int(time.time())
    shutil.copy2(a.db, bak)
    print("已备份到 %s" % os.path.basename(bak))

    now_ms = int(time.time() * 1000)
    c.execute("INSERT OR REPLACE INTO executor_state(id,ts,balance,pos_qty,pos_entry)"
              " VALUES(1,?,?,0,0)", (now_ms, TARGET))
    c.commit()

    after = snapshot(c)
    print()
    print("=== 重置后 ===")
    for k, v in after.items():
        print("  %-16s %s" % (k, v))
    c.close()

    # 写进研究日志：这是制度性的记录，不是随手一条 note
    from ai_trader.journal import Journal
    body = "\n".join([
        "旧纪元的账对不上，已经在看板上挂了一天多，必须处理掉。",
        "",
        "实测数字：",
        "  4 笔已平仓 pnl_net 合计 %+.4f U" % before["sum_pnl_net"],
        "  权益实际变化 100.00 -> %.5f U" % start_eq,
        "  缺口 %+.4f U（说不清去向）" % gap,
        "  fills 只有 %d 条（4 笔 x 建平各一条 = 应为 8）" % before["fills"],
        "",
        "成因（已查清）：",
        "  1. 第 4 笔平仓原因是「策略切换：剥头皮仓位强制平仓」，",
        "     那是 tools/cutover_trend.py 做的——它直接改 trades 行和 executor_state，",
        "     不调用 market_order，所以 fills/orders 里没有那笔成交，账无法从成交复原。",
        "  2. cutover 之前余额就已经偏了约 5.36 U，漂移来自更早的代码版本，无法可信复原。",
        "",
        "现行记账是好的：用当前代码跑三个场景（先涨后跌/先跌后涨/基本不动），",
        "余额变化与归因盈亏之差精确为 0.000000000。",
        "",
        "处置：开新纪元，余额重置为 %.0f U（与用户真实账户一致），" % TARGET,
        "旧 trades 全部保留、一条没删。新纪元从此刻起可以正常对账。",
    ])
    try:
        j = Journal(a.db)
        j.start()
        j.add("note", "纸面账开新纪元：余额重置为 100U（旧纪元缺口 %.2fU 已查清成因）"
              % abs(gap), body,
              json.dumps({"old_epoch_end": start_eq,
                          "sum_pnl_net": before["sum_pnl_net"],
                          "gap": gap, "new_balance": TARGET,
                          "backup": os.path.basename(bak)}, ensure_ascii=False))
        j.stop()
        print("已写入研究日志")
    except Exception as e:
        print("写研究日志失败（不影响余额重置）:", e)

    ok = abs(after["executor_state"][1] - TARGET) < 1e-9 and after["executor_state"][2] == 0
    print()
    print("结论:", "重置成功，余额 %.2f 持仓 0" % TARGET if ok else "重置失败")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
