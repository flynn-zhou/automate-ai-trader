import io, json, os, sys
d = json.load(io.open('_cov.json', encoding='utf-8'))
out = []
out.append('type=%s' % type(d))
out.append('keys=%s' % (list(d.keys())[:6] if isinstance(d, dict) else len(d)))
io.open('tools/_out.txt','w',encoding='utf-8').write(chr(10).join(out))