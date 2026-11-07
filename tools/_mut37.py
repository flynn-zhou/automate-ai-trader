# -*- coding: utf-8 -*-
"""变异：撤掉粉尘阈值，排练必须失败。

这条排练的价值全在这里：如果它抓不住上轮修的粉尘缺陷，
它就只是又一盏绿灯——而绿灯比红灯更危险，因为没人会去查它。
"""
import io, subprocess, sys

P = "ai_trader/exchange/spot.py"
orig = io.open(P, encoding="utf-8").read()

OLD = "        if step > 0 and 0.0 < abs(qty) < step:"
MUT = orig.replace(OLD, "        if False:", 1)
assert MUT != orig, "anchor not found"

io.open(P, "w", encoding="utf-8").write(MUT)
try:
    r = subprocess.run([sys.executable, "-m", "pytest",
                        "tests/test_live_full_rehearsal.py", "-q", "--tb=line"],
                       capture_output=True, text=True, timeout=300)
    out = (r.stdout or "") + (r.stderr or "")
    keep = [l for l in out.splitlines()
            if l.strip() and "Warning" not in l and "warnings.warn" not in l
            and "asyncio" not in l.lower()][-4:]
    print("变异：撤掉粉尘阈值")
    for l in keep:
        print("  " + l)
    print("  退出码:", r.returncode)
    print("  => " + ("排练正确失败：它真的能抓住粉尘缺陷" if r.returncode != 0
                     else "!!! 排练没失败——它抓住粉尘缺陷只是我的想象"))
finally:
    io.open(P, "w", encoding="utf-8").write(orig)
    print("已还原")


# --- 第二个变异：让假交易所退回到"不收手续费"（旧 e2e 的世界）---
P2 = "tools/fake_binance.py"
orig2 = io.open(P2, encoding="utf-8").read()
OLD2 = "        fee = cost * 0.001"
MUT2 = orig2.replace(OLD2, "        fee = 0.0", 1)
assert MUT2 != orig2, "fee anchor not found"
io.open(P2, "w", encoding="utf-8").write(MUT2)
try:
    r = subprocess.run([sys.executable, "-m", "pytest",
                        "tests/test_live_full_rehearsal.py", "-q", "--tb=line"],
                       capture_output=True, text=True, timeout=300)
    out = (r.stdout or "") + (r.stderr or "")
    keep = [l for l in out.splitlines()
            if l.strip() and "Warning" not in l and "warnings.warn" not in l
            and "asyncio" not in l.lower()][-4:]
    print()
    print("变异：假交易所不收手续费（退回旧 e2e 那个不存在的世界）")
    for l in keep:
        print("  " + l)
    print("  退出码:", r.returncode)
    print("  => " + ("排练正确失败：假交易所的严格性是被测试锁住的"
                     if r.returncode != 0 else "!!! 没失败——手续费模型没被验证"))
finally:
    io.open(P2, "w", encoding="utf-8").write(orig2)
    print("已还原")
