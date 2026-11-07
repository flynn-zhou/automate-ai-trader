# -*- coding: utf-8 -*-
"""h=0.2% 的改善，是普遍年份都有，还是只来自少数几年？"""
import datetime, sqlite3

NL, SP, COST = 200, 25.0, 0.0010
c = sqlite3.connect("data/backtest.db")
rows = c.execute("SELECT open_time,h,l,c FROM klines WHERE interval='4h' ORDER BY open_time").fetchall()
c.close()
rows = [r for r in rows if r[3] and r[3] > 0]
T=[r[0] for r in rows]; H=[r[1] for r in rows]; L=[r[2] for r in rows]; C=[r[3] for r in rows]
N=len(rows); yr=[datetime.datetime.utcfromtimestamp(t/1000).year for t in T]
yrs=(T[-1]-T[0])/(365.25*86400000)

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

b={}
for i,y in enumerate(yr):
    b.setdefault(y,[i,i]); b[y][1]=i
years=sorted(b)
def yearly(mtm):
    out,prev={},1.0
    for y in years:
        i0,i1=b[y]; out[y]=(mtm[i1]/prev-1)*100; prev=mtm[i1]
    return out

m0,_=run(0.0); y0=yearly(m0)
m2,t2=run(0.002); y2=yearly(m2)
print("数据 %d 根，%.2f 年" % (N,yrs))
print()
print("%-6s %11s %11s %10s" % ("年份","h=0","h=0.2%","差额"))
print("-"*42)
win=lose=0
for y in years:
    d=y2[y]-y0[y]
    if d>0.5: win+=1
    elif d<-0.5: lose+=1
    print("%-6d %+10.0f%% %+10.0f%% %+9.0f%%" % (y,y0[y],y2[y],d))
print("-"*42)
print("  优于现行 %d 年，劣于 %d 年，基本持平 %d 年" % (win,lose,len(years)-win-lose))
print()
neg0=sum(1 for y in years if y0[y]<0); neg2=sum(1 for y in years if y2[y]<0)
print("  亏损年数：h=0 为 %d 年，h=0.2%% 为 %d 年" % (neg0,neg2))
print()
import statistics
print("  年度收益标准差：h=0 %.1fpp   h=0.2%% %.1fpp"
      % (statistics.pstdev([y0[y] for y in years]), statistics.pstdev([y2[y] for y in years])))
