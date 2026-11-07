# -*- coding: utf-8 -*-
import io, re
P = "tools/journal_round53.py"
lines = io.open(P, encoding="utf-8").read().split("\n")
out, changed = [], 0
for ln in lines:
    m = re.match(r'^(\s*u")(.*)("\,?\)?)$', ln)
    if m:
        head, body, tail = m.group(1), m.group(2), m.group(3)
        if '"' in body:
            buf, toggle = [], True
            for ch in body:
                if ch == '"':
                    buf.append(u"\u300c" if toggle else u"\u300d")
                    toggle = not toggle
                else:
                    buf.append(ch)
            body = "".join(buf)
            changed += 1
        ln = head + body + tail
    out.append(ln)
io.open(P, "w", encoding="utf-8").write("\n".join(out))
print("处理行数:", changed)
