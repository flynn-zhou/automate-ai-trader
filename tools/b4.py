import json, urllib.request
d = json.loads(urllib.request.urlopen("http://127.0.0.1:8090/api/trade/state", timeout=15).read().decode())
s = d.get("stats", {})
print("stats:", json.dumps({k: s.get(k) for k in ("strategy","trades","win_rate","pnl_net")}, ensure_ascii=False))
print("by_strategy:", json.dumps(s.get("by_strategy"), ensure_ascii=False))
print("trend ready:", (d.get("trend") or {}).get("ready"), " above:", (d.get("trend") or {}).get("above"))
