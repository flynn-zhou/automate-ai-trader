# -*- coding: utf-8 -*-
"""救命配置必须被钉住：进程崩了、卡了、被误停，系统要能自己回来。

为什么这些断言值得存在（2026-11-02 实测）：
    04:24:13 手动 stop      -> 04:24:29 healthcheck 拉起成功（16 秒）
    04:25:20 kill -9 主进程  -> 04:25:26 systemd Restart=always 拉起（6 秒）

这两条自愈路径全靠三个配置文件里的几行：
    ai-trader.service           Restart=always / RestartSec=5
    ai-trader-healthcheck.timer 每 5 分钟跑一次、开机 2 分钟后开始
    healthcheck.sh              检出未运行 -> reset-failed -> start

它们一旦被改掉（比如把 Restart 改成 on-failure 之外的值、或把 timer 停用），
系统就从"能自愈"退化成"挂了没人管"，而任何运行期指标都看不出来——
服务照样 active、看板照样有数，只是再也不会自己回来。
所以必须在仓库里钉死，而不是只写在运维文档里。

★ 本文件断言的是【配置结构】，不是【自愈能力本身】。
  配置在 ≠ 真能救回来（脚本可以写了但没人调用它 —— 2026-11-01 事故 2 就是这样，
  脚本一直只是手动执行的东西）。自愈能力的权威证据是生产实测：
      04:24:13 手动 stop   -> 04:24:29 拉起成功（16s）
      04:25:20 kill -9     -> 04:25:26 恢复（6s，NRestarts=1）
  本文件的职责只有一个：防止某次改动把这些配置悄悄删掉或改弱。
"""
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEPLOY = os.path.join(ROOT, "deploy")


def _read(name):
    p = os.path.join(DEPLOY, name)
    assert os.path.exists(p), u"deploy/%s 不存在" % name
    with open(p, encoding="utf-8") as f:
        return f.read()


def _kv(text, key):
    """取 systemd 配置项的值（最后一次出现）。"""
    vals = re.findall(r"^\s*%s\s*=\s*(.+?)\s*$" % re.escape(key), text, re.M)
    return vals[-1] if vals else None


def test_service_auto_restarts_on_crash():
    """崩溃自愈：Restart=always。交给 on-failure 之类会漏掉"正常退出"。"""
    u = _read("ai-trader.service")
    assert _kv(u, "Restart") == "always", (
        u"ai-trader.service 的 Restart 不是 always，崩溃后不会自愈")
    sec = _kv(u, "RestartSec")
    assert sec is not None, u"没有 RestartSec，默认 100ms 会造成重启风暴"
    m = re.match(r"(\d+)", sec)
    assert m and 1 <= int(m.group(1)) <= 60, (
        u"RestartSec=%s 不合理（实测 5s 合适：够冷却、又不至于宕机太久）" % sec)


def test_service_uses_env_file_and_the_real_entrypoint():
    u = _read("ai-trader.service")
    assert "/etc/ai-trader/env" in u, u"没加载 EnvironmentFile，配置会退回默认值"
    assert "-m ai_trader.main" in u, u"ExecStart 不是主入口"


def test_healthcheck_timer_is_periodic_and_enableable():
    """卡死自愈：必须有一个定期跑的 timer，且能被 systemctl enable。"""
    t = _read("ai-trader-healthcheck.timer")
    assert _kv(t, "Unit") == "ai-trader-healthcheck.service", (
        u"timer 没指向 healthcheck.service")
    period = _kv(t, "OnUnitActiveSec")
    assert period is not None, u"timer 没有 OnUnitActiveSec，不会周期触发"
    m = re.match(r"(\d+)\s*(min|s)", period)
    assert m, u"无法解析周期 %r" % period
    n, unit = int(m.group(1)), m.group(2)
    mins = n if unit == "min" else n / 60.0
    assert mins <= 10, u"周期 %s 太长：宕机后要等这么久才被发现" % period
    assert _kv(t, "OnBootSec") is not None, (
        u"没有 OnBootSec：重启后要等满一个周期才开始巡检")
    assert "WantedBy=timers.target" in t, (
        u"没有 [Install] WantedBy=timers.target，systemctl enable 不生效，开机不会跑")


def test_healthcheck_can_escape_crash_loop():
    """Restart=always 撞上 StartLimitBurst 后 systemd 会永久放弃，
    必须 reset-failed 才能重新拉起 —— 少了它，"自动重启"在崩溃循环面前失效。"""
    h = _read("healthcheck.sh")
    assert "reset-failed" in h, (
        u"healthcheck.sh 缺 reset-failed：崩溃到阈值后 systemd 会放弃，系统再也起不来")
    # ★ 这里必须锚定行尾/空白，不能用 \b。
    # "ai-trader" 后面的 "-"（ai-trader-sentinel）也是词边界，
    # 用 \b 会匹配到哨兵那一行——变异测试实测：把主服务的 start 删掉，
    # 断言照样通过。测试通过得不对，比测试没通过更危险。
    assert re.search(r"systemctl\s+start\s+ai-trader(?:\s|$)", h, re.M), (
        u"healthcheck.sh 检出未运行却没有 start ai-trader 动作")
    # ★ 断言必须带"!"守卫语义，不能只查 is-active 是否出现。
    # healthcheck.sh 里有两处对主服务的 is-active：
    #   第 31 行  if ! systemctl is-active ... ai-trader; then   <- 真正的守卫（触发拉起）
    #   第 39 行  if systemctl is-active ... ai-trader; then     <- 拉起后的复核
    # 只查"出现"的话，把守卫删掉、留下复核行，断言照样通过。
    # 变异测试实测:把守卫改成 if false 仍绿。所以必须匹配 "!"。
    assert re.search(
        r"if\s*!\s*systemctl\s+is-active\s+(?:--quiet\s+)?ai-trader(?:\s|$)", h, re.M), (
        u"healthcheck.sh 没有「服务未运行 -> 拉起」的守卫（if ! is-active ai-trader）")


def test_production_runs_exactly_one_healthcheck_script():
    """仓库里曾有 healthcheck_new.sh / healthcheck_v2.sh 多个变体。

    多个变体的真实风险是：改了一份、装的是另一份，
    而且"测过了"和"装的是哪份"从此无关。生产必须只认一个。
    """
    t = _read("ai-trader-healthcheck.service")
    m = re.search(r"ExecStart=.*?(\S+\.sh)", t)
    assert m, u"healthcheck.service 的 ExecStart 里没有脚本"
    used = os.path.basename(m.group(1))
    assert used == "healthcheck.sh", (
        u"生产跑的是 %s，不是 healthcheck.sh —— 与测试/文档不一致" % used)

    # 同目录下其它 healthcheck*.sh 若存在，必须没有被任何 unit 引用
    scripts = [f for f in os.listdir(DEPLOY)
               if f.startswith("healthcheck") and f.endswith(".sh")]
    units = "".join(_read(u) for u in os.listdir(DEPLOY) if u.endswith((".service", ".timer")))
    for s in scripts:
        if s == used:
            continue
        assert s not in units, (
            u"变体 %s 被 unit 引用了，生产到底跑哪一份又说不清了" % s)
