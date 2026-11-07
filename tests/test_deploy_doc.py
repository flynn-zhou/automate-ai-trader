# -*- coding: utf-8 -*-
"""部署文档的回归防线。

部署是这个项目代价最高的动作：走错一步会覆盖生产库，或者让服务带着错配置跑。
这份文档里的铁律都来自真实事故，测它们是为了防止有人「顺手简化」。
"""
from __future__ import annotations

import io
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    return io.open(os.path.join(ROOT, *parts), encoding="utf-8").read()


def test_four_hard_rules_present():
    src = _read("docs", "DEPLOY.md")
    # 1 scp 拍平 basename —— 真实事故，app.py 被拍进 ai_trader/ 根目录
    assert "一次只能" in src and "拍平" in src
    assert "test_deploy_layout.py" in src
    # 2 md5 双向：服务 active != 部署成功
    assert "md5" in src.lower()
    assert "不等于部署成功" in src
    # 3 data/ 永不部署
    assert "永远不部署" in src
    # 4 绝不 systemctl stop（会卡 90 秒并掉 SSH）
    assert "systemctl stop" in src
    assert "90" in src


def test_doc_matches_real_server_state():
    """文档必须写实测状态，不能沿用旧说法。"""
    src = _read("docs", "DEPLOY.md")
    # 实测：只有两个单元 active，两个 timer 已 inactive
    assert "ai-trader-snapshot.timer" in src
    assert "inactive" in src
    # 健康检查走 cron 不走 timer
    assert "cron.d/ai-trader" in src
    # 旧文档说四个全 active，这句必须被标注为过期
    assert "过期" in src or "出入" in src


def test_env_file_trap_documented():
    """EnvironmentFile 前缀 - 的陷阱：env 缺失时服务照样起来但跑默认配置。"""
    src = _read("docs", "DEPLOY.md")
    assert "EnvironmentFile" in src
    assert "默认配置" in src or "默认值" in src
    assert "AI_TRADE_MODE" in src


def test_rollback_and_checklist_present():
    src = _read("docs", "DEPLOY.md")
    assert "回滚" in src
    assert "git checkout" in src
    # 核对清单必须包含最易被跳过的两条
    assert "核对清单" in src
    assert "价格在动" in src or "看板页面" in src


def test_infra_points_to_deploy_doc():
    """两份部署说明会漂移，INFRA 必须变成指针。"""
    inf = _read("docs", "INFRA.md")
    assert "docs/DEPLOY.md" in inf
    assert "唯一权威" in inf or "迁移" in inf


def test_index_lists_deploy_doc():
    idx = _read("docs", "README.md")
    assert "docs/DEPLOY.md" in idx