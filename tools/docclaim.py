# -*- coding: utf-8 -*-
"""扫「测试 docstring 声称覆盖了某行为，但对应代码从未被执行」。

起因：第 101 轮发现 tests/test_first_live_trade.py 的 docstring 写着
「买入前 _equity_stale 拒绝入场」，而整个文件里 _equity_stale 只出现在那句 docstring 里，
同时 _open 的守卫（engine.py:1089-1093）在覆盖率里一行没跑。
清单上划了勾、实际没做 —— 这比「没写测试」更危险，因为它让人以为有保护。

第 102 轮把这件事做成机械扫描，并交叉验证。

用法：
    AI_COV=1 AI_COV_OUT=tools/_cov.json python -m pytest tests -q
    python tools/docclaim.py

===============================================================
★ 这个工具的【三个已知假阳性来源】，用它之前必须先读：
===============================================================

1) 函数的 def 行在 import 时就执行了。
   如果按「该标识符有任意一行被执行」来判，那么只要模块被 import 过，
   任何函数都会显得「测试过」。所以本工具只统计【使用点】
   （调用/属性读写/变量引用），显式排除 def 行和 import 行。
   这一条最初写漏了，导致 24 个假阴性被当成假阳性放过去。

2) 同名标识符跨模块冲突。
   `klines` 在 market/rest.py、trend.py、feed.py 各有一份。
   测试针对 trend.py 的种子路径，而未执行的是 feed.py:127 的 REST 兜底，
   两者同名但无关，会被误判成「声称测了却没跑」。

3) docstring 提到某个名字，可能是在说明【它没被调用】。
   例：tests/test_trend_risk_gate.py 写着「趋势分支不调用 check_risk_state」，
   这是有价值的文档，不是谎报。

===============================================================
★ 第 102 轮的实测结论：真阳性 0 个。
===============================================================

交叉验证后只剩 3 个候选，逐一核实全部是假阳性：
  ERROR           散文里的词（「没有 ERROR 循环」），源码匹配到 aiohttp.WSMsgType.ERROR
  check_risk_state docstring 在说明它【没被调用】（假阳性类型 3）
  klines          同名冲突（假阳性类型 2）

也就是说：_equity_stale 那一个是孤例，不是普遍现象。
这个负结果写在这里，是为了避免将来再花一整轮重跑同一件事。

> 顺带记一条方法结论：**docstring 扫描只能产生假设，覆盖率才是验证。**
> 单独用任何一个都会撒谎。
"""
import ast
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, 'ai_trader')
TESTS = os.path.join(ROOT, 'tests')
IDENT = re.compile(r'\b([A-Za-z_][A-Za-z0-9_]{2,})\b')

# 普通词 / 日志级别 / 关键字：不是行为名，扫进来只会制造噪声
STOP = set("""board default error warning debug critical info exception
true false none self return if else for while import from class def
test tests assert string int float dict list set bool object type""".split())


def py_files(d):
    for dp, _dn, fns in os.walk(d):
        if '__pycache__' in dp:
            continue
        for fn in fns:
            if fn.endswith('.py'):
                yield os.path.join(dp, fn)


def split_doc_and_code(path):
    """把文件拆成「docstring 文本」和「真实代码文本」。"""
    src = open(path, encoding='utf-8').read()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return '', src
    lines = src.splitlines()
    docl = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            if ast.get_docstring(node, clean=False) is None:
                continue
            body = getattr(node, 'body', [])
            if body and isinstance(body[0], ast.Expr):
                a = getattr(body[0], 'lineno', None)
                b = getattr(getattr(body[0], 'value', None), 'end_lineno', a)
                if a:
                    for i in range(a, (b or a) + 1):
                        docl.add(i)
    doc = chr(10).join(lines[i - 1] for i in sorted(docl) if i <= len(lines))
    code = chr(10).join(l for i, l in enumerate(lines, 1) if i not in docl)
    return doc, code


def usage_sites():
    """标识符 -> 真正的【使用点】（排除 def 行与 import 行，见假阳性来源 1）。"""
    defs = set()
    out = {}
    for p in py_files(PKG):
        try:
            tree = ast.parse(open(p, encoding='utf-8').read())
        except SyntaxError:
            continue
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defs.add((p, getattr(n, 'lineno', 0)))
        for n in ast.walk(tree):
            ln = getattr(n, 'lineno', None)
            if not ln or (p, ln) in defs:
                continue
            if isinstance(n, (ast.Import, ast.ImportFrom)):
                continue
            names = []
            if isinstance(n, ast.Name):
                names = [n.id]
            elif isinstance(n, ast.Attribute):
                names = [n.attr]
            elif isinstance(n, ast.arg):
                names = [n.arg]
            for nm in names:
                out.setdefault(nm, set()).add((p, ln))
    return out


def main(cov_path='tools/_cov.json'):
    if not os.path.exists(cov_path):
        print('缺少 %s。先跑：' % cov_path)
        print('  AI_COV=1 AI_COV_OUT=%s python -m pytest tests -q' % cov_path)
        return 1
    cov = json.load(open(cov_path))
    hit = {k: set(v) for k, v in cov['files'].items()}
    us = usage_sites()
    code_ids = set()
    per = []
    for p in py_files(TESTS):
        doc, code = split_doc_and_code(p)
        per.append((p, doc, code))
        code_ids.update(IDENT.findall(code))

    tp, fp = [], []
    for p, doc, code in per:
        own = set(IDENT.findall(code))
        for w in set(IDENT.findall(doc)):
            if w in STOP or w in own or w in code_ids:
                continue
            if w not in us:
                continue
            sites = us[w]
            n_cov = sum(1 for (f, ln) in sites if ln in hit.get(f, ()))
            rel = os.path.relpath(p, ROOT)
            (tp if n_cov == 0 else fp).append((w, rel, n_cov, len(sites)))
    tp.sort()
    print('=== 候选：docstring 提到、使用点全未执行 ===')
    if not tp:
        print('  （无）')
    for w, f, _c, n in tp:
        print('  %-28s %-46s 使用点 %d 处，全未执行' % (w, f, n))
    print()
    print('候选 %d 个；已被证伪（使用点确实执行过）%d 个' % (len(tp), len(fp)))
    print()
    print('★ 候选不等于结论。必须逐个核实是不是上面那三种假阳性。')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else 'tools/_cov.json'))
