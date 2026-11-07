import json, re, urllib.request
js = urllib.request.urlopen("http://127.0.0.1:8090/static/board.js", timeout=15).read().decode()
print("线上 board.js 字节数:", len(js))
print("含 by_strategy:", "by_strategy" in js)
print("含 该策略尚无成交:", "该策略尚无成交" in js)
i = js.find("function renderStats")
print("--- renderStats 片段 ---")
print(js[i:i+520] if i > 0 else "NOT FOUND")
