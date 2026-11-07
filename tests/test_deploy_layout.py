# -*- coding: utf-8 -*-
"""部署布局防线。

这条测试的来由是一次真实事故：scp 多个源文件到同一个目录时，我漏写了
目标子路径，于是 web/app.py / web/dashboard.py / web/live.py 被扔到了
ai_trader/ 根目录，而服务器上 ai_trader/web/ 里还是旧版本。
结果是页面 500、模板变量 Undefined、总览页一片空白 ——
而在本地跑测试全绿，因为本地布局是对的。

所以这里用"目录形状"把它钉死：这几个文件只允许存在于 web/ 下。
"""
from __future__ import annotations

import io
import os

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "ai_trader")

# ws_server.py 是例外：它一直在 ai_trader/ 根，被 main.py 直接 import，
# 不属于 web/ 包。只钉真正属于 web/ 的那几个。
WEB_ONLY = ("app.py", "dashboard.py", "live.py", "auth.py")


def test_web_modules_live_under_web_dir():
    for name in WEB_ONLY:
        root_copy = os.path.join(APP, name)
        web_copy = os.path.join(APP, "web", name)
        assert not os.path.exists(root_copy), (
            "ai_trader/%s 不该存在——它属于 ai_trader/web/。"
            "这正是 scp 目标目录写错造成的部署事故。" % name)
        assert os.path.exists(web_copy), "ai_trader/web/%s 不存在" % name


def test_store_is_not_shadowed_by_range_store():
    """ai_trader/store.py（行情库）和 ai_trader/range/store.py（区间库）是两回事。"""
    top = io.open(os.path.join(APP, "store.py"), encoding="utf-8").read()
    rng = io.open(os.path.join(APP, "range", "store.py"), encoding="utf-8").read()
    assert "def put_bar" in top, "ai_trader/store.py 不像行情库"
    assert "bars_1h" in rng, "ai_trader/range/store.py 不像区间库"
    assert top != rng, "两个 store.py 内容相同，说明有一个被覆盖了"
