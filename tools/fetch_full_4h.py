# -*- coding: utf-8 -*-
"""把 2017 起的 4h 历史补进临时库，再跑一次带 Guard 的回放。

为什么要补（第 121 轮）：
  backtest.db 只有 2019-12-28 起的 14999 根 4h。
  而 HANDOFF 六点五十四 测出的真实回撤 75.53% 来自
  「2017-12-17 峰值 413U -> 2019-02-14 谷底 101U，历时 424 天」——
  **整段都在 backtest.db 覆盖范围之外**。

  所以「带 Guard 跑到 46.15% 没触发硬停」这个结论，
  只在 2019-12-28 之后成立。用它来断言「资金到位后不会停」是越界的。
  要回答"硬停会不会触发"，必须把 2017-2019 那段数据补上。
"""
import json
import os
import sqlite3
import sys
import urllib.request

OUT = "/tmp/bt_full.db"
PROXY = os.environ.get("AI_HTTP_PROXY") or ""
opener = (urllib.request.build_opener(
    urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))
    if PROXY else urllib.request.build_opener())


def fetch_all():
    rows, end = [], None
    for _ in range(20):
        q = "symbol=BTCUSDT&interval=4h&limit=1000"
        if end:
            q += "&endTime=%d" % end
        req = urllib.request.Request(
            "https://api.binance.com/api/v3/klines?" + q,
            headers={"User-Agent": "guard-on-history/1.0"})
        with opener.open(req, timeout=45) as r:
            page = json.loads(r.read())
        if not page:
            break
        rows = page + rows
        end = int(page[0][0]) - 1
        if len(page) < 1000:
            break
    return rows


rows = fetch_all()
print("抓取 4h K：%d 根" % len(rows))
c = sqlite3.connect(OUT)
c.execute("drop table if exists klines")
c.execute("create table klines (interval text, open_time integer, o real, "
          "h real, l real, c real, v real)")
c.executemany(
    "insert into klines values (?,?,?,?,?,?,?)",
    [("4h", int(k[0]), float(k[1]), float(k[2]), float(k[3]),
      float(k[4]), float(k[5])) for k in rows])
c.commit()
c.close()
import time
t0 = time.strftime("%Y-%m-%d", time.gmtime(int(rows[0][0]) / 1000))
t1 = time.strftime("%Y-%m-%d", time.gmtime(int(rows[-1][0]) / 1000))
print("区间：%s -> %s（写入 %s）" % (t0, t1, OUT))