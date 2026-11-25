# -*- coding: utf-8 -*-
"""变异验证：Algo Order 端点迁移（-4120）的每条守卫。

纪律同前：rc=2/3/4 无效；桩缺方法的 AttributeError 无效；
「不许抛/不许静默」类守卫被打破时抛出的异常**就是**它的失败形态。
"""
import io
import subprocess
import sys

P = "ai_trader/exchange/live.py"
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
        lines = [l.strip() for l in out.splitlines() if l.strip().startswith("E   ")]
        print("=" * 72)
        print("变异：%s" % name)
        print("  rc=%d" % r.returncode)
        if r.returncode in (2, 3, 4) or "SyntaxError" in out:
            print("  => !! 无效：收集/语法错误")
            return False
        if r.returncode == 0:
            print("  => !! 存活：回归不会被发现")
            return False
        hit = [l for l in lines if "AssertionError" in l]
        if hit:
            print("  断言：%s" % hit[0][:110])
            print("  => 正确")
            return True
        stub = any("has no attribute '_" in l or "SimpleNamespace" in l
                   for l in lines)
        if stub or not lines:
            print("  首行：%s" % (lines[0][:110] if lines else "-"))
            print("  => !! 无效：桩缺方法/无失败行")
            return False
        print("  异常：%s" % lines[0][:110])
        print("  => 正确（该守卫的失败形态就是抛）")
        return True
    finally:
        io.open(P, "w", encoding="utf-8").write(ORIG)
        print("  已还原")


results = []

# ① 条件单不走 algo 端点（回到 -4120）
results.append(run(
    "① 条件单走旧端点",
    "        if self._needs_algo(order_type):",
    "        if False:",
    "tests/test_algo_conditional_orders.py::"
    "test_stop_market_goes_to_algo_endpoint_not_the_old_one"))

# ② 字段名用旧的（stopPrice / newClientOrderId）
results.append(run(
    "② 用旧字段名 stopPrice",
    '                "triggerPrice": "%.*f" % (spec["price_precision"],',
    '                "stopPrice": "%.*f" % (spec["price_precision"],',
    "tests/test_algo_conditional_orders.py::"
    "test_algo_request_uses_the_algo_field_names"))

# ③ 漏 algoType
results.append(run(
    "③ 漏 algoType",
    '        algo = {"algoType": "CONDITIONAL",',
    '        algo = {',
    "tests/test_algo_conditional_orders.py::"
    "test_algo_request_uses_the_algo_field_names"))

# ④ 限价单也被改到 algo 端点
results.append(run(
    "④ 限价单误走 algo",
    '        if self._needs_algo(order_type):',
    '        if True:',
    "tests/test_algo_conditional_orders.py::"
    "test_limit_orders_still_use_the_old_endpoint"))

# ⑤ 响应字段译错（orderId 而不是 algoId）
results.append(run(
    "⑤ 响应字段译错",
    '            order_id=str(d.get("algoId", "") or ""),',
    '            order_id=str(d.get("orderId", "") or ""),',
    "tests/test_algo_conditional_orders.py::"
    "test_algo_response_fields_are_mapped"))

# ⑥ 状态译错（status 而不是 algoStatus）→ working 判错
results.append(run(
    "⑥ 状态字段译错",
    '            status=str(d.get("algoStatus", "") or ""),',
    '            status=str(d.get("status", "") or ""),',
    "tests/test_algo_conditional_orders.py::"
    "test_algo_response_fields_are_mapped"))

# ⑦ open_orders 不合并条件单簿
results.append(run(
    "⑦ 不合并条件单簿",
    "            out.extend(await self.open_algo_orders(sym))",
    "            pass",
    "tests/test_algo_conditional_orders.py::"
    "test_open_orders_merges_the_algo_book"))

# ⑧ 条件单簿失败时静默返回空
results.append(run(
    "⑧ 条件单簿失败返回空",
    '            logger.error("[实盘] 拉条件单列表失败：%s", e)\n'
    '            raise',
    '            logger.error("[实盘] 拉条件单列表失败：%s", e)\n'
    '            return []',
    "tests/test_algo_conditional_orders.py::"
    "test_algo_book_failure_does_not_silently_look_empty"))

# ⑨ 查询不回落条件单簿
results.append(run(
    "⑨ 查询不回落条件单簿",
    "        if not client_id:\n"
    "            return None\n"
    "        try:\n"
    "            for o in await self.open_algo_orders(sym):\n"
    "                if o.client_id == client_id:\n"
    "                    return o\n",
    "        if True:\n"
    "            return None\n"
    "        try:\n"
    "            for o in await self.open_algo_orders(sym):\n"
    "                if o.client_id == client_id:\n"
    "                    return o\n",
    "tests/test_algo_conditional_orders.py::"
    "test_query_falls_back_to_the_algo_book_by_client_id"))

# ⑩ 撤单不回落 algo 端点
results.append(run(
    "⑩ 撤单不回落 algo",
    "            if await self._cancel_algo(order_id, client_id, sym):\n"
    "                return True",
    "            pass",
    "tests/test_algo_conditional_orders.py::"
    "test_cancel_falls_back_to_the_algo_endpoint"))

# ⑪ 已不在的条件单按失败处理
results.append(run(
    "⑪ 已不在按失败",
    '            if "-2013" in str(e) or "Order does not exist" in str(e):\n'
    '                logger.info("[实盘] 条件单已不在（%s），按撤单成功处理",\n'
    '                            order_id or client_id)\n'
    '                return True',
    '            if False:\n'
    '                return True',
    "tests/test_algo_conditional_orders.py::"
    "test_cancel_algo_treats_already_gone_as_success"))

# ⑫ allOrders 兜底被删
results.append(run(
    "⑫ 删掉 allOrders 兜底",
    '            rows = await self._req("GET", "/fapi/v1/allOrders",\n'
    '                                   {"symbol": sym, "limit": 200})',
    '            rows = []',
    "tests/test_algo_conditional_orders.py::"
    "test_closed_order_lookup_uses_all_orders_fallback"))

print()
print("=" * 72)
ok = sum(1 for r in results if r)
print("结果：%d/%d 有效抓住" % (ok, len(results)))
sys.exit(0 if ok == len(results) else 1)
