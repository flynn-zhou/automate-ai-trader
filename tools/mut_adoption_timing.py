# -*- coding: utf-8 -*-
"""变异验证：接管即挂保护单的三条守卫必须真的能抓住回归。

覆盖分工（每条变异只打**真正守它的那条测试**）：
  ① `_reconcile_positions` 里不挂  → 端到端（真交易所替身看得到单）
  ② 具名入口方法体变空操作        → 行为测试（走假的 live 执行器）
  ③ 维护被挪到 15m 守卫之后      → 源码顺序判据

纪律：rc=2/4（收集/语法错误）与「非零但没有断言失败行」都判无效，
因为它们看起来像"抓住了"，其实测试根本没跑。
"""
import io
import subprocess
import sys

P = "ai_trader/trading/engine.py"
orig = io.open(P, encoding="utf-8").read()


def _run(name, mutated, target):
    """跑一次变异，返回是否有效抓住。"""
    assert mutated != orig, "%s：变异没改动文件" % name
    io.open(P, "w", encoding="utf-8").write(mutated)
    try:
        r = subprocess.run([sys.executable, "-m", "pytest", target, "-q",
                            "-p", "no:warnings", "--tb=short"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace")
        out = (r.stdout or "") + (r.stderr or "")
        lines = [l.strip() for l in out.splitlines()
                 if l.strip().startswith("E   ") or "AssertionError" in l]
        print("=" * 70)
        print("变异：%s" % name)
        print("  守它的测试：%s" % target)
        print("  rc=%d" % r.returncode)
        if r.returncode in (2, 3, 4) or "SyntaxError" in out:
            print("  => !! 无效：rc=%d 是收集/语法错误，测试没跑" % r.returncode)
            return False
        if r.returncode == 0:
            print("  => !! 存活：回归不会被发现")
            return False
        if not lines:
            print("  => !! 无效：非零退出但看不到断言失败行")
            return False
        print("  断言：%s" % lines[0][:120])
        print("  => 正确：被断言抓住")
        return True
    finally:
        io.open(P, "w", encoding="utf-8").write(orig)
        print("  已还原")


results = []

# ① 对账里不挂保护单
old1 = "                if self.adopted_pos:\n                    _px = float(getattr(self.feed, \"last_price\", 0.0) or 0.0)\n                    await self._maintain_adopted_protection(_px)"
new1 = "                if False:\n                    _px = float(getattr(self.feed, \"last_price\", 0.0) or 0.0)\n                    await self._maintain_adopted_protection(_px)"
if old1 in orig:
    results.append(_run("① 对账里不挂保护单", orig.replace(old1, new1, 1),
                        "tests/test_adoption_protection_e2e.py"))
else:
    print("!! ① 锚点没找到")
    results.append(False)

# ② 具名入口的方法体变成空操作（保留签名与 early return）
old2 = ("        pos, _slot = self._adopted_slot()\n"
        "        if not pos:\n"
        "            return\n"
        "        try:\n"
        "            await self._sync_adopted_stop_order(pos, price)")
new2 = ("        pos, _slot = self._adopted_slot()\n"
        "        if not pos:\n"
        "            return\n"
        "        try:\n"
        "            pass")
if old2 in orig:
    results.append(_run(
        "② 维护入口变成空操作", orig.replace(old2, new2, 1),
        "tests/test_adopted_stop_order.py::"
        "test_maintain_adopted_protection_places_the_order"))
else:
    print("!! ② 锚点没找到")
    results.append(False)

# ③ 把维护调用挪到出场判断之后（仍在同一个 if 块内 → 语法合法）
old3 = ("            await self._maintain_adopted_protection(price)\n"
        "            try:\n"
        "                _b15 = self._to_pending_bars((bars_by_iv or {}).get(900000))\n"
        "                await self._step_adopted_exit(bars_by_iv, _b15, price, now_ms,\n"
        "                                             log_quiet=True)\n"
        "            except Exception as e:\n"
        "                logger.error(\"[接管] 人工仓出场判断失败：%s\", e, exc_info=True)\n"
        "                try:\n"
        "                    self.db.log_risk(\"adopted_exit_error\", str(e)[:200])\n"
        "                except Exception:\n"
        "                    pass\n")
new3 = ("            try:\n"
        "                _b15 = self._to_pending_bars((bars_by_iv or {}).get(900000))\n"
        "                await self._step_adopted_exit(bars_by_iv, _b15, price, now_ms,\n"
        "                                             log_quiet=True)\n"
        "            except Exception as e:\n"
        "                logger.error(\"[接管] 人工仓出场判断失败：%s\", e, exc_info=True)\n"
        "                try:\n"
        "                    self.db.log_risk(\"adopted_exit_error\", str(e)[:200])\n"
        "                except Exception:\n"
        "                    pass\n"
        "            await self._maintain_adopted_protection(price)\n")
if old3 in orig:
    results.append(_run(
        "③ 维护被挪到出场判断之后", orig.replace(old3, new3, 1),
        "tests/test_adopted_stop_order.py::"
        "test_step_maintains_the_protective_order_before_the_15m_guard"))
else:
    print("!! ③ 锚点没找到")
    results.append(False)

print()
print("=" * 70)
print("结果：%d/%d 有效抓住" % (sum(1 for r in results if r), len(results)))
sys.exit(0 if all(results) else 1)
