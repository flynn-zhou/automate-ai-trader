# -*- coding: utf-8 -*-
"""页头不许在没有故障的时候喊故障。

2026-11-02 实测（第一次真的打开大屏看）：
    源 = WS、tick 在涨、ws_idle_ms = 1000、一切正常，
    页头却显示「WS 静默 1.0s」。

「静默」读起来就是「行情断了」。一个常驻的假告警比没有告警更糟：
它会训练用户无视这一格，真断的时候反而没人看。
（第 86 轮修引擎报错条时是同一个道理的反面：那次是真故障没人显示。）

真正的告警信号是快照里的 ws_silent —— feed.py:344，
阈值 max(60s, ws_stale_ms)，也就是说 60 秒没消息才叫静默。
"""
import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(ROOT, 'tests', 'board_harness.js')


def _base():
    return {
        'mode': 'paper', 'equity': 71.7703, 'pos': None,
        'risk': {'day_pnl': 0.0, 'consecutive_losses': 0},
        'executor': {'halted': False, 'reason': ''},
        'guard': {'can_open': True, 'block_reason': '', 'drawdown_pct': 0.0,
                  'rules': {'max_drawdown_pct': 60}},
        'stats': {'trades': 0, 'win_rate': None, 'pnl_net': 0.0,
                  'decisions': 1, 'strategy': 'trend', 'by_strategy': {}},
        'block_stats': {}, 'last_error': '', 'last_error_ts': 0,
        'trend': {'mode': 'trend', 'ready': True, 'bars': 419, 'lookback': 200,
                  'sma': 82254.2685, 'price': 77848.2, 'above': False,
                  'dist_pct': -5.36, 'need_pct': 5.87, 'next_bar_h': 3.2,
                  'hold': False, 'hyst_pct': 0.2, 'trigger': 82418.78,
                  'roll_bars': None, 'roll_days': None, 'roll_never': True,
                  'plan_notional': 70.84, 'plan_qty': 0.00091, 'stop_pct': 25.0,
                  'target_leverage': 1.0, 'exec_market': 'spot', 'wait': {}},
    }


def _write(obj):
    fd, p = tempfile.mkstemp(suffix='.json')
    os.close(fd)
    with open(p, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False)
    return p


def _render(feed_state):
    """行情源快照必须走 WS 推给页头——applyState 只由 WS 触发。"""
    p_trade = _write(_base())
    p_feed = _write(feed_state)
    try:
        r = subprocess.run(['node', HARNESS, p_trade, p_feed],
                           capture_output=True, cwd=ROOT)
        out = json.loads(r.stdout.decode('utf-8'))
    finally:
        os.unlink(p_trade)
        os.unlink(p_feed)
    return out


def _chip(state):
    rows = [x for x in _render(state) if x['id'] == 'wsChip']
    return rows[0] if rows else None


def test_healthy_ws_does_not_say_silent():
    """健康时必须说「正常」。这是本轮修的核心。"""
    chip = _chip({'src': 'WS', 'ws_idle_ms': 1000, 'ws_silent': False,
                  'rest_latency_ms': 0, 'ticks_total': 621, 'uptime_s': 70})
    assert chip is not None, '页头没有渲染 WS 状态格'
    html = chip.get('v') or ''
    assert '静默' not in html, (
        '行情源健康（ws_silent=false）却显示「静默」，读起来像断了：%r' % html)
    assert '正常' in html, '应明确说「WS 正常」：%r' % html


def test_real_silence_does_say_silent_and_warns():
    """真静默（ws_silent=true）时必须说「静默」并带上警示样式。"""
    chip = _chip({'src': 'REST', 'ws_idle_ms': 325740, 'ws_silent': True,
                  'rest_latency_ms': 120, 'ticks_total': 40, 'uptime_s': 326})
    assert chip is not None
    html = chip.get('v') or ''
    assert '静默' in html, '真的断了却不说静默：%r' % html
    assert 'warn' in (chip.get('c') or ''), (
        '静默时没有警示样式（className=%r），用户扫一眼看不出来'
        % (chip.get('c'),))


def test_idle_seconds_still_shown():
    """措辞改了，但那个秒数必须还在——它是排查时的唯一证据。"""
    chip = _chip({'src': 'WS', 'ws_idle_ms': 1500, 'ws_silent': False,
                  'rest_latency_ms': 0, 'ticks_total': 1, 'uptime_s': 10})
    assert '1.5s' in (chip.get('v') or ''), (
        '空闲秒数丢了：%r' % (chip.get('v'),))
