"""命令行写研究日志：python tools/journal_add.py --kind pivot --title "..." --body "..."

看板「研究日志」页实时可见，是 AI 与人共用的推理留痕。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.journal import Journal  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", default="note",
                    choices=["hypothesis", "experiment", "result", "pivot", "verify",
                             "note", "alert"])
    ap.add_argument("--title", required=True)
    ap.add_argument("--body", required=True)
    ap.add_argument("--data", default="")
    ap.add_argument("--db", default="")
    ap.add_argument("--author", default="ai")
    a = ap.parse_args()
    db = a.db or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "data", "market_trade.db")
    j = Journal(db)
    j.start()
    jid = j.add(a.kind, a.title, a.body, a.data or None, a.author)
    j.stop()
    print(json.dumps({"ok": True, "id": jid, "db": db}, ensure_ascii=False))


if __name__ == "__main__":
    main()
