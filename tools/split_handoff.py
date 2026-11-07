
# -*- coding: utf-8 -*-
"""把 HANDOFF.md 按阶段拆成 docs/handoff/ 下的分文件。

为什么要有这个脚本：拆分是一次性动作，但"没丢行"这件事必须可复现地验证。
把验证逻辑留在脚本里，下次谁想核对（或想把新加的轮次也拆进去），
重跑一遍就能看到 1..N 行是否无缝覆盖，而不是靠肉眼。

三条不变量：
  1. 内容一字不改 —— 逐行切片，不重写、不总结、不重排。
  2. 不丢行 —— 所有分文件正文行数之和必须等于原文件行数。
  3. 保留轮次编号 —— docs/README.md 写明这是"按轮次编号"的考古日志，
     文件名丢掉轮次号就等于把人挖出来的路径又埋回去。

用法：
    python tools/split_handoff.py            # 干跑，只校验不写文件
    python tools/split_handoff.py --apply    # 真的写 docs/handoff/
"""
from __future__ import annotations

import io
import os
import sys

SRC = "HANDOFF.md"
OUT = os.path.join("docs", "handoff")

# (起, 止, 文件名, 一句话说明) —— 行号含首含尾
PLAN = [
    (1,   87,  "00-系统现状.md",
     "活文档：这是什么、跑在哪、代码地图、当前状态、下一步、当前参数"),
    (88,  194, "r132-收割模型证伪.md",
     "收割模型：哪些被证实、哪些被证伪、我把 beta 当 alpha 那一关"),
    (195, 372, "r131-唐奇安线上翻车.md",
     "唐奇安上线三次翻车与修法（AI_EXEC_MARKET / KeyError / 体检系统撒谎）"),
    (373, 552, "r130-唐奇安上线与剥头皮证伪.md",
     "1h 唐奇安上线五道关 + 剥头皮八个阶段全部证伪"),
    (553, 593, "r003-策略推翻重建.md",
     "策略推翻重建的起因与核心洞察"),
    (594, 635, "r034-现货方案.md",
     "资金费数据推翻永续，改为现货"),
    (636, 668, "r035-实盘就绪.md",
     "实盘模式就绪：三条不可妥协的保护与切换步骤"),
    (669, 683, "r004-值守架构.md",
     "无人值守三层架构"),
    (684, 758, "r005-事故记录.md",
     "2026-11-01 事故记录"),
    (759, 790, "r023-静默监控.md",
     "唯一会静默失效的监控，以及用真实故障数据验证它"),
    (791, 946, "r129-复位鉴权.md",
     "复位按钮没有鉴权，而它能把所有风控闸清空"),
    (947, 1066,"r128-状态双副本.md",
     "同一份状态两份副本，只落了其中一份"),
]


def main(argv):
    apply = "--apply" in argv
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src_path = os.path.join(root, SRC)
    raw = io.open(src_path, encoding="utf-8").read()
    src_lines = raw.splitlines(True)
    total = len(src_lines)
    print("源文件 %d 行" % total)

    covered = []
    for a, b, name, _ in PLAN:
        assert 1 <= a <= b <= total, "范围越界 %s" % name
        covered.append((a, b))
    covered.sort()
    assert covered[0][0] == 1, "没有从第 1 行开始"
    for i in range(1, len(covered)):
        assert covered[i][0] == covered[i - 1][1] + 1, \
            "切片不连续：%s 之后是 %s" % (covered[i - 1], covered[i])
    assert covered[-1][1] == total, "没有覆盖到最后一行 %d" % total
    print("切片覆盖检查：无缝、无重叠、覆盖 1..%d" % total)

    sum_body = sum(b - a + 1 for a, b, _, _ in PLAN)
    assert sum_body == total, "丢行了！%d != %d" % (sum_body, total)
    print("行数守恒：%d == %d" % (sum_body, total))

    if not apply:
        print("干跑结束（未写文件）。加 --apply 才真的落盘。")
        return 0

    out_dir = os.path.join(root, OUT)
    os.makedirs(out_dir, exist_ok=True)
    for a, b, name, _ in PLAN:
        body = "".join(src_lines[a - 1:b])
        head = (
            "<!-- 从 HANDOFF.md 第 %d-%d 行拆分而来，正文一字未改。\n"
            "     拆分脚本：tools/split_handoff.py（可重跑校验）。 -->\n\n"
            % (a, b)
        )
        io.open(os.path.join(out_dir, name), "w",
                encoding="utf-8", newline="\n").write(head + body)
        print("  写入 %-42s %4d 行" % (name, b - a + 1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
