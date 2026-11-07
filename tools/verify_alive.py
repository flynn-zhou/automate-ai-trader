# -*- coding: utf-8 -*-
"""一条命令核对系统还活着。全部 ✓ 再谈策略。

用法：cd /home/cryptobot/ai-trader && ./.venv/bin/python tools/verify_alive.py
"""
import json
import os
import sqlite3
import subprocess
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ok = fail = 0


def chk(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print("  [OK  ] %-26s %s" % (name, detail))
    else:
        fail += 1
        print("  [FAIL] %-26s %s" % (name, detail))


print("=" * 70)
print("1. 服务")
for u in ("ai-trader", "ai-trader-sentinel", "ai-trader-healthcheck.timer",
          "ai-trader-snapshot.timer"):
    r = subprocess.run(["systemctl", "is-active", u], capture_output=True, text=True)
    chk(u, r.stdout.strip() == "active", r.stdout.strip())

print()
print("2. 日志 ERROR（应用日志，不是 journalctl）")
log = os.path.join(ROOT, "logs", "ai-trader.log")
if os.path.exists(log):
    lines = open(log, encoding="utf-8", errors="replace").read().splitlines()
    s = 0
    for i, l in enumerate(lines):
        if "启动 env=" in l:
            s = i
    tail = lines[s:]
    errs = [l for l in tail if " ERROR " in l]
    chk("近期 ERROR 行数", len(errs) == 0, "%d 行（自上次启动起 %d 行日志）" % (len(errs), len(tail)))
    for e in errs[:3]:
        print("        ! " + e[:110])
else:
    chk("日志文件", False, "找不到 %s" % log)

print()
print("3. 大屏状态")
try:
    d = json.load(urllib.request.urlopen("http://127.0.0.1:8090/api/trade/state", timeout=20))
    t = d.get("trend") or {}
    g = d.get("guard") or {}
    st = d.get("stats") or {}
    chk("策略", t.get("mode") == "donchian", str(t.get("mode")))
    chk("周期", t.get("interval") == "1h", str(t.get("interval")))
    chk("就绪", bool(t.get("ready")), str(t.get("ready")))
    chk("上轨/下轨", t.get("upper") and t.get("lower"),
        "%s / %s" % (t.get("upper"), t.get("lower")))
    chk("现价", bool(t.get("price")),
        "%s  距上轨 %.2f%%  需涨 %.2f%%"
        % (t.get("price"), t.get("dist_pct") or 0, t.get("need_pct") or 0))
    chk("计划", t.get("plan_qty") is not None,
        "qty=%s 名义=%sU blocked=%s"
        % (t.get("plan_qty"), t.get("plan_notional"), t.get("plan_blocked")))
    chk("风控闸", not g.get("halted_hard"),
        "halted=%s temp=%r 回撤=%.2f%%"
        % (g.get("halted_hard"), g.get("temp_block"), g.get("drawdown_pct") or 0))
    print("       decisions=%s  trades=%s  equity=%s" %
          (st.get("decisions"), st.get("trades"), d.get("equity")))
except Exception as e:
    chk("大屏 API", False, str(e)[:80])

print()
print("4. env 回读（读文件，不读内存）")
env = {}
try:
    for line in open("/etc/ai-trader/env", encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    for k, want in (("AI_STRATEGY", "donchian"), ("AI_EXEC_MARKET", "perp"),
                    ("AI_DONCHIAN_ENTRY", "150"), ("AI_DONCHIAN_EXIT", "75")):
        chk(k, env.get(k) == want, "%s（期望 %s）" % (env.get(k), want))
    chk("AI_TRADE_MODE", True, env.get("AI_TRADE_MODE"))
    print("       密钥长度 %d/%d（不打印值）"
          % (len(env.get("AI_API_KEY", "")), len(env.get("AI_API_SECRET", ""))))
except Exception as e:
    chk("env 读取", False, str(e)[:80])

print()
print("5. 记账库")
try:
    c = sqlite3.connect(os.path.join(ROOT, "data", "market_trade.db"))
    bal = c.execute("select balance from executor_state").fetchone()
    chk("执行器余额", bal is not None, "%.4fU" % (bal[0] if bal else 0))
    nt = c.execute("select count(*) from trades").fetchone()[0]
    chk("trade 表行数", True, "%d（含历史现货实验，保留作审计）" % nt)
    c.close()
except Exception as e:
    chk("记账库", False, str(e)[:80])

print()
print("=" * 70)
print("通过 %d 项，失败 %d 项" % (ok, fail))
print("结论：" + ("系统正常" if fail == 0 else "有 %d 项需要处理，别急着谈策略" % fail))
sys.exit(0 if fail == 0 else 1)
