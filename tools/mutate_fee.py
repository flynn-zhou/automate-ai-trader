# -*- coding: utf-8 -*-
"""变异测试：把假交易所改回"手续费记在 USDT"的旧行为，
新的往返测试必须挂。否则说明我的测试抓不到真问题。
"""
import io, subprocess, sys, os

P = "tools/fake_binance.py"
orig = io.open(P, encoding="utf-8").read()

# 变异：改回买入手手续费记 USDT、余额不减 BTC
MUT = orig.replace(
    '''            self.usdt -= cost
            self.btc += qty - (fee / px)''',
    '''            self.usdt -= cost + fee
            self.btc += qty''', 1)
assert MUT != orig, "变异锚点未找到"

io.open(P, "w", encoding="utf-8").write(MUT)
try:
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_spot_round_trip.py",
         "-q", "--tb=no", "-p", "no:cacheprovider"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = (r.stdout or "") + (r.stderr or "")
    print("退出码:", r.returncode)
    tail = [l for l in out.splitlines() if "passed" in l or "failed" in l]
    print("结果:", tail[-1] if tail else out[-400:])
    if r.returncode != 0:
        print("=> 变异被抓住（测试有效）")
    else:
        print("=> !!! 变异没被抓住，测试是假的")
finally:
    io.open(P, "w", encoding="utf-8").write(orig)
    print("已还原 fake_binance.py")
