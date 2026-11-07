# -*- coding: utf-8 -*-
import json, urllib.request

for port in (8090, 8091):
    try:
        raw = urllib.request.urlopen("http://127.0.0.1:%d/api/snapshot" % port,
                                     timeout=10).read().decode("utf-8")
    except Exception as e:
        print("port %d 取不到: %s" % (port, e))
        continue
    d = json.loads(raw)
    print("=" * 60)
    print("port %d 顶层键: %s" % (port, list(d.keys())))
    for k in ("trend", "state", "snapshot"):
        if isinstance(d.get(k), dict):
            print("  %s 的键: %s" % (k, list(d[k].keys())[:20]))
    break
