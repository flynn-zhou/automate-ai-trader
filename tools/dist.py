import sqlite3, json
c = sqlite3.connect("data/market_trade.db")
rows = c.execute("SELECT feature_json FROM decisions WHERE feature_json IS NOT NULL ORDER BY id DESC LIMIT 120").fetchall()
vals = []
for (fj,) in rows:
    try: f = json.loads(fj)
    except Exception: continue
    vals.append((f.get("impulse_pct",0), f.get("impulse_vol_x",0), f.get("atr3s_pct",0)))
if vals:
    imp = sorted(abs(v[0]) for v in vals)
    vx = sorted(v[1] for v in vals)
    atr = sorted(v[2] for v in vals)
    def q(a, p): return a[min(len(a)-1, int(len(a)*p))]
    print("n=", len(vals))
    print("|impulse| p50=%.4f p80=%.4f p90=%.4f p95=%.4f max=%.4f" % (q(imp,.5), q(imp,.8), q(imp,.9), q(imp,.95), imp[-1]))
    print("vol_x    p50=%.2f p80=%.2f p90=%.2f max=%.2f" % (q(vx,.5), q(vx,.8), q(vx,.9), vx[-1]))
    print("atr3s    p50=%.5f p90=%.5f max=%.5f" % (q(atr,.5), q(atr,.9), atr[-1]))
    both = [v for v in vals if abs(v[0])>=0.03 and v[1]>=1.05]
    print("would fire at 0.03%%/1.05x:", len(both), "of", len(vals))
