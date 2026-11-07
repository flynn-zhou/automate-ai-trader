# -*- coding: utf-8 -*-
"""健康自检脚本的措辞守卫（静态）。

背景（2026-11-02 事故 4）：
  数据停滞时脚本既没重启任何东西、也没修复任何东西，却照样打印「已自愈」
  并 exit 0。运维读日志会以为已经好了，实际上行情/决策仍然停滞，
  只是还没到自动重启的计数阈值。

  「说出了什么问题」和「做了什么处置」是两件事，不能用一个词糊过去。

这条测试是【静态结构】检查，不是行为测试：
  本地 bash 是 WSL 空壳，跑不了这个 shell 脚本。
  真正的行为验证在服务器上跑：
      cd /home/cryptobot/ai-trader && .venv/bin/python tools/hcfault.py
  它会做时间平移故障注入，验证检测/计数/节流/告警落库/不谎报五件事。

★ 第一版这四条测试全都红了，而脚本是对的——错的是测试：
  我用 re.findall 直接在整份源码上匹配，结果匹配到了【注释里的演示文字】
  （注释里写着「却照样打印『已自愈』并 exit 0」，于是计数多了一个）。
  测试若在注释上通过或失败，就等于什么都没验。
  所以下面统一先剥注释，再断言。
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SH = os.path.join(ROOT, 'deploy', 'healthcheck.sh')


def _code():
    """只保留真正的代码行：去掉整行注释和行尾注释。

    这里不能只按 '#' 切——字符串里也可能有 '#'。本脚本里没有，
    所以按 "行首可选空白 + #" 去掉整行注释，再按行尾 ' #' 去尾巴，
    足够精确，且不会误伤 echo 的中文内容。
    """
    out = []
    with open(SH, encoding='utf-8') as f:
        for line in f:
            s = line.rstrip('\n')
            if s.lstrip().startswith('#'):
                continue
            i = s.find(' #')
            if i > 0:
                s = s[:i]
            out.append(s)
    return '\n'.join(out)


def _raw():
    with open(SH, encoding='utf-8') as f:
        return f.read()


def test_every_restart_site_sets_healed():
    """每一处真正重启/拉起的地方都必须把 HEALED 置 1。

    三处：start 主服务、start 哨兵、停滞满 3 次后的 restart。
    漏一处就会出现「明明重启了却说没重启」或反过来。
    """
    s = _code()
    sites = re.findall(
        r'systemctl\s+(?:start|restart)\s+(ai-trader(?:-sentinel)?)', s)
    assert len(sites) == 3, '应有 3 处拉起/重启，实际 %d：%s' % (len(sites), sites)
    assert s.count('HEALED=1') == 3, (
        '有 3 处拉起/重启，却只有 %d 处 HEALED=1' % s.count('HEALED=1'))


def test_healed_message_is_guarded():
    """「已自愈」这句话只能出现在 HEALED=1 的分支里。"""
    s = _code()
    m = re.search(r'if \[ "\$HEALED" -eq 1 \]; then', s)
    assert m, '找不到 HEALED 判断，措辞可能又变成无条件的了'
    msg = '已自愈（本次确实重启过）'
    assert msg in s, '找不到「已自愈」的真实消息'
    assert s.find(msg) > m.start(), '「已自愈」出现在 HEALED 判断之前'
    assert s.count('已自愈') == 1, (
        '代码里「已自愈」出现 %d 次，只应有一次且受 HEALED 保护'
        % s.count('已自愈'))


def test_stale_message_admits_no_restart():
    """数据停滞分支必须明说「未重启」，不能含糊过去。"""
    s = _code()
    i = s.find('elif [ -n "$DATA_CHK" ]; then')
    assert i > 0, '找不到数据停滞分支'
    seg = s[i:i + 300]
    assert '未重启' in seg, '数据停滞时的消息没有明说「未重启」'
    assert '已自愈' not in seg, '数据停滞分支里出现了「已自愈」'


def test_failure_exit_is_last_resort():
    """只有「服务真的起不来」才返回 1；拉起成功不该被记成 FAILED。

    这是 2026-11-02 事故 3 的教训：每次都 exit 1，
    会让「服务未运行->拉起成功」这种正常自愈在 systemd 里显示 FAILED，
    运维见到红色会以为脚本坏了，真正失败时反而没人信。
    """
    s = _code()
    stmts = re.findall(r'^\s*exit (\d)\s*$', s, re.M)
    assert stmts.count('1') == 1, '只应有 1 处 exit 1，实际 %d：%s' % (
        stmts.count('1'), stmts)
    i = s.find('exit 1')
    assert '需人工介入' in s[max(0, i - 200):i], (
        'exit 1 之前没有「需人工介入」，可能被用在了非致命路径上')


def test_raw_file_mentions_the_incident():
    """注释里要留下这次事故的记录，否则下一个人会把它「修」回去。"""
    s = _raw()
    assert '事故 4' in s, '脚本头部的三次事故记录应继续累积'
    assert 'HEALED' in s
