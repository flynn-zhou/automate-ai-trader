# -*- coding: utf-8 -*-
"""部署把关：写好的 unit 必须真的被安装。

为什么这条测试存在
------------------

`deploy/ai-trader-kbar.service` + `.timer` + `tools/kbar_tick.sh` +
`tools/kbar_freshness.py` 全都写好了、有测试、有注释解释为什么需要它们。
但 `install_server.sh` **一个都没装**。

后果不是报错，是：新服务器上 K 线管道从不运行 →
  · `S5` 三道出场闸永不触发
  · 计划源构不出开仓窗口（窗口依赖区间 `tag`）
  · 看板显示「策略没信号」，和真的没信号一模一样

这属于「写好了但没接线」，和「拆开测了但没装」——同一类。
现有 `test_kbar_freshness.py` 只验 unit **文件内容**，不验它**被安装**，
所以这个缺口一直没人抓到。
"""
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALL = os.path.join(ROOT, "deploy", "install_server.sh")
DEPLOY = os.path.join(ROOT, "deploy")


def _sh():
    with open(INSTALL, encoding="utf-8") as f:
        return f.read()


@pytest.mark.parametrize("unit", [
    "ai-trader.service",
    "ai-trader-snapshot.service",
    "ai-trader-snapshot.timer",
    "ai-trader-kbar.service",
    "ai-trader-kbar.timer",
])
def test_every_documented_unit_is_installed(unit):
    """★★ deploy/ 里有的 unit，安装脚本必须 `cp` 它。

    ★ 为什么不写成「kbar 那几个」：写死了以后，下一个 unit 加进 deploy/
      而忘了装，这条测试仍然绿。这里改成**扫目录**：deploy/ 里每个
      `.service` / `.timer` 都必须出现在安装脚本里。
      例外要显式列在下面，不能默默跳过。
    """
    if unit in _NOT_INSTALLED_BY_DESIGN:
        pytest.skip("%s 由别的安装脚本负责" % unit)
    if unit in _KNOWN_REDUNDANT:
        pytest.skip("%s 已知冗余（由 cron 代替）" % unit)
    assert os.path.exists(os.path.join(DEPLOY, unit)), "unit 文件不存在"
    assert re.search(r"cp\s+\"?\$DEST/deploy/%s" % re.escape(unit),
                     _sh()), (
        "deploy/%s 写好了但 install_server.sh 没装它 —— "
        "新服务器上它永远不会运行" % unit)


#: 这些由专门的安装脚本负责（不是主安装脚本）。
_NOT_INSTALLED_BY_DESIGN = {
    "ai-trader-sentinel.service",     # deploy/install_sentinel.sh
}

#: ★ 已知冗余（**已记录的事实，不是“正常”**）：
#:
#:   `ai-trader-healthcheck.{service,timer}` 写好了但**从来没被安装**。
#:   健康自检实际是 cron 在跑（每 10 分钟），
#:   而 timer 写的是每 5 分钟 —— 两者做同一件事，
#:   只有 cron 生效。
#:
#:   ★ 我不在这里改它：“自检每 5 分钟还是 10 分钟”
#:     是一个线上运营决定，不该顺手改。
#:     但必须记下来 —— 否则下一个人会以为 timer 在跑。
_KNOWN_REDUNDANT = {
    "ai-trader-healthcheck.service",  # cron 代替，见 install_server.sh
    "ai-trader-healthcheck.timer",
}


def test_all_units_on_disk_are_accounted_for():
    """反向检查：deploy/ 里不许有既没被装、也没被登记的 unit。"""
    on_disk = {n for n in os.listdir(DEPLOY)
               if n.endswith(".service") or n.endswith(".timer")}
    accounted = set(_NOT_INSTALLED_BY_DESIGN) | set(_KNOWN_REDUNDANT)
    for n in on_disk:
        if re.search(r"cp\s+\"?\$DEST/deploy/%s" % re.escape(n), _sh()):
            accounted.add(n)
    missing = on_disk - accounted
    assert not missing, (
        "这些 unit 既没被 install_server.sh 装、也没登记为例外：%r" % (missing,))


def test_kbar_timer_is_enabled_not_just_copied():
    """只 `cp` 不 `enable --now` 等于装了个不会跑的定时器。"""
    s = _sh()
    assert "systemctl enable --now ai-trader-kbar.timer" in s, (
        "kbar timer 只被拷贝、没被启用 —— 它永远不会跑")


def test_kbar_tick_is_executable():
    """`kbar_tick.sh` 必须被 chmod +x，否则 oneshot 直接失败。"""
    assert re.search(r"chmod \+x\s+\"?\$DEST/tools/kbar_tick\.sh", _sh()), (
        "kbar_tick.sh 没被赋可执行权限")
    assert os.path.exists(os.path.join(ROOT, "tools", "kbar_tick.sh"))


def test_kbar_freshness_watchdog_is_in_the_tick():
    """管道跑完必须过 freshness 哨兵 —— 否则停了也没人知道。

    历史事实：2026-11-10 ~ 11-16，`kbar.db` **静默停摆 6.5 天**，
    期间页面照常渲染。
    """
    tick = os.path.join(ROOT, "tools", "kbar_tick.sh")
    with open(tick, encoding="utf-8") as f:
        body = f.read()
    assert "kbar_freshness.py" in body, "kbar_tick.sh 没有过 freshness 哨兵"
