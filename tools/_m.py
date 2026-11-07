# -*- coding: utf-8 -*-
"""合并后重跑变异：确认两处修复仍被钉住，且反证仍成立。"""
import io, os, subprocess, sys, tempfile
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
E = "ai_trader/trading/engine.py"
orig = io.open(E, encoding="utf-8").read()
GATE = """        if (self.trend.last_eval_bar
                and self.trend.last_eval_bar <= self._trend_block_bar):
            self._log_decision(bars_by_iv, price, equity, now_ms, "trend_wait", "hold",
                               "本根 4h 内已熔断出局，等下一根再评估（启动对账也受限）",
                               gates=[], risk_info=None)
            return
"""
CLEAR = """        if self.pos and getattr(self.trend, "need_reconcile", False):
            self.trend.mark_reconciled()
"""
NODE = "tests/test_startup_reconcile.py::"

def run(text, node):
    io.open(E, "w", encoding="utf-8").write(text)
    try:
        t = tempfile.mktemp(suffix=".txt")
        with io.open(t, "w", encoding="utf-8") as fh:
            subprocess.run([sys.executable, "-m", "pytest", NODE + node, "-q",
                            "-p", "no:warnings", "--tb=line"],
                           stdout=fh, stderr=subprocess.STDOUT)
        out = io.open(t, encoding="utf-8").read(); os.unlink(t)
        return ("AssertionError" in out), out
    finally:
        io.open(E, "w", encoding="utf-8").write(orig)

checks = []
checks.append(("撤 CLEAR", run(orig.replace(CLEAR, "", 1),
                 "test_takeover_clears_reconcile_flag")[0]))
checks.append(("两处都撤", run(orig.replace(GATE, "", 1).replace(CLEAR, "", 1),
                 "test_no_reentry_after_stop_in_same_bar")[0]))
checks.append(("两处都撤-flag", run(orig.replace(GATE, "", 1).replace(CLEAR, "", 1),
                 "test_takeover_clears_reconcile_flag")[0]))
# 反证：原有三条语义测试在变异下不应受影响
checks.append(("两处都撤-旧语义", not run(orig.replace(GATE, "", 1).replace(CLEAR, "", 1),
                 "test_above_sma_keeps_reconcile_until_entry_happens")[0]))

for name, ok in checks:
    print("[%s] %s" % ("OK" if ok else "PROBLEM", name))
io.open(E, "w", encoding="utf-8").write(orig)
print()
print("已还原")
