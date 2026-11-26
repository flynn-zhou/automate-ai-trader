# -*- coding: utf-8 -*-
"""反包策略的**变异测试**：故意改坏实现，确认 `tests/test_engulf.py` 会红。

为什么必须有这一步
------------------

「测试全绿」本身不证明测试有用。这个项目已经踩过一次：反包形态用严格
不等号（`b.o < p.c`）判定实体包含，于是「今开 == 前收」这种连续市场的
**常态**被静默漏掉 —— 测试当时也是全绿的，因为没有任何一条测试喂过
「今开恰好等于前收」这个用例。

变异测试就是把「删掉一条规则，测试会不会报警」这件事自动化：
每个变异体只改一处真实缺陷，跑一遍测试套件，**必须至少一条测试失败**。
存活下来的变异体 = 那一处逻辑没有任何测试在保护它。

用法
----

    python tools/mut_engulf.py            # 全部
    python tools/mut_engulf.py -k 反包     # 只跑名字含关键字的

★ 每个变异体跑完都会**恢复原文件**，并且清掉 `__pycache__` ——
  Python 会按 (mtime, size) 复用旧字节码，而变异体常常与原文件同尺寸、
  同一秒写入，于是「源码改了但跑的还是旧逻辑」，变异测试会假报存活。
  实测踩过（见 `mut_vol_gate.py` 的注释）。
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGET = os.path.join(ROOT, "ai_trader", "trading", "engulf.py")
ENGINE = os.path.join(ROOT, "ai_trader", "trading", "engine.py")
PENDING = os.path.join(ROOT, "ai_trader", "trading", "pending_plan.py")
TESTS = os.path.join(ROOT, "tests", "test_engulf.py")

#: (名字, 目标文件, 原文, 替换, 说明, 是否等价变异体)
#:
#: 第五个元素为 True 的是**等价变异体**：改法与原文在**所有输入**上行为
#: 完全相同，测试不可能抓住它。这类变异体不算测试盲区，必须逐步解释清楚，
#: 否则「全部杀死」这个结论会被自己注水。
MUTANTS = [
    ("反包_严格小于_漏掉今开等于前收", TARGET,
     "and float(cur.c) >= float(prev.o)",
     "and float(cur.c) > float(prev.o)",
     "把 `>=` 改回 `>`：11-25 22:45 这类标准反包会被静默漏掉"),
    ("反包_上沿不校验", TARGET,
     "and float(cur.c) >= float(prev.o)",
     "and True",
     "完全不校验上沿：任何阳线都算反包"),
    ("反包_下沿不校验", TARGET,
     "and float(cur.o) <= float(prev.c)",
     "and True",
     "完全不校验下沿：跳空高开的阳线也算反包"),
    ("手续费门槛归零", TARGET,
     "MIN_R_COST_MULT = 6.0",
     "MIN_R_COST_MULT = 0.0",
     "关掉决定性过滤器 → 1R 只有 100 点的小样本也放行"),
    ("手续费门槛放松到一倍", TARGET,
     "MIN_R_COST_MULT = 6.0",
     "MIN_R_COST_MULT = 1.0",
     "门槛从 6 倍降到 1 倍 → 费/1R 吃掉全部毛优势"),
    ("往返费率写错", TARGET,
     "ROUND_TRIP_FEE = 0.0002 + 0.0005",
     "ROUND_TRIP_FEE = 0.0002",
     "漏掉出场 taker → 回测与实盘脱节"),
    ("档位重复", TARGET,
     "TRANCHE_LEVELS: Tuple[float, ...] = (0.8, 0.5, 0.3)",
     "TRANCHE_LEVELS: Tuple[float, ...] = (0.8, 0.5, 0.5)",
     "两档同价 → plan_key 去重成一条 = 只剩三分之二仓"),
    ("目标倒序", TARGET,
     "TRANCHE_TARGETS: Tuple[float, ...] = (1.0, 2.0, 3.0)",
     "TRANCHE_TARGETS: Tuple[float, ...] = (3.0, 2.0, 1.0)",
     "第一份拿 3R、第三份拿 1R —— 与用户口径正好相反"),
    ("风险分摊关掉", TARGET,
     "RISK_SCALE = 1.0 / float(len(TRANCHE_LEVELS))",
     "RISK_SCALE = 1.0",
     "三档各吃满额风险 → 敞口 10.10% 变 30.30%（生产安全红线）"),
    ("未知形态给零风险系数", TARGET,
     "return float(RISK_SCALE) if kind == KIND_ENGULF else 1.0",
     "return float(RISK_SCALE) if kind == KIND_ENGULF else 0.0",
     "既有三条形态拿到 0 → 手数算成 0 → 全部静默不开仓"),
    ("波动率过滤器恒真", TARGET,
     "    idx_max = lo + amps.index(max(amps))",
     "    return True\n    idx_max = lo + amps.index(max(amps))",
     "过滤器形同虚设 → 用户「太远了就不用」的口径失效"),
    ("波动率不校验近期性", TARGET,
     "    return idx_max >= r0",
     "    return idx_max >= 0",
     "只要窗口内有波动就放行 → 尖峰在 15 根前也过"),
    ("波动率为零时放行", TARGET,
     "    if not amps or max(amps) <= 0:\n        return False",
     "    if not amps:\n        return False",
     "全平盘时放行 → 用户「没有波动率的时候不用」失效", True),
    ("止损放在最低点上方", TARGET,
     "    stop = l - STOP_TICKS * tick",
     "    stop = l + STOP_TICKS * tick",
     "做多止损跑到最低点上方 → Plan.validate 的价位关系被破坏"),
    ("计划丢掉risk_scale", TARGET,
     '                "risk_scale": RISK_SCALE,',
     '                "risk_scale": 1.0,',
     "计划证据里写 1.0 → 引擎按全额风险下单（三倍敞口）"),
    ("计划丢掉stop_distance", TARGET,
     '                "stop_distance": abs(float(t["entry"]) - float(t["stop"])),',
     '                "stop_distance": 0.0,',
     "挂单期跑偏撤销距离变 0 → 僵尸计划永不撤销，价格跑了还照价成交"),
    ("计划产出两档", TARGET,
     "    for level, mult in zip(TRANCHE_LEVELS, TRANCHE_TARGETS):",
     "    for level, mult in list(zip(TRANCHE_LEVELS, TRANCHE_TARGETS))[:2]:",
     "只出两档 → 第三份（3R 那一份）永远不下单"),
    ("thesis_id不带挂单价", PENDING,
     '    tid = thesis_id or "%s-%s-%s-%.4f" % (\n'
     '        plan.direction, plan.kind, plan.bar_ts, float(plan.order_price or 0.0))',
     '    tid = thesis_id or "%s-%s-%s" % (\n'
     '        plan.direction, plan.kind, plan.bar_ts)',
     "三档撞成同一个 thesis_id → 落库互相覆盖，下三档只剩一条"),
    ("仓位闸忽略risk_scale", ENGINE,
     'risk_scale=float((intent.meta or {}).get("risk_scale", 1.0)))',
     "risk_scale=1.0)",
     "闸门不透传系数 → 三档各吃满额风险"),
    ("风险系数非法不拒绝", ENGINE,
     '        if not (_scale > 0):\n            return 0.0, "风险系数非法：%r（必须 > 0）" % (risk_scale,)',
     '        if False:\n            return 0.0, "风险系数非法：%r（必须 > 0）" % (risk_scale,)',
     "系数 0 时手数算成 inf，直接下出一张天上掉的单"),
    ("仓位计算忽略系数", ENGINE,
     'risk_pct=float(params["sizing.risk_pct"]) * _scale,',
     'risk_pct=float(params["sizing.risk_pct"]),',
     "系数传进来了但没用 → 分摊形同虚设"),
    ("缩仓改成放大止损距离", ENGINE,
     "pp = plan_position(cfg, risk_frac * 100.0)",
     "pp = plan_position(cfg, risk_frac * 100.0 / _scale)",
     "拿爆仓检查共用的止损距离当缩仓旋钮 → 误触「止损超上限」拒单"),
    ("缩仓方向写反", ENGINE,
     "pp = plan_position(cfg, risk_frac * 100.0)",
     "pp = plan_position(cfg, risk_frac * 100.0 * _scale)",
     "乘而不是除 → scale=1/3 时手数反而放大 3 倍（实测踩过）"),
]


def _clear_pycache():
    """删掉仓库里的 __pycache__。

    ★ 不清会假报「存活」：Python 按 (mtime, size) 复用字节码，
      而变异体常与原文件同尺寸、同秒写入。
    """
    for base, dirs, _ in os.walk(ROOT):
        if "__pycache__" in dirs:
            shutil.rmtree(os.path.join(base, "__pycache__"),
                          ignore_errors=True)
            dirs.remove("__pycache__")


def _run_tests():
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    p = subprocess.run(
        [sys.executable, "-m", "pytest", TESTS, "-q", "--no-header", "-x"],
        cwd=ROOT, env=env, capture_output=True,
        encoding="utf-8", errors="replace")
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-k", default="", help="只跑名字含该关键字的变异体")
    args = ap.parse_args()

    # 先确认**未变异**时是绿的，否则后面全是噪音
    _clear_pycache()
    rc, out = _run_tests()
    if rc != 0:
        print("[先决条件失败] 未变异时测试就不绿，先修测试：")
        print(out[-2000:])
        return 2
    print("[基线] 测试全绿，开始变异\n")

    killed = survived = equiv = 0
    bad = []
    for row in MUTANTS:
        name, path, old, new, why = row[:5]
        is_equiv = bool(row[5]) if len(row) > 5 else False
        if args.k and args.k not in name:
            continue
        with open(path, "r", encoding="utf-8") as f:
            orig = f.read()
        if orig.count(old) != 1:
            print("  !! %-28s 原文匹配 %d 次（应为 1），跳过"
                  % (name, orig.count(old)))
            bad.append(name)
            continue
        mutated = orig.replace(old, new, 1)
        tmp = tempfile.mkdtemp()
        backup = os.path.join(tmp, os.path.basename(path))
        shutil.copy2(path, backup)
        try:
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(mutated)
            _clear_pycache()
            rc, out = _run_tests()
        finally:
            shutil.copy2(backup, path)
            _clear_pycache()
            shutil.rmtree(tmp, ignore_errors=True)
        if rc != 0:
            killed += 1
            print("  ✅ 杀死  %-28s %s" % (name, why))
        elif is_equiv:
            equiv += 1
            print("  ≈  等价  %-28s %s" % (name, why))
        else:
            survived += 1
            print("  ❌ 存活  %-28s %s" % (name, why))

    total = killed + survived + equiv + len(bad)
    print("\n" + "=" * 78)
    print("变异测试：%d 个变异体 · 杀死 %d · 存活 %d · 等价 %d · 配置错误 %d"
          % (total, killed, survived, equiv, len(bad)))
    if survived or bad:
        print("★ 存活的变异体说明那一处逻辑**没有任何测试在保护它**。")
        return 1
    print("全部杀死（等价变异体除外）—— 每一处关键逻辑都有测试兜底。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
