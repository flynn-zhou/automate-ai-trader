# -*- coding: utf-8 -*-
import io, json, re, sys
src = io.open('ai_trader/web/static/board.js', encoding='utf-8').read()
# 抽出所有 X.field 形式的引用，按对象前缀归类
pat = re.compile(r"\b(t|ex|g|r|s|st|w|d)\.([A-Za-z_][A-Za-z0-9_]*)")
refs = {}
for obj, field in pat.findall(src):
    refs.setdefault(obj, set()).add(field)
for k in sorted(refs):
    print('%s.*  (%d): %s' % (k, len(refs[k]), ', '.join(sorted(refs[k]))))
