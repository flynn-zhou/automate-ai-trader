# -*- coding: utf-8 -*-
"""变异验证：EXIT-1（收盘跌破 / 有效跌破）的每条判据都必须被测试抓住。

判「抓住」的标准（和本项目历史口径一致）：
  * 变异后测试**失败**（rc=1）才算抓住；
  * rc=2/3/4、SyntaxError、collection error 都不算 —— 那是测试自己崩了；
  * 变异必须落在**这一行的源语义**上，不能靠打空指针制造 AttributeError。

每条变异都对应一条真实约束，不是为凑数编的。
"""
from __future__ import annotations

import io
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BR = os.path.join(ROOT, "ai_trader", "trading", "break_rules.py")
TH = os.path.join(ROOT, "ai_trader", "trading", "thesis.py")

#: (文件, 原串, 变异串, 说明, 期望的测试集)
MUTANTS = [
    (BR, "CONFIRM_BARS = 3", "CONFIRM_BARS = 99",
     "有效跌破不确认了（永远等回抽）", "tests/test_break_rules.py"),
    (BR, "CONFIRM_BARS = 3", "CONFIRM_BARS = 1",
     "有效跌破过度确认（第 1 根就确认，等于不等回抽）", "tests/test_break_rules.py"),
    (BR, "state = CONFIRMED if since >= CONFIRM_BARS else CLOSED_BELOW",
     "state = CLOSED_BELOW",
     "永远不进 CONFIRMED（确认后还在等回抽）", "tests/test_break_rules.py"),
    (BR, "NOISE_ATR = 0.05", "NOISE_ATR = 0.0",
     "去掉触发缓冲（任何零点几点越过都算跌破）", "tests/test_break_rules.py tests/test_pending_plan.py"),
    (BR, "NOISE_ATR = 0.05", "NOISE_ATR = 5.0",
     "触发缓冲大到永远触发不了", "tests/test_break_rules.py"),
    (BR, "BOUNCE_ATR = 0.10", "BOUNCE_ATR = 0.0",
     "回抽位退回正关键位（2026-11-25 差 2 点不成交）", "tests/test_break_rules.py"),
    (BR, "if _reclaimed(rs[i][3]):", "if True:",
     "锚点永不解除（一根老锚点变成永久看空开关）", "tests/test_break_rules.py"),
    (BR, "            # 收盘重新站回关键位上方 → 那个\"跌破\"是假警报，前提恢复\n                anchor = None",
     "            # 变异：不解除\n                pass",
     "收复也不解除武装", "tests/test_break_rules.py"),
    (BR, "if rs_all >= 2", "if False",           # 占位，不存在则跳过
     "占位", "tests/test_break_rules.py"),
    (BR, "a_est = _atr_rows(rs_all[-14:]) if len(rs_all) >= 2 else 0.0",
     "a_est = _atr_rows(rs) if len(rs) >= 2 else 0.0",
     "ATR 只用入场后那几根算（判读随进场时点跳变）", "tests/test_break_rules.py"),
    (BR, "    rs = [r for r in rs_all if r[0] >= int(since_ts)] if since_ts else rs_all",
     "    rs = rs_all",
     "忽略 since_ts（拿入场前走势判跌破）", "tests/test_break_rules.py"),
    (BR, "return True, (\"有效跌破：%s\" % read.describe())",
     "return False, \"\"",
     "有效跌破也不走（永远等回抽）", "tests/test_break_rules.py"),
    (BR, "        return True, (\"收盘跌破后回抽到位：%s\" % read.describe())",
     "        return False, \"\"",
     "回抽到位也不走", "tests/test_break_rules.py"),
    (BR, "    reach = (px >= read.exit_level) if d > 0 else (px <= read.exit_level)",
     "    reach = (px <= read.exit_level) if d > 0 else (px >= read.exit_level)",
     "回抽方向判反（多空镜像搞错）", "tests/test_break_rules.py"),
    # ---- thesis.py 的接线 ----
    (TH, "        if _read.state == _br.CONFIRMED:", "        if False:",
     "状态机不再处理有效跌破", "tests/test_exit_break_wiring.py"),
    (TH, "                    _px = float(_read.exit_level)",
     "                    _px = float(bar.h)",
     "回抽出场价用当根最高价（前视偷价）", "tests/test_exit_break_wiring.py tests/test_break_rules.py"),
    (TH, "                    reason=\"closed_below_entry\", price=_px,",
     "                    reason=\"thesis_invalidated\", price=_px,",
     "收盘跌破的归因被换成失效（统计上无法区分）",
     "tests/test_exit_break_wiring.py"),
    (TH, "                _hit = (float(bar.h) >= _read.exit_level) if _long \\\n                    else (float(bar.l) <= _read.exit_level)",
     "                _hit = True",
     "窗口期只要还在就出场（不等回抽，等于立刻止损）",
     "tests/test_exit_break_wiring.py"),
    (TH, "    _fill_bar_ts: int = int(fill_bar_ts or 0)",
     "    _fill_bar_ts: int = 0",
     "锚点不看调用方传的 fill_bar_ts（增量推进下判据失效）",
     "tests/test_exit_break_wiring.py"),
    (TH, "                    _fill_bar_ts = sb_ts",
     "                    _fill_bar_ts = 0",
     "成交时不记锚点", "tests/test_exit_break_wiring.py"),
]


