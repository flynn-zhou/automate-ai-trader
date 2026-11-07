# -*- coding: utf-8 -*-
"""SQLite 多线程访问的两个约定，集中一处，避免每个 store 各写一套。

背景
----
本项目所有 sqlite 连接都以 check_same_thread=False 创建（store.py、trade_db.py
的既有约定），代价是 sqlite3 对象本身不线程安全。而 Web 层用 waitress 多线程
处理请求，连接往往在主线程建好、被工作线程复用。

漏掉同步时的线上症状：
    sqlite3.ProgrammingError: SQLite objects created in a thread can only be
    used in that same thread.

**单元测试用 Flask test_client 是同线程的，永远看不到这个错误**——
这个 bug 只在真实服务器上出现，所以修复必须以线上验证为准，不能以测试通过为准。
"""
from __future__ import annotations

import functools


def open_conn(sqlite3_mod, path: str, **pragmas):
    """建一个可跨线程使用的连接（线程安全由调用方的锁保证）。"""
    con = sqlite3_mod.connect(path, check_same_thread=False)
    for k, v in (pragmas or {}).items():
        con.execute("PRAGMA %s=%s" % (k, v))
    return con


def synchronized(fn):
    """把方法体整个放进 self._lock。

    要求被装饰的类在 __init__ 里建好 self._lock（threading.RLock）。
    用装饰器而不是手写 with，是为了不改方法体缩进——
    大段缩进调整是改动引入回归的高发点。
    """
    @functools.wraps(fn)
    def wrapper(self, *a, **kw):
        with self._lock:
            return fn(self, *a, **kw)
    return wrapper
