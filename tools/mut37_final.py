# -*- coding: utf-8 -*-
"""变异验证（重做版）：必须看到"因为哪一条断言失败"。

上一版我用 subprocess 捕获输出，结果 stdout 是空的，
只看到 aiohttp 的 "Unclosed client session" 警告和退出码 1。
那时候我差点得出"变异被抓住了"的结论——但那个退出码可能来自清理失败，
而不是断言失败。两个都返回 1，含义完全不同。

所以这次直接改进程内跑，把 "E  " 开头的断言行打出来。
"""
import io, re, subprocess, sys

CASES = [
    ("撤掉粉尘阈值", "ai_trader/exchange/spot.py",
     "        if step > 0 and 0.0 < abs(qty) < step:", "        if False:"),
    ("假交易所不收手续费", "tools/fake_binance.py",
     "        fee = cost * 0.001", "        fee = 0.0"),
    ("假交易所不校验最小名义", "tools/fake_binance.py",
     "        if min_notional > 0 and cost < min_notional - 1e-9:", "        if False:"),
]

for name, path, old, new in CASES:
    orig = io.open(path, encoding="utf-8").read()
    mut = orig.replace(old, new, 1)
    assert mut != orig, "%s: anchor not found" % name
    io.open(path, "w", encoding="utf-8").write(mut)
    try:
        r = subprocess.run([sys.executable, "-m", "pytest",
                            "tests/test_live_full_rehearsal.py",
                            "-q", "-p", "no:warnings", "--tb=short"],
                           capture_output=True, text=True, timeout=300)
        out = (r.stdout or "") + (r.stderr or "")
        elines = [l.strip()[2:].strip() for l in out.splitlines()
                  if l.strip().startswith("E ")]
        print("=" * 66)
        print("变异：%s" % name)
        print("  退出码: %d" % r.returncode)
        ok = r.returncode != 0 and bool(elines)
        if elines:
            for l in elines[:3]:
                print("  断言: %s" % l[:120])
        else:
            print("  !!! 没有任何断言失败——退出码不是来自断言")
        print("  => %s" % ("正确：因为该失败的原因失败" if ok
                           else "!!! 无效：没看到断言失败"))
    finally:
        io.open(path, "w", encoding="utf-8").write(orig)
        print("  已还原")
