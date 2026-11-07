# -*- coding: utf-8 -*-
"""线上看板报的 SMA200，到底是用合约数据还是现货数据算出来的？

这是策略唯一的输入。算错市场等于策略跑在一个从没回测过的市场上。
做法：分别用合约 4h 和现货 4h 算 SMA200，再和看板/引擎报的值比。
"""
import json
import urllib.request

# 1) 看板报的 SMA200
tok = ''
try:
    for ln in open('/etc/ai-trader/env', encoding='utf-8'):
        if ln.startswith('AI_WEB_TOKEN='):
            tok = ln.split('=', 1)[1].strip()
except Exception:
    pass

def get(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'v'})
    if tok:
        req.add_header('Authorization', 'Bearer ' + tok)
    return json.loads(urllib.request.urlopen(req, timeout=15).read())

board = get('http://127.0.0.1:8090/api/snapshot')
def dig(d, path):
    for k in path:
        if isinstance(d, dict) and k in d:
            d = d[k]
        else:
            return None
    return d
tr = board.get('trend') or {}
sma_board = tr.get('sma') or tr.get('sma200') or dig(tr, ['state', 'sma'])
print('看板 trend 字段:', json.dumps(tr, ensure_ascii=False)[:400])
print('看板 SMA200 =', sma_board)
print()

def klines(base, path, limit=260):
    url = '%s%s?symbol=BTCUSDT&interval=4h&limit=%d' % (base, path, limit)
    rows = json.loads(urllib.request.urlopen(
        urllib.request.Request(url, headers={'User-Agent': 'v'}), timeout=20).read())
    # 去掉未收盘那根（与 seed_from_rest 的规则一致）
    import time
    now = int(time.time() * 1000)
    iv = 14400000
    return [float(x[4]) for x in rows if int(x[0]) + iv <= now]

sp = klines('https://api.binance.com', '/api/v3/klines')
fu = klines('https://fapi.binance.com', '/fapi/v1/klines')
sma_sp = sum(sp[-200:]) / 200
sma_fu = sum(fu[-200:]) / 200
print('用现货数据算的 SMA200 = %.4f' % sma_sp)
print('用合约数据算的 SMA200 = %.4f' % sma_fu)
print('两者相差 = %.4f（%.4f%%）' % (sma_fu - sma_sp, (sma_fu - sma_sp) / sma_sp * 100))
if sma_board:
    b = float(sma_board)
    print()
    print('看板值 %.4f：离现货 %.4f，离合约 %.4f' % (b, abs(b - sma_sp), abs(b - sma_fu)))
    print('结论:', '合约（正确）' if abs(b - sma_fu) < abs(b - sma_sp) else '现货（仍是旧口径）')
