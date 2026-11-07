import json, urllib.request
d = json.loads(urllib.request.urlopen("http://127.0.0.1:8090/api/trade/state", timeout=15).read().decode())
print("大屏 stats =", json.dumps(d.get("stats"), ensure_ascii=False, indent=2))
