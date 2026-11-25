# -*- coding: utf-8 -*-
"""变异验证：本轮四个修复的测试真的能抓住 bug 吗？

纪律（沿用 mut37_final 的教训）：
  · 必须看到**断言失败行**（"E   " 开头），退出码非 0 本身不算证据；
  · rc=2（collection error）是**无效**结果，不是抓住 —— 那只说明测试没跑起来；
  · 只改一处、改完立刻还原，任何异常都走 finally。

四个修复：
  A 归属反查（recent_fills → allOrders）
  B 三态（None ≠ ""）
  C 空仓平仓要看接管仓
  D armed_at_sub_ts 落库
"""
import io
import subprocess
import sys

CASES = [
    # ---------------- A：不补归属 ----------------
    ("A1 不调归属补全", "ai_trader/exchange/live.py",
     "        await self._attach_client_ids(out)",
     "        pass", 
     "tests/test_fill_attribution.py"),

    ("A2 归属映射按错键取", "ai_trader/exchange/live.py",
     "            cid = by_id.get(str(f.get(\"order_id\")))",
     "            cid = by_id.get(str(f.get(\"ts\")))",
     "tests/test_fill_attribution.py"),

    # ---------------- B：把「不知道」当「手单」 ----------------
    ("B1 None 退化成 manual", "ai_trader/trading/engine.py",
     "        if cid is None:\n            saw_unknown = True",
     "        if False:\n            saw_unknown = True",
     "tests/test_fill_attribution.py"),

    ("B2 unknown 不落保守分支", "ai_trader/trading/engine.py",
     "    if saw_unknown:\n        return PROV_UNKNOWN, used",
     "    if False:\n        return PROV_UNKNOWN, used",
     "tests/test_fill_attribution.py"),

    # ---------------- C：只看系统仓 ----------------
    ("C1 不看接管仓", "ai_trader/trading/engine.py",
     "            if _adopted_qty > 0:",
     "            if False:",
     "tests/test_pending_chain.py"),

    # ---------------- D：锚点不落库 ----------------
    ("D1 不写 armed_at_sub_ts", "ai_trader/trading/pending_thesis_store.py",
     "                 row.get(\"armed_at_bar\"), row.get(\"armed_at_sub_ts\"),",
     "                 row.get(\"armed_at_bar\"), None,",
     "tests/test_pending_anchor_persist.py"),

    ("D2 progress 不交回锚点", "ai_trader/trading/pending_thesis_store.py",
     "            \"armed_at_sub_ts\": row.get(\"armed_at_sub_ts\"),",
     "            \"armed_at_sub_ts\": None,",
     "tests/test_pending_anchor_persist.py"),

    ("D3 迁移不补列", "ai_trader/trading/pending_thesis_store.py",
     "        if \"armed_at_sub_ts\" not in have:",
     "        if False:",
     "tests/test_pending_anchor_persist.py"),

    # ---------------- E：市价单不带 client_id ----------------
    ("E1 市价单不下发 id", "ai_trader/exchange/live.py",
     "        if client_id:\n            params[\"newClientOrderId\"] = client_id",
     "        if False:\n            params[\"newClientOrderId\"] = client_id",
     "tests/test_fill_attribution.py"),

    ("E2 开仓 id 仍在下单后生成", "ai_trader/trading/engine.py",
     "        self._seq += 1\n        client_id = \"AI-%d-%d\" % (now_ms, self._seq)\n"
     "        fill = await self.ex.market_order(\n"
     "            order_side, plan.qty,\n"
     "            reason=\"%s 开仓 %s\" % (sig[\"name\"], side_txt),\n"
     "            client_id=client_id)",
     "        fill = await self.ex.market_order(\n"
     "            order_side, plan.qty,\n"
     "            reason=\"%s 开仓 %s\" % (sig[\"name\"], side_txt))\n"
     "        self._seq += 1\n        client_id = \"AI-%d-%d\" % (now_ms, self._seq)",
     "tests/test_open_fill_qty.py"),
]


def run_one(name, path, old, new, tests):
    orig = io.open(path, encoding="utf-8").read()
    if old not in orig:
        print("  !! 锚点没找到（锚点写错了，这条无效）：%s" % name)
        return False
    mut = orig.replace(old, new, 1)
    assert mut != orig
    io.open(path, "w", encoding="utf-8").write(mut)
    try:
        r = subprocess.run(
            [sys.executable, "-m", "pytest", tests, "-q",
             "-p", "no:warnings", "--tb=short"],
            capture_output=True, text=True, timeout=600,
            encoding="utf-8", errors="replace")
        out = (r.stdout or "") + (r.stderr or "")
        # `--tb=short` 的断言行长这样：`E   AssertionError: ...`；
        # `--tb=line` 则是 `path:行: AssertionError: ...`，两种都算证据。
        elines = [l.strip() for l in out.splitlines()
                  if l.strip().startswith("E ") or "AssertionError" in l]
        print("=" * 70)
        print("变异：%s" % name)
        print("  目标测试：%s" % tests)
        print("  退出码：%d" % r.returncode)
        if r.returncode == 2:
            print("  => !! 无效：rc=2 是收集/导入错误，测试根本没跑")
            return False
        if r.returncode == 0:
            print("  => !! 存活：测试全绿，说明这条 bug 没被测到")
            return False
        if not elines:
            print("  => !! 无效：非零退出但没有断言失败行（可能是清理失败）")
            return False
        for l in elines[:2]:
            print("  断言：%s" % l[:130])
        print("  => 正确：被断言抓住")
        return True
    finally:
        io.open(path, "w", encoding="utf-8").write(orig)


def main():
    caught = 0
    for case in CASES:
        if run_one(*case):
            caught += 1
    print()
    print("=" * 70)
    print("结果：%d/%d 抓住" % (caught, len(CASES)))
    return 0 if caught == len(CASES) else 1


sys.exit(main())
