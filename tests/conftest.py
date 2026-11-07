# -*- coding: utf-8 -*-
"""覆盖率采集钩子。默认完全不生效 —— 只有 AI_COV=1 时才挂 sys.settrace。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def pytest_configure(config):
    if os.environ.get('AI_COV') != '1':
        return
    import tools.cov as cov
    cov.start()


def pytest_sessionfinish(session, exitstatus):
    if os.environ.get('AI_COV') != '1':
        return
    import tools.cov as cov
    cov.stop()
    out = os.environ.get('AI_COV_OUT', 'tools/_cov.json')
    cov.dump(out)
    sys.stderr.write('[coverage] files=%d seen=%d -> %s'
                     % (len(cov.hit), cov._seen_any, out) + chr(10))
