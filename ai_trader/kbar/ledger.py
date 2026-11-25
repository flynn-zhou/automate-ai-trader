# -*- coding: utf-8 -*-
"""维度记账的**公共**推导：判据到底读了哪些维度。

这个模块只做一件事，但它是「台账不许自己骗自己」的地基，所以单独放：

    `shape.judge` 读了哪些维度 —— **从源码 AST 求，不许手写字面量**。

## 为什么必须从代码求

`tests/test_dims_ledger.py` 原来手写着：

    JUDGE = {("kbar_evolution", c) for c in
             ("o", "h", "l", "c", "vol_rel", "taker_buy_ratio")} | {
                 ("kbar_line", "line_price")}

问题不在它**当时**对不对 —— 它对，但它和 `shape.py` 之间**没有任何连接**。
2026-11 审计实测：往 `judge` 里插一行 `_x = b.get("zone")`，整个
`test_dims_ledger.py` 照样全绿。也就是说「★ 必须等于代码实际读的列」这条
测试，防的是「文档 vs 手抄的代码」，不是「文档 vs 真代码」——
手抄那份一过时，测试就变成了自我确认。

所以推导挪到这里，**测试和 Web 快照面板共用同一份实现** ——
否则前端为了标 ★ 又抄一遍字面量，等于把刚拆掉的那个坑重挖一次。

## 求法分三步，每步都只认代码事实

1. **字面量键**：`judge` 及其本模块调用闭包里，所有 `x["k"]` / `x.get("k")`
   的字符串键，与 `dims.DIM_NAMES` 取交集。覆盖 OHLC / vol_rel /
   taker_buy_ratio 这类直读。
2. **声明式入参**：`judge` 自己的形参名里，与 `DIM_NAMES` 同名的那些。
   目前就是 `n_touch` —— 它是「无」拆成「无·真空/无·贴线」的唯一依据
   （`judge` 里 `if int(n_touch) < NO_SPLIT_BY_TOUCH`），确实决定 label。
   ⚠ 只对 `judge` 本身做这一步，**不进闭包**：闭包里的辅助函数有形参叫
   `side`（`pin_extreme_kept(..., side, ...)`），那是「上/下针」的方向字符串，
   不是维度 `side`，进闭包会误报。
3. **动态键**：线快照是 `{line_key: price}` 这种「键名就是数据」的字典，
   AST 里看不到 `line_price` 这个字面量。所以只要 `judge` 有
   `prev_lines` / `lines_at_bar` 这类线快照入参，就认定它读了 `line_price`。

## 不变量

`ledger` 只**读** `shape.py` 的源码文本，不 import 它。
所以「`shape.py` 一读未来、判定就偷看未来」那条红线不受影响：
本模块不参与任何判定，只在上层被测试和只读接口调用。
"""
from __future__ import annotations

import ast
import os
from functools import lru_cache
from typing import Dict, FrozenSet, List, Tuple

from . import dims as D

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
_SHAPE_SRC = os.path.join(_ROOT, "ai_trader", "kbar", "shape.py")

# 维度名 → 总账里登记它的那张表。没列在这里的都算表 1。
# 这条映射必须和 docs/K线演化维度总账.md §四 的登记表一致，
# `test_dims_ledger.py` 会逐列核对（它读的就是本函数的返回值）。
TABLE_OF: Dict[str, str] = {"line_price": "kbar_line"}


def _parse_shape() -> ast.Module:
    with open(_SHAPE_SRC, encoding="utf-8") as f:
        return ast.parse(f.read())


def _literals(fn) -> set:
    """`x["k"]` 与 `x.get("k")` 里的字符串键。"""
    out = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Subscript):
            s = n.slice
            if isinstance(s, ast.Index):          # Python < 3.9
                s = s.value
            if isinstance(s, ast.Constant) and isinstance(s.value, str):
                out.add(s.value)
        elif isinstance(n, ast.Call):
            f = n.func
            if (isinstance(f, ast.Attribute) and f.attr == "get"
                    and n.args):
                a = n.args[0]
                if isinstance(a, ast.Constant) and isinstance(a.value, str):
                    out.add(a.value)
    return out


def _callees(fn, mod_funcs) -> set:
    out = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
            if n.func.id in mod_funcs:
                out.add(n.func.id)
    return out


@lru_cache(maxsize=1)
def judge_dim_names() -> FrozenSet[str]:
    """`shape.judge` 实际读到的维度名集合（缓存：源码在一次运行里不变）。"""
    tree = _parse_shape()
    mod_funcs = {n.name: n for n in tree.body
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    if "judge" not in mod_funcs:
        raise AssertionError("shape.py 里找不到 judge —— 判据被改名了？")

    names = set(D.DIM_NAMES)

    # judge + 它的本模块传递调用闭包（只走函数名，不跨 import）
    closure, todo = set(), ["judge"]
    while todo:
        nm = todo.pop()
        if nm in closure or nm not in mod_funcs:
            continue
        closure.add(nm)
        todo.extend(_callees(mod_funcs[nm], mod_funcs))

    hits = set()
    for nm in closure:
        hits |= _literals(mod_funcs[nm]) & names

    args = mod_funcs["judge"].args
    params = {a.arg for a in
              list(getattr(args, "posonlyargs", [])) + list(args.args)
              + list(args.kwonlyargs)}
    hits |= params & names

    if {"prev_lines", "lines_at_bar"} & params:
        hits.add("line_price")

    return frozenset(hits)


def judge_reads() -> FrozenSet[Tuple[str, str]]:
    """→ {(表, 列)}，给总账逐列核对用。"""
    return frozenset((TABLE_OF.get(x, "kbar_evolution"), x)
                     for x in judge_dim_names())


@lru_cache(maxsize=1)
def dim_catalog() -> List[Dict]:
    """39 个维度的静态说明：[{name, cat, starred}]，给快照面板分组用。

    `starred` 来自 `judge_dim_names()` —— 前端不许自己写「哪 8 个是判据」
    的字面量，那正是 2026-11 审计拆掉的坑。
    """
    judge = judge_dim_names()
    return [{"name": d.name, "cat": d.cat, "starred": d.name in judge}
            for d in D.DIMS]
