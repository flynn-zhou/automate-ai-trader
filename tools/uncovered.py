# -*- coding: utf-8 -*-
import ast, json, os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
d = json.load(open('tools/_cov_full.json'))
target = (sys.argv[1] if len(sys.argv) > 1 else 'ai_trader/trading/engine.py').replace('/', os.sep)
full = os.path.join(ROOT, target)
lines = set(d['files'].get(full, []))
assert lines, 'no coverage data for %r (keys like %r)' % (full, list(d['files'])[:2])
src = open(full, encoding='utf-8').read().splitlines()
tree = ast.parse(chr(10).join(src))
SKIP = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Import, ast.ImportFrom)
owner = {}
for node in ast.walk(tree):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        for sub in ast.walk(node):
            if isinstance(sub, ast.stmt) and not isinstance(sub, SKIP):
                owner.setdefault(sub.lineno, node.name)
ex = sorted(owner)
miss = [l for l in ex if l not in lines]
from collections import defaultdict
g = defaultdict(list)
for l in miss:
    g[owner[l]].append(l)
print('%s: %d 可执行行, %d 未覆盖 (%.0f%% 覆盖)'
      % (target, len(ex), len(miss), 100.0 * (len(ex) - len(miss)) / len(ex)))
print()
for fn in sorted(g, key=lambda k: -len(g[k])):
    ls = g[fn]
    print('--- %s (%d 行未覆盖) ---' % (fn, len(ls)))
    runs = []
    s = p = ls[0]
    for x in ls[1:]:
        if x == p + 1:
            p = x
        else:
            runs.append((s, p)); s = p = x
    runs.append((s, p))
    for a, b in runs:
        if b - a <= 3:
            for i in range(a, b + 1):
                print('  %4d| %s' % (i, src[i - 1][:100]))
        else:
            print('  %4d| %s' % (a, src[a - 1][:100]))
            print('      | ...(%d)..' % (b - a - 1))
            print('  %4d| %s' % (b, src[b - 1][:100]))
    print()