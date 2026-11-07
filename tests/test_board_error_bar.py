# -*- coding: utf-8 -*-
"""引擎报错必须出现在大屏上，而且要能区分「正在坏」和「早就好了」。

为什么补这个（2026-11-02）：
    engine.py 把错误写进 last_error（主循环异常 engine.py:337、实盘对账失败 engine.py:158），
    也通过 /api/trade/state 暴露出来了——但 board.js 从来没有读过它，
    board.html 里也【没有任何错误显示元素】。

    于是最糟的一种情况在用户眼里和正常完全一样：
    引擎每 3 秒抛一次异常、策略停止评估、决策不再更新，
    而大屏上行情还在跳、均线还在显示、等待文案还在写「空仓等待」。
    用户会以为系统在安静地等信号。

    这违反本项目已经写下的原则：监控发现了问题却不显示，等于没有监控。
"""
import json
import os
import shutil
import subprocess
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(ROOT, 'tests', 'board_harness.js')
need_node = pytest.mark.skipif(shutil.which('node') is None, reason='需要 node')


def _state(**kw):
    st = {
        'mode': 'paper', 'equity': 100.0, 'pos': None,
        'risk': {'day_pnl': 0.0, 'consecutive_losses': 0},
        'executor': {'halted': False, 'reason': ''},
        'guard': {'can_open': True, 'block_reason': '', 'drawdown_pct': 0.0,
                  'rules': {'max_drawdown_pct': 60}},
        'stats': {'trades': 0, 'win_rate': None, 'pnl_net': 0.0,
                  'decisions': 100, 'strategy': 'trend', 'by_strategy': {}},
        'block_stats': {},
        'last_error': '',
        'last_error_ts': 0,
        'trend': {'mode': 'trend', 'ready': True, 'bars': 419, 'lookback': 200,
                  'sma': 82323.77, 'price': 78091.1, 'above': False,
                  'dist_pct': -5.13, 'need_pct': 5.63, 'next_bar_h': 1.2,
                  'hold': False, 'hyst_pct': 0.2, 'trigger': 82488.42,
                  'roll_bars': None, 'roll_days': None, 'roll_never': True,
                  'plan_notional': 98.99, 'plan_qty': 0.0012, 'stop_pct': 25.0,
                  'target_leverage': 1.0, 'exec_market': 'spot', 'wait': {}},
    }
    st.update(kw)
    return st


def _render(tmp_path, st):
    p = tmp_path / 'state.json'
    p.write_text(json.dumps(st, ensure_ascii=False), encoding='utf-8')
    r = subprocess.run(['node', HARNESS, str(p)], capture_output=True,
                       cwd=ROOT, timeout=120)
    assert r.returncode == 0, r.stderr.decode('utf-8', 'replace')[:800]
    return json.loads(r.stdout.decode('utf-8'))


def _bar(out):
    for x in out:
        if x['id'] == 'errBar':
            return x
    return None


@need_node
def test_error_bar_shows_current_error(tmp_path):
    """正在出错：必须显示错误原文，且不是「历史」样式。"""
    now = int(time.time() * 1000)
    st = _state(last_error='决策步骤异常: division by zero', last_error_ts=now - 5000)
    bar = _bar(_render(tmp_path, st))
    assert bar is not None, '引擎正在报错，大屏却没有任何错误提示'
    html = bar['v']
    assert 'division by zero' in html, '错误原文没显示：' + html[:300]
    assert '引擎报错' in html, '没有标明这是当前错误：' + html[:300]
    assert bar.get('c') == '', ('当前错误不该用「历史」样式，实际 className=%r'
                               % (bar.get('c'),))


@need_node
def test_error_bar_marks_old_error_as_history(tmp_path):
    """很久以前出过错：仍要能看到，但不能和「正在坏」长得一样。"""
    old = int(time.time() * 1000) - 3 * 3600 * 1000      # 3 小时前
    st = _state(last_error='对账失败：timeout', last_error_ts=old)
    bar = _bar(_render(tmp_path, st))
    assert bar is not None, '历史错误被完全藏掉了，事后无从追溯'
    html = bar['v']
    assert '对账失败' in html, '错误原文没显示：' + html[:300]
    assert '历史错误' in html, '没有标明这是历史错误：' + html[:300]
    assert bar.get('c') == 'hist', ('历史错误应当淡化显示（className=hist），'
                                   '实际 className=%r：%s' % (bar.get('c'), html[:300]))
    assert '小时前' in html, '历史错误应说明是多久以前：' + html[:300]


@need_node
def test_error_bar_absent_when_no_error(tmp_path):
    """没出过错：不占位。否则大屏常驻一条空白红条，用户就学会无视它了。"""
    bar = _bar(_render(tmp_path, _state()))
    assert bar is None, '没有错误却渲染了错误条：' + (bar or {}).get('v', '')[:300]


@need_node
def test_error_bar_without_timestamp_does_not_print_nan(tmp_path):
    """老快照可能只有 last_error 没有 _ts：不许印 NaN / undefined。"""
    bar = _bar(_render(tmp_path, _state(last_error='未知时刻的错误',
                                       last_error_ts=0)))
    assert bar is not None, '有错误文本却不显示'
    html = bar['v']
    for bad in ('NaN', 'undefined', 'Infinity'):
        assert bad not in html, '错误条印出了 %s：%s' % (bad, html[:300])
    assert '时间未知' in html, '缺少时间戳时应当明说，而不是留空：' + html[:300]
