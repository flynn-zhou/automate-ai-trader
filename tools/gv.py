import json, urllib.request
d = json.loads(urllib.request.urlopen("http://127.0.0.1:8090/api/trade/state", timeout=15).read().decode())
g = d.get("guard")
print("state['guard'] =", json.dumps(g, ensure_ascii=False, indent=2))
print()
t = d.get("trend") or {}
print("trend: above=%s hold=%s dist=%s" % (t.get("above"), t.get("hold"), round(t.get("dist_pct",0),2)))
