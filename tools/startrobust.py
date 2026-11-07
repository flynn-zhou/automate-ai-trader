# -*- coding: utf-8 -*-
"""稳健性：把起点挪几个位置，结论会不会翻转？

6 年窗口起点 2020-10 正好在牛市启动前。如果策略"跑输买入持有"
只是起点选择的偶然，那么换起点应该翻回来。
"""
import datetime, sqlite3
DB, NL, SP, COST = "data/backtest.db", 200, 25.0, 0.0010
c = sqlite3.connect(DB)
rows = c.execute("SELECT open_time,h,l,c FROM klines WHERE interval='4h' ORDER BY open_time").fetchall()
c.close()
rows = [r for r in rows if r[3] and r[3] > 0]
T = [r[0] for r in rows]; H = [r[1] for r in rows]
L = [r[2] for r in rows]; C = [r[3] for r in rows]
N = len(rows)

def run(i0):
    cash, qty, pos, entry, stop = 1.0, 0.0, False, 0.0, 0.0
    mtm = []
    for i in range(i0, N):
        if i >= NL + 1:
            s = sum(C[i-NL:i])/NL; sp = sum(C[i-1-NL:i-1])/NL
            if not pos:
                if C[i] > s and C[i-1] <= sp:
                    pos = True; entry = C[i]; stop = entry*(1-SP/100.0)
                    f = cash*COST; cash -= f; qty = (cash*(1-COST))/entry; cash -= qty*entry
            else:
                if L[i] <= stop:
                    cash += qty*stop; cash -= qty*stop*COST; qty = 0.0; pos = False
                elif C[i] < s:
                    cash += qty*C[i]; cash -= qty*C[i]*COST; qty = 0.0; pos = False
        mtm.append(cash + qty*C[i])
    return mtm

def mdd(cur):
    pk = cur[0]; w = 0.0
    for v in cur:
        pk = max(pk, v); w = max(w, (pk-v)/pk)
    return w*100

print("%-12s %7s %11s %11s %9s %9s %8s %8s" %
      ("起点", "年数", "策略CAGR", "买入CAGR", "差额", "策略DD", "买入DD", "Calmar"))
print("-"*82)
starts = []
y, seen = None, set()
for i, t in enumerate(T):
    yy = datetime.datetime.utcfromtimestamp(t/1000).year
    m = datetime.datetime.utcfromtimestamp(t/1000).month
    if yy != y:
        y = yy
        for mm in (1, 7):
            j = next((k for k in range(i, N)
                      if datetime.datetime.utcfromtimestamp(T[k]/1000).month >= mm), None)
            if j and j not in seen and N - j > NL + 400:
                seen.add(j); starts.append(j)
wins = 0; tot = 0
for j in starts:
    cur = run(j)
    bh = [C[i]/C[j] for i in range(j, N)]
    yrs = (T[-1]-T[j])/(365.25*86400000)
    if yrs < 1.0:
        continue
    sc_ = (cur[-1]**(1/yrs)-1)*100
    bc_ = (bh[-1]**(1/yrs)-1)*100
    sd, bd = mdd(cur), mdd(bh)
    d = datetime.datetime.utcfromtimestamp(T[j]/1000).strftime("%Y-%m")
    tot += 1
    if sc_ > bc_: wins += 1
    print("%-12s %6.1f %+10.1f%% %+10.1f%% %+8.1f%% %8.1f%% %7.1f%% %8.2f"
          % (d, yrs, sc_, bc_, sc_-bc_, sd, bd, (sc_/sd) if sd else 0))
print()
print("策略在 %d/%d 个起点上跑赢买入持有的 CAGR" % (wins, tot))
