# -*- coding: utf-8 -*-
import json, urllib.request
tok = ''
for ln in open('/etc/ai-trader/env', encoding='utf-8'):
    if ln.startswith('AI_WEB_TOKEN='):
        tok = ln.split('=', 1)[1].strip()
print('token 长度:', len(tok))
for path in ('/api/snapshot', '/api/state', '/api/status'):
    try:
        req = urllib.request.Request('http://127.0.0.1:8090' + path, headers={'User-Agent': 'v'})
        if tok:
            req.add_header('Authorization', 'Bearer ' + tok)
        d = json.loads(urllib.request.urlopen(req, timeout=10).read())
        print(path, '-> 顶层键:', list(d.keys())[:20])
        if 'trend' in str(d.keys()):
            print('   trend:', json.dumps(d.get('trend'), ensure_ascii=False)[:500])
    except Exception as e:
        print(path, '-> 失败', e)
