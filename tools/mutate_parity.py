# -*- coding: utf-8 -*-
"""失败测试：把引擎的策略参数改掉，一致性检查器必须报不一致。

没做过失败测试的检查器，等于没有检查器。
"""
import io, subprocess, sys

P = "tools/replay_engine.py"
orig = io.open(P, encoding="utf-8").read()

# 变异：把回调的 SMA 周期从 200 改成 150（引擎与回测就此分叉）
MUT = orig.replace("cfg.trading.sma_lookback = NL", "cfg.trading.sma_lookback = 150", 1)
assert MUT != orig, "变异锚点未找到"

io.open(P, "w", encoding="utf-8").write(MUT)
try:
    r = subprocess.run([sys.executable, "tools/parity_auto.py"],
                       capture_output=True, text=True, timeout=900,
                       env={"PYTHONPATH": ".", "BT_DB": "data/spot.db", "PATH": "/usr/bin:/bin"})
    out = (r.stdout or "") + (r.stderr or "")
    tail = [l for l in out.splitlines() if l.strip()][-6:]
    print("退出码:", r.returncode)
    for l in tail:
        print("  " + l)
    if r.returncode != 0:
        print()
        print("=> 变异被抓住（检查器有效）")
    else:
        print()
        print("=> !!! 变异没被抓住，检查器是假的")
finally:
    io.open(P, "w", encoding="utf-8").write(orig)
    print("已还原")
