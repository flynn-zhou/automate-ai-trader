# -*- coding: utf-8 -*-
"""VOL-1 变异验证：把新口径逐个改回旧行为，确认测试会红。

★ 每轮必须清 `__pycache__`：本仓库踩过「源码写 A、字节码跑 B」，
  缓存陈旧时"变异存活"的结论是假的。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGET = os.path.join(ROOT, "ai_trader", "trading", "pending_plan.py")
SRC = os.path.join(ROOT, "ai_trader", "trading", "plan_source.py")
TESTS = ["tests/test_vol_gate_rule.py", "tests/test_kind_wiring.py"]

#: (名字, 文件, 原文, 改成)
MUTANTS = [
    ("基准用中位数而不是均值",
     TARGET, "            ref = _mean(ref_slice)", "            ref = _median(ref_slice)"),
    ("只看突破根本身（忽略附近）",
     TARGET,
     "            peak = max(all_vols[near_lo:near_hi]) if near_hi > near_lo else 0.0",
     "            peak = all_vols[i]"),
    ("附近半径不生效（写死 0）",
     TARGET,
     "            near_lo = max(0, i - int(vol_near))\n"
     "            near_hi = min(len(all_vols), i + int(vol_near) + 1)",
     "            near_lo = i\n            near_hi = i + 1"),
    ("门槛退回 1.5",
     TARGET, "WINDOW_VOL_MULT = 3.0", "WINDOW_VOL_MULT = 1.5"),
    ("附近半径退回 1",
     TARGET, "WINDOW_VOL_NEAR = 2", "WINDOW_VOL_NEAR = 1"),
    ("反转不再能开窗口",
     TARGET,
     "WINDOW_START_TAGS = (TAG_BREAKOUT, TAG_REVERSAL)",
     "WINDOW_START_TAGS = (TAG_BREAKOUT,)"),
    ("窗口起点只认反转（丢掉突破）",
     TARGET,
     "WINDOW_START_TAGS = (TAG_BREAKOUT, TAG_REVERSAL)",
     "WINDOW_START_TAGS = (TAG_REVERSAL,)"),
    ("三引线被放回生产启用集合",
     TARGET,
     "ENABLED_KINDS = (KIND_INVERSE_V, KIND_EMA_REJECT)",
     "ENABLED_KINDS = KINDS"),
    ("计划源不再传 kinds（回退到默认全开）",
     SRC,
     "        return P.detect_all(ctx, params, kinds=P.ENABLED_KINDS,\n"
     "                            require_window=True, include_mirror=True)",
     "        return P.detect_all(ctx, params, require_window=True,\n"
     "                            include_mirror=True)"),
    ("参考窗口退回 12 小时",
     TARGET, "WINDOW_VOL_WIN = 24", "WINDOW_VOL_WIN = 12"),
]


def clear_pyc():
    for base in (os.path.join(ROOT, "ai_trader"), os.path.join(ROOT, "tests")):
        for dirpath, dirnames, _ in os.walk(base):
            for d in list(dirnames):
                if d == "__pycache__":
                    shutil.rmtree(os.path.join(dirpath, d), ignore_errors=True)
                    dirnames.remove(d)


def run_tests():
    clear_pyc()
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    p = subprocess.run([sys.executable, "-m", "pytest"] + TESTS + ["-q",
                        "--no-header", "-p", "no:cacheprovider"],
                       cwd=ROOT, capture_output=True, text=True, env=env, encoding="utf-8", errors="replace")
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def main():
    print("基线（未变异）…", flush=True)
    rc, out = run_tests()
    if rc != 0:
        print("★ 基线就红了，先修基线：\n" + out[-3000:])
        return 1
    print("  基线绿\n", flush=True)

    killed = 0
    for name, path, old, new in MUTANTS:
        with open(path, "r", encoding="utf-8") as f:
            orig = f.read()
        assert orig.count(old) == 1, (
            "变异点 %r 在 %s 里出现 %d 次，无法唯一定位"
            % (name, os.path.basename(path), orig.count(old)))
        try:
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(orig.replace(old, new))
            rc, out = run_tests()
            bad = ("SyntaxError" in out or "IndentationError" in out
                   or "ERROR collecting" in out or "error" in out.lower()
                   and "ImportError" in out)
            if rc != 0 and not bad:
                killed += 1
                print("  ✅ 抓住：%s" % name, flush=True)
            elif rc != 0 and bad:
                print("  ⚠️  变异体本身坏了（不算抓住）：%s" % name, flush=True)
            else:
                print("  ❌ 存活：%s" % name, flush=True)
        finally:
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(orig)
            clear_pyc()

    print("\n%d/%d 个变异体被抓住，%d 个存活"
          % (killed, len(MUTANTS), len(MUTANTS) - killed))
    return 0 if killed == len(MUTANTS) else 2


if __name__ == "__main__":
    sys.exit(main())