def _read(p):
    return io.open(p, encoding="utf-8").read()


def _clear_pyc() -> None:
    """删掉 ai_trader / tests 下的字节码缓存。

    ★ 只删目录里的 .pyc 不够 —— 变异脚本改的是源码，
      而 Python 的 .pyc 校验是 (mtime, size)，同秒内改等长内容可能命中旧缓存，
      于是"跑的是旧代码"，变异被判成存活。这是本轮实测踩到的坑。
    """
    import shutil
    for sub in ("ai_trader", "tests"):
        for dirpath, dirnames, _ in os.walk(os.path.join(ROOT, sub)):
            for d in list(dirnames):
                if d == "__pycache__":
                    shutil.rmtree(os.path.join(dirpath, d), ignore_errors=True)
                    dirnames.remove(d)


def main() -> int:
    ok = caught = skipped = 0
    survived = []
    for path, old, new, desc, tests in MUTANTS:
        src = _read(path)
        if old not in src:
            print("SKIP  %-58s （原串不存在）" % desc)
            skipped += 1
            continue
        io.open(path, "w", encoding="utf-8").write(src.replace(old, new, 1))
        # ★ 必须清 __pycache__：源码改了但 .pyc 的 mtime/大小校验可能仍命中，
        #   Python 会继续跑**旧字节码** —— 于是变异"存活"，其实是变异没生效。
        #   这个坑本轮真的踩到了（break_rules 的 .pyc 陈旧，
        #   导致 exit_now 源码和行为对不上、三条用例看似莫名其妙地失败）。
        _clear_pyc()
        try:
            r = subprocess.run([sys.executable, "-m", "pytest", "-x", "-q"]
                               + tests.split(),
                               cwd=ROOT, capture_output=True, text=True,
                               env=dict(os.environ, PYTHONIOENCODING="utf-8"))
            out = (r.stdout or "") + (r.stderr or "")
            # 只认"测试断言失败"；语法/收集错误不算抓住
            real = (r.returncode == 1
                    and ("SyntaxError" not in out)
                    and ("IndentationError" not in out)
                    and ("ERROR collecting" not in out))
            if real:
                print("CAUGHT %-57s rc=%d" % (desc, r.returncode))
                caught += 1
            else:
                print("SURVIVED %-55s rc=%d" % (desc, r.returncode))
                survived.append(desc)
        finally:
            io.open(path, "w", encoding="utf-8").write(src)
        ok += 1
    print("\n%d 个变异体：抓住 %d，存活 %d，跳过 %d"
          % (ok, caught, len(survived), skipped))
    if survived:
        print("存活清单：")
        for s in survived:
            print("  - %s" % s)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
