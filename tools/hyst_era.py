# -*- coding: utf-8 -*-
"""用 trend.py 自己记录的标准来审滞回带。

代码注释里写着，"连续 N 根确认"被否决的理由是：
  聚合看很漂亮，但按三个独立时代拆开就散了，
  「同时改善收益与回撤」的时代数是 0/3。

同一个标准必须用在滞回带上。达不到就不采纳——
否则我就是在用一个自己刚立的标准去否决别人、却放过自己。
"""
import datetime, sqlite3

NL, SP, COST = 200, 25.0, 0.0010
c = sqlite3.connect("data/backtest.db")
rows = c.execute("SELECT open_time,h,l,c FROM klines WHERE interval='4h' ORDER BY open_time").fetchall()
c.close()
rows = [r for r in rows if r[3] and r[3] > 0]
T=[r[0] for r in rows]; H=[r[1] for r in rows]; L=[r[2] for r in rows]; C=[r[3] for r in rows]
N=len(rows); yrs=(T[-1]-T[0])/(365.25*86400000)
dts=[datetime.datetime.utcfromtimestamp(t/1000) for t in T]

def run(h):
    cash,qty,pos,entry,stop=1.0,0.0,False,0.0,0.0
    mtm,tr=[],0; up,dn=1.0+h,1.0-h
    for i in range(N):
        if i>=NL+1:
            s=sum(C[i-NL:i])/NL; sp=sum(C[i-1-NL:i-1])/NL
            if not pos:
                if C[i]>s*up and C[i-1]<=sp*up:
                    pos=True; entry=C[i]; stop=entry*(1-SP/100.0)
                    cash-=cash*COST; qty=(cash*(1-COST))/entry; cash-=qty*entry; tr+=1
            else:
                if L[i]<=stop:
                    cash+=qty*stop; cash-=qty*stop*COST; qty,pos=0.0,False
                elif C[i]<s*dn:
                    cash+=qty*C[i]; cash-=qty*C[i]*COST; qty,pos=0.0,False
        mtm.append(cash+qty*C[i])
    return mtm,tr

def mdd(cur):
    pk,w=cur[0],0.0
    for v in cur:
        pk=max(pk,v); w=max(w,(pk-v)/pk)
    return w*100

ERAS=[("2018-2020",2018,2020),("2021-2023",2021,2023),("2024-2026",2024,2026)]
m0,t0=run(0.0); m2,t2=run(0.002)

print("数据 %d 根，%.2f 年，%s ~ %s" % (N,yrs,dts[0].date(),dts[-1].date()))
print("笔数 h=0: %d   h=0.2%%: %d" % (t0,t2))
print()
print("%-11s %11s %11s %10s %10s" % ("时代","收益 h=0","收益 h=.2%","回撤 h=0","回撤 h=.2%"))
print("-"*58)
both=0; tested=0
for name,y0,y1 in ERAS:
    idx=[i for i,d in enumerate(dts) if y0<=d.year<=y1]
    if len(idx)<NL+10:
        print("%-11s 数据不足" % name); continue
    i0,i1=idx[0],idx[-1]
    r0=(m0[i1]/m0[max(0,i0-1)]-1)*100
    r2=(m2[i1]/m2[max(0,i0-1)]-1)*100
    d0=mdd(m0[i0:i1+1]); d2=mdd(m2[i0:i1+1])
    ok = (r2>r0) and (d2<d0)
    tested+=1
    if ok: both+=1
    print("%-11s %+10.1f%% %+10.1f%% %9.1f%% %9.1f%%  %s"
          % (name,r0,r2,d0,d2,"收益与回撤同时改善" if ok else "未同时改善"))
print("-"*58)
print()
print("  ★「同时改善收益与回撤」的时代数：%d/%d" % (both,tested))
print("  （「连续N根确认」当年的成绩是 0/3，据此被否决）")
print()
if both==tested and tested>0:
    print("  结论：滞回带在每一个时代都同时改善了收益和回撤，")
    print("        达到了当初否决「连续N根确认」的同一把尺子。可以采纳。")
else:
    print("  结论：未达到当初的标准，不采纳，只记录。")
