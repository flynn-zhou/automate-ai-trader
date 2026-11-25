# -*- coding: utf-8 -*-
"""体检：变异脚本里的每个锚点，在当前源码里必须**恰好出现 1 次**。

为什么需要这个
--------------
2026-11-19 实测到一次静默污染：变异脚本跑到一半被强杀（
前台 600 秒上限），`finally` 没执行，`pending_plan.py` 里留下了
`family=T.FAMILY_TREND` 这个**变异的替换文本**。

它没有被任何东西报出来 —— 只是下一次跑变异时
`build-family-hardcoded-trend` 显示「锚点匹配 0 次」并**跳过**，
而汇总行写着「跳过 1」，看起来像正常的锚点漂移。
真相是：源码里此刻就是那个变异，四条家族测试全在失败。

所以体检判据是：锚点必须**恰好 1 次**。
  * 0 次 = 源码已被替换文本污染（或者变异定义过期）
  * >=2 次 = 锚点不够唯一，变异会打偏

用法：python tools/check_mut_anchors.py
"""
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import _mut_pending_plan as MM       # noqa: E402


def main():
    bad = 0
    for name, old, new in MM.MUTANTS:
        # 与运行器同一套默认：没登记的文件一律打 pending_plan.py
        path = MM.MUTANT_TARGETS.get(name, MM.TARGET)
        if not os.path.exists(path):
            print("MISSING  %-46s 文件不存在 %s" % (name, path))
            bad += 1
            continue
        text = io.open(path, encoding="utf-8").read()
        n = text.count(old)
        if n != 1:
            # 已经等于替换文本 → 源码就是变异体
            polluted = text.count(new) >= 1
            print("%-8s %-46s 锚点 %d 次%s"
                  % ("污染" if polluted else "异常", name, n,
                     "（源码里已有替换文本！）" if polluted else ""))
            bad += 1
    print("\n共 %d 个变异，异常 %d 个" % (len(MM.MUTANTS), bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
