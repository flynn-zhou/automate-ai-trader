import json, sqlite3, urllib.request
c = sqlite3.connect('/home/cryptobot/ai-trader/data/market_trade.db')
print('journal =', c.execute('SELECT COUNT(*) FROM journal').fetchone()[0])
print('最新    :', c.execute('SELECT substr(title,1,70) FROM journal ORDER BY id DESC LIMIT 1').fetchone()[0])
tok = ''
for ln in open('/etc/ai-trader/env'):
    if ln.startswith('AI_WEB_TOKEN='):
        tok = ln.strip().split('=', 1)[1]
s = json.load(urllib.request.urlopen('http://127.0.0.1:8090/api/trade/state?token=' + tok, timeout=15))
t = s.get('trend') or {}; w = t.get('wait') or {}; g = s.get('guard') or {}
print('mode=%s price=%s sma=%.2f trigger=%.2f need=%.2f%% hyst=%s'
      % (s.get('mode'), t.get('price'), t.get('sma') or 0, t.get('trigger') or 0,
         t.get('need_pct') or 0, t.get('hyst_pct')))
print('wait=%s天  can_open=%s  peak=%.4f  pos=%s'
      % (w.get('median_days'), g.get('can_open'), g.get('peak_equity') or 0, s.get('pos')))