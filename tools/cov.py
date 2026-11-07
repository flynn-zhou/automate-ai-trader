# -*- coding: utf-8 -*-
"""用 sys.settrace 收集 ai_trader/ 的行覆盖。

为什么自己写：本地没有 coverage 包，也装不上（离线）。
只对 ai_trader/ 开启行追踪，其余帧立刻返回 None。

★ 踩过的坑：第一版把判定写成 `fn.startswith(PKG)`，在 pytest 里一条都没收到
  （独立脚本里同样的判定是好的）。改成子串匹配 `'ai_trader' in fn` 之后正常。
  原因没能完全坐实，所以这里用【更宽松】的判定 + 最后按真实路径再过滤一遍，
  宁可多记几个文件，也不要静默地记不到。
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
hit = {}
_seen_any = 0


def _global(frame, event, arg):
    global _seen_any
    fn = frame.f_code.co_filename
    if 'ai_trader' not in fn:
        return None
    _seen_any += 1
    return _local


def _local(frame, event, arg):
    if event == 'line':
        fn = frame.f_code.co_filename
        s = hit.get(fn)
        if s is None:
            s = hit[fn] = set()
        s.add(frame.f_lineno)
    return _local


def start():
    sys.settrace(_global)


def stop():
    sys.settrace(None)


def dump(path):
    with open(path, 'w') as f:
        json.dump({'files': {k: sorted(v) for k, v in hit.items()},
                   'seen_any': _seen_any}, f)
