import json, urllib.request, sqlite3
d = json.load(urllib.request.urlopen("http://127.0.0.1:8090/api/trade/state", timeout=20))
t = d["trend"]
print("mode=%s iv=%s ready=%s" % (t.get("mode"), t.get("interval"), t.get("ready")))
print("upper=%s lower=%s price=%s need=%.2f%% dist=%.2f%%"
      % (t.get("upper"), t.get("lower"), t.get("price"),
         t.get("need_pct") or 0, t.get("dist_pct") or 0))
print("plan_qty=%s notional=%s blocked=%s" % (t.get("plan_qty"), t.get("plan_notional"), t.get("plan_blocked")))
print("equity=%s pos=%s" % (d.get("equity"), (d.get("pos") or {}).get("qty")))
g = d.get("guard") or {}
print("guard halted=%s temp=%r drawdown=%.2f%% peak=%.2f day_start=%.2f"
      % (g.get("halted_hard"), g.get("temp_block"), g.get("drawdown_pct") or 0,
         g.get("peak_equity") or 0, g.get("day_start_equity") or 0))
s = d.get("stats") or {}
print("stats strategy=%s decisions=%s trades=%s pnl=%s" % (s.get("strategy"), s.get("decisions"), s.get("trades"), s.get("pnl_net")))
c = sqlite3.connect("/home/cryptobot/ai-trader/data/market_trade.db")
print("db trades=%d decisions=%d" % (c.execute("select count(*) from trades").fetchone()[0],
                                     c.execute("select count(*) from decisions").fetchone()[0]))
print("db balance=", c.execute("select balance from executor_state").fetchone())
c.close()
