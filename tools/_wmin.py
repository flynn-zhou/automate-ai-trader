# -*- coding: utf-8 -*-
"""直接验 window_min 有没有前视。这是最可能的真凶。

如果 window_min(arr,w)[k] 不等于 min(arr[k-w+1 .. k])，
那"支撑位"就用了未来数据 —— 后面所有正 alpha 都是假的。
"""
import sys, random
sys.path.insert(0, "/home/cryptobot/ai-trader/tools")
import research_limit_neighbor as R

rnd = random.Random(42)
arr = [rnd.uniform(100, 200) for _ in range(500)]
for w in (5, 50):
    got = R.window_min(arr, w)
    bad = []
    for k in range(len(arr)):
        want = min(arr[max(0, k - w + 1):k + 1])
        if abs(got[k] - want) > 1e-9:
            bad.append((k, got[k], want))
    print("w=%-3d 不符 %d 处" % (w, len(bad)))
    for k, g, wn in bad[:5]:
        # 判断是不是"用了未来"
        fut = min(arr[max(0, k - w + 1):k + 1])
        nxt = min(arr[k:min(len(arr), k + w + 1)])
        tag = "用了未来数据!" if abs(g - nxt) < 1e-9 and abs(nxt - wn) > 1e-9 else ""
        print("   k=%-4d 实际 %.6f  应为 %.6f  %s" % (k, g, wn, tag))
