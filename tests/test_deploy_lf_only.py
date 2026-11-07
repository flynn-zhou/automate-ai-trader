# -*- coding: utf-8 -*-
"""部署脚本必须是 LF 行尾。

2026-11-02 事故 5（真的发生了，不是假想）：
  我用编辑工具改了 deploy/healthcheck.sh，工具按 Windows 习惯把整个文件
  重写成了 CRLF。推上服务器后，那个每 5 分钟跑一次的健康自检直接崩了：

      line 20: set: -: invalid option
      line 30: 命令找不到（$ + 引号 + CR）
      line 54: unexpected EOF while looking for matching )

  也就是说 7x24 的安全网在最该在的时候是断的。

  更糟的是：我的验证工具 tools/hcfault.py 用 Python 文本模式读这个脚本，
  读的时候 CRLF 被自动规范成 LF，于是它跑得通、还报「全部通过」——
  验证工具悄悄修好了它正在验证的东西。这是「验证手段本身会撒谎」的又一例。

所以这里从【字节】层面盯住它：只要是会拿到 Linux 上执行的脚本，
文件里就不许出现 CR。
"""
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 会被 Linux 直接执行的脚本；CRLF 会直接让它们崩掉
SHELL_DIRS = ('deploy', 'tools')


def _shell_files():
    out = []
    for d in SHELL_DIRS:
        base = os.path.join(ROOT, d)
        if not os.path.isdir(base):
            continue
        for name in sorted(os.listdir(base)):
            if name.endswith(('.sh', '.bash')):
                out.append(os.path.join(base, name))
    return out


def test_shell_files_exist():
    files = _shell_files()
    assert files, '没找到任何 shell 脚本，扫描路径可能写错了'


@pytest.mark.parametrize('path', _shell_files(), ids=lambda p: os.path.basename(p))
def test_no_crlf_in_shell_scripts(path):
    """字节级检查：不许有 CR（0x0d）。"""
    with open(path, 'rb') as f:
        data = f.read()
    assert data, '空文件'
    crs = data.count(b'\r')
    assert crs == 0, (
        '{} 里有 {} 个 CR 字节，是 CRLF 行尾。推到 Linux 后 bash 会报'
        ' command not found 和语法错误，整个脚本跑不起来。'.format(
            os.path.basename(path), crs))


def test_healthcheck_starts_with_bash_shebang():
    with open(os.path.join(ROOT, 'deploy', 'healthcheck.sh'), 'rb') as f:
        first = f.readline()
    assert first.strip() == b'#!/bin/bash', (
        'healthcheck.sh 首行必须是 #!/bin/bash，实际: %r' % first)
