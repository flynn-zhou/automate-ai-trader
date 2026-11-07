# -*- coding: utf-8 -*-
"""把所有中文串里的 ASCII 双引号换成「」。
规则：在 u"..." 行内，第 2 个及之后出现的 " 视为内容引号，
成对替换成「」——按出现顺序交替。
不追求通用，只处理本文件这种"一行一个字符串字面量"的形态。
"""
import io, re
P = "tools/journal_round48.py"   # 只作参考
P = "tools/journal_round50.py"
lines = io.open(P, encoding="utf-8").read().split("\n")
out = []
changed = 0
for ln in lines:
    s = ln
    # 找到形如  u"....." , 的字符串（结尾必须是 " 或 ", 或 ")）
    m = re.match(r'^(\s*u")(.*)("\,?\)?)$', s)
    if m:
        head, body, tail = m.group(1), m.group(2), m.group(3)
        if '"' in body:
            n = body.count('"')
            # 交替替换
            buf = []
            toggle = True
            for ch in body:
                if ch == '"':
                    buf.append(u"\u300c" if toggle else u"\u300d")
                    toggle = not toggle
                else:
                    buf.append(ch)
            body = "".join(buf)
            changed += 1
        s = head + body + tail
    out.append(s)
io.open(P, "w", encoding="utf-8").write("\n".join(out))
print("处理行数:", changed)
