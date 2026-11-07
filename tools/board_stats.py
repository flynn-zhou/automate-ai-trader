import json, urllib.request
d = json.loads(urllib.request.urlopen("http://127.0.0.1:8090/api/trade/state", timeout=15).read().decode())
def walk(o, p=""):
    if isinstance(o, dict):
        for k, v in o.items():
            if "closed" in k.lower() or "stat" in k.lower() or "win" in k.lower():
                print("  %s%s = %s" % (p, k, json.dumps(v, ensure_ascii=False)[:160]))
            walk(v, p + k + ".")
    elif isinstance(o, list):
        for i, v in enumerate(o[:2]):
            walk(v, p + "[%d]." % i)
print("顶层键:", list(d.keys()))
walk(d)
