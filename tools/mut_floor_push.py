# -*- coding: utf-8 -*-
"""变异验证：A（推送）与 B（策略仓地板单）的每条守卫都必须真能抓住回归。

纪律（这一轮已经吃过两次亏，写死在这里）：
  · rc=2/3/4（收集/语法错误）不算抓住 —— 测试根本没跑
  · 非零退出但看不到断言失败行，也不算（可能是桩的 AttributeError）
  · 变异必须打**真正守它的那条测试**，否则"存活"只是覆盖分工
"""
import io
import subprocess
import sys

P = "ai_trader/trading/engine.py"
ORIG = io.open(P, encoding="utf-8").read()


def run(name, old, new, target):
    if old not in ORIG:
        print("!! 锚点没找到（无效）：%s" % name)
        return False
    mutated = ORIG.replace(old, new, 1)
    assert mutated != ORIG
    io.open(P, "w", encoding="utf-8").write(mutated)
    try:
        r = subprocess.run([sys.executable, "-m", "pytest", target, "-q",
                            "-p", "no:warnings", "--tb=short"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace")
        out = (r.stdout or "") + (r.stderr or "")
        lines = [l.strip() for l in out.splitlines()
                 if l.strip().startswith("E   ")]
        print("=" * 72)
        print("变异：%s" % name)
        print("  守它：%s" % target)
        print("  rc=%d" % r.returncode)
        if r.returncode in (2, 3, 4) or "SyntaxError" in out:
            print("  => !! 无效：收集/语法错误")
            return False
        if r.returncode == 0:
            print("  => !! 存活：回归不会被发现")
            return False
        if not lines:
            print("  => !! 无效：非零退出但无失败行")
            return False
        hit = [l for l in lines if "AssertionError" in l]
        if hit:
            print("  断言：%s" % hit[0][:110])
            print("  => 正确：被真断言抓住")
            return True
        # 「不许抛」这类守卫被打破时，抛出来的异常**就是**它的失败形态
        # （例如 notifier 抛 RuntimeError 而 try/except 没了）。
        # 但要排除"桩缺方法"那种假抓住 —— 那种说明测试根本没跑到产品逻辑。
        stub_missing = any("AttributeError" in l and "SimpleNamespace" in l
                           or "has no attribute '_" in l for l in lines)
        if stub_missing:
            print("  首行：%s" % lines[0][:110])
            print("  => !! 无效：桩缺方法（不是产品缺陷被抓住）")
            return False
        print("  异常：%s" % lines[0][:110])
        print("  => 正确：被异常抓住（该守卫的失败形态就是抛）")
        return True
    finally:
        io.open(P, "w", encoding="utf-8").write(ORIG)
        print("  已还原")


results = []

# ① 地板单不挂（挂单动作整段短路）
results.append(run(
    "① 策略仓地板单不挂",
    '        if str(getattr(self.ex, "name", "") or "") != "live":\n'
    '            return\n'
    '        pos = self.pos\n',
    '        if True:\n'
    '            return\n'
    '        pos = self.pos\n',
    "tests/test_system_stop_floor.py"))

# ② 维护调用被挪进接管仓分支（我第一版就是这个错）
results.append(run(
    "② 维护被挪进接管仓分支",
    "        try:\n"
    "            await self._maintain_system_protection(price)\n"
    "        except Exception as e:                           # noqa: BLE001\n"
    "            logger.error(\"[地板] 维护失败（下一 tick 重试）：%s\", e)\n"
    "\n"
    "        if self._is_adopted():\n",
    "        if self._is_adopted():\n"
    "            try:\n"
    "                await self._maintain_system_protection(price)\n"
    "            except Exception as e:                       # noqa: BLE001\n"
    "                logger.error(\"[地板] 维护失败（下一 tick 重试）：%s\", e)\n",
    "tests/test_system_stop_floor.py::"
    "test_step_maintains_system_stop_outside_the_adopted_branch"))

# ③ 幂等失效（每次都重挂）
results.append(run(
    "③ 幂等失效（每次重挂）",
    '        if (state.get("oid") and abs(float(state.get("px") or 0.0) - stop) < 1e-9\n'
    '                and abs(float(state.get("qty") or 0.0) - qty) < 1e-12):\n'
    '            return                                   # 已对齐，不发请求\n',
    '        if False:\n'
    '            return\n',
    "tests/test_system_stop_floor.py::"
    "test_system_stop_is_idempotent_when_already_aligned"))

# ④ 已穿过止损失仍然去挂（-2021 拒单）
results.append(run(
    "④ 止损失已被穿过仍去挂",
    '        if price > 0 and ((is_long and price <= stop)\n'
    '                          or ((not is_long) and price >= stop)):\n',
    '        if False:\n',
    "tests/test_system_stop_floor.py::"
    "test_system_stop_is_skipped_when_price_already_through_it"))

# ⑤ 无仓不撤地板单（白占保证金）
results.append(run(
    "⑤ 无仓时不撤地板单",
    '        if not pos:\n'
    '            await self._cancel_system_stop_order("已无策略仓")\n'
    '            return\n',
    '        if not pos:\n'
    '            return\n',
    "tests/test_system_stop_floor.py::"
    "test_system_stop_is_cancelled_when_position_is_gone"))

# ⑥ 纸面也挂（会造成"以为有保护"）
results.append(run(
    "⑥ 纸面模式也挂地板单",
    '        if str(getattr(self.ex, "name", "") or "") != "live":\n'
    '            return\n'
    '        pos = self.pos\n',
    '        pos = self.pos\n',
    "tests/test_system_stop_floor.py"))

# ⑦ 成交价还原：不认地板单，只认接管仓那张
results.append(run(
    "⑦ 不认地板单的成交价",
    '            for _cid, _src in ((_system_stop_cid(entry_ts), "策略仓地板单"),\n'
    '                               (self._adopted_stop_cid(slot), "接管仓保护单")):',
    '            for _cid, _src in ():',
    "tests/test_system_stop_floor.py::"
    "test_stop_fill_is_booked_at_its_real_price_not_at_entry"))

# ⑧ 部分成交也当整笔的价
results.append(run(
    "⑧ 部分成交当整笔价",
    '                if qty > 0 and got > 0 and got + 1e-9 < qty:',
    '                if False:',
    "tests/test_system_stop_floor.py::"
    "test_partial_fill_is_not_used_as_the_whole_position_price"))

# ⑨ 占位记账不留痕（把事件名换掉 —— 只删 log_risk 的调用形式，
#    `position_closed_unpriced` 这个字符串还在源码里，测不出区别）
results.append(run(
    "⑨ 占位记账不记风险事件",
    '                    "position_closed_unpriced",',
    '                    "position_closed_quiet_renamed",',
    "tests/test_system_stop_floor.py::test_unpriced_close_is_not_silent"))

# ⑩ 推送抛异常不兜住（会把交易搞崩）
results.append(run(
    "⑩ 推送异常不兜住",
    '        except Exception as e:                               # noqa: BLE001\n'
    '            # 只记日志，而且**不记 risk 事件**：通知失败本身不是交易风险，\n'
    '            # 落进 risk_events 只会污染"最近出过什么事"这个视图。\n'
    '            logger.warning("[通知] 推送失败（不影响交易）：%s", e)',
    '        except ZeroDivisionError as e:\n'
    '            logger.warning("[通知] 推送失败：%s", e)',
    "tests/test_money_events_push.py::"
    "test_push_swallows_errors_from_the_notifier"))

# ⑪ 闸门推送不节流
results.append(run(
    "⑪ 保证金不足推送不节流",
    '        self._push("【拒单】可用保证金不足", "%s\\n%s" % (tid, why),\n'
    '                   key="pending_margin_short", min_gap_s=3600)',
    '        self._push("【拒单】可用保证金不足", "%s\\n%s" % (tid, why))',
    "tests/test_money_events_push.py::test_gate_rejections_push_with_throttle"))

# ⑫ 平仓不推送
results.append(run(
    "⑫ 平仓不推送",
    '        # 钱动了就要叫人（不节流）\n'
    '        self._push(\n'
    '            "【平仓·挂单策略】',
    '        (lambda *a, **k: None)(\n'
    '            "【平仓·挂单策略】',
    "tests/test_money_events_push.py::test_pending_fill_and_pending_close_push"))

print()
print("=" * 72)
ok = sum(1 for r in results if r)
print("结果：%d/%d 有效抓住" % (ok, len(results)))
sys.exit(0 if ok == len(results) else 1)
