# -*- coding: utf-8 -*-
import json, urllib.request
d = json.loads(urllib.request.urlopen("http://127.0.0.1:8090/api/trade/state", timeout=10).read().decode())
t = d.get("trend") or {}
print("sma=%s price=%s above=%s dist=%.2f%% need=%.2f%%" % (
    t.get("sma"), t.get("price"), t.get("above"),
    t.get("dist_pct") or 0.0, t.get("need_pct") or 0.0))
g = d.get("guard") or {}
print("guard: can_open=%s peak=%s" % (g.get("can_open"), g.get("peak_equity")))
e = d.get("executor") or {}
print("executor: %s halted=%s" % (e.get("name"), e.get("halted")))
w = t.get("wait") or {}
print("wait: median %s days / %s samples" % (w.get("median_days"), w.get("samples")))
