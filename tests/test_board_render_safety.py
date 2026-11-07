# -*- coding: utf-8 -*-
"""大屏渲染的兜底测试：任何状态下都不许把 undefined / NaN 印给用户。

为什么需要它：
  board.js 是手写的渲染逻辑，有十几个分支（空仓等待 / 滞回带内 / 持仓 /
  安全闸拦截 / 执行器熔断 / 等待预期外推 / 均线永不下来 …），
  而错误表现不是崩溃，是安静地印出 undefined 或 NaN。
  这类错我以前修过两次（trigger、hyst），每次都是碰巧看到。

  这里用 node 起一个最小 DOM 桩，把 board.js 真跑一遍，
  断言渲染出来的每个字都不是 undefined / NaN / Infinity。

状态的键集来自【实测的 /api/trade/state 返回】，不是我编的形状。
"""
import json
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(ROOT, 'tests', 'board_harness.js')


def _base_state():
    return {
        'mode': 'paper',
        'equity': 100.0,
        'pos': None,
        'risk': {'day_pnl': 0.0, 'consecutive_losses': 0},
        'executor': {'halted': False, 'reason': ''},
        'guard': {'can_open': True, 'block_reason': '', 'drawdown_pct': 3.2,
                  'rules': {'max_drawdown_pct': 20}},
        'stats': {'trades': 0, 'win_rate': None, 'pnl_net': 0.0,
                  'decisions': 12354, 'strategy': 'trend', 'by_strategy': {}},
        'block_stats': {},
        'last_error': '',
        'trend': {
            'mode': 'trend', 'ready': True, 'bars': 419, 'lookback': 200,
            'sma': 82323.77, 'price': 78091.1, 'above': False,
            'dist_pct': -5.13, 'need_pct': 5.63, 'next_bar_h': 1.2,
            'hold': False, 'hyst_pct': 0.2, 'trigger': 82488.42,
            'roll_bars': None, 'roll_days': None, 'roll_never': True,
            'plan_notional': 98.99, 'plan_qty': 0.0012, 'stop_pct': 25.0,
            'target_leverage': 1.0, 'exec_market': 'spot',
            'wait': {'median_days': 9.8, 'p90_days': 35.7, 'samples': 333,
                     'bucket': '(-6,-4]', 'extrapolated': None,
                     'source': {'target': 'x', 'hyst_pct': 0.2,
                                'bars': 14999}},
        },
    }


def _flat_below():
    return _base_state()


def _flat_in_band():
    st = _base_state()
    st['trend'].update({'above': True, 'price': 82380.0, 'dist_pct': 0.07,
                        'need_pct': 0.13, 'roll_bars': None,
                        'roll_never': True})
    return st


def _flat_can_roll():
    st = _base_state()
    st['trend'].update({'roll_bars': 200, 'roll_days': 31.5,
                        'roll_never': False})
    return st


def _holding():
    st = _base_state()
    pos = {'side': 'long', 'qty': 0.0012, 'entry': 78000.0,
           'stop': 58500.0, 'entry_ts': 1793500000000}
    st['pos'] = pos
    st['trend'].update({'hold': True, 'above': True, 'price': 82000.0,
                        'pos': pos, 'pos_pnl_pct': 5.13, 'hold_h': 26.4,
                        'roll_bars': None, 'roll_days': None,
                        'roll_never': False})
    return st


def _guard_blocked():
    st = _base_state()
    st['guard'] = {'can_open': False, 'block_reason': '数据源 DOWN',
                   'drawdown_pct': 25.0, 'rules': {'max_drawdown_pct': 20}}
    return st


def _executor_halted():
    st = _base_state()
    st['executor'] = {'halted': True, 'reason': '连续下单失败 5 次'}
    return st


def _wait_extrapolated():
    st = _base_state()
    st['trend']['wait'].update({'extrapolated': True,
                                'calibrated_hyst_pct': 0.2,
                                'requested_hyst_pct': 0.5})
    return st


def _not_ready():
    st = _base_state()
    st['trend'].update({'ready': False, 'bars': 37, 'sma': None,
                        'above': None, 'dist_pct': None, 'need_pct': None,
                        'trigger': None, 'wait': {}})
    return st


def _no_guard():
    st = _base_state()
    st['guard'] = {'can_open': None}
    return st


def _risk_partial():
    """risk 存在但字段不全（比如旧记录或截断的快照）。

    当前后端总是下发 consecutive_losses（engine.py 里由 self.risk 直接取），
    所以这是一个【防御性】状态：它验证的是——万一哪天后端少给一个字段，
    大屏会印成 undefined 还是印成 0。宁可显示 0 也不要显示 undefined，
    因为 undefined 会被当成一个数字读下去。
    """
    st = _base_state()
    st['risk'] = {'day_pnl': 0.0}          # 故意不给 consecutive_losses
    return st


CASES = [
    ('空仓均线下方', _flat_below),
    ('空仓滞回带内', _flat_in_band),
    ('空仓均线会下来', _flat_can_roll),
    ('持仓中', _holding),
    ('安全闸拦截', _guard_blocked),
    ('执行器熔断', _executor_halted),
    ('等待预期外推', _wait_extrapolated),
    ('K线还不够', _not_ready),
    ('安全闸未接入', _no_guard),
    ('risk字段不全', _risk_partial),
]

BAD = ('undefined', 'NaN', 'Infinity')


def _render(tmp_path, st):
    p = tmp_path / 'state.json'
    p.write_text(json.dumps(st, ensure_ascii=False), encoding='utf-8')
    r = subprocess.run(['node', HARNESS, str(p)], capture_output=True,
                       cwd=ROOT, timeout=120)
    assert r.returncode == 0, r.stderr.decode('utf-8', 'replace')[:800]
    return json.loads(r.stdout.decode('utf-8'))


@pytest.mark.skipif(shutil.which('node') is None, reason='需要 node')
@pytest.mark.parametrize('name,make', CASES, ids=[c[0] for c in CASES])
def test_board_never_prints_undefined(name, make, tmp_path):
    out = _render(tmp_path, make())
    assert out, '大屏什么都没渲染出来'
    hits = [x for x in out if any(b in x['v'] for b in BAD)]
    assert not hits, ('状态「%s」下大屏印出了 undefined/NaN：' % name
                      + json.dumps(hits[:3], ensure_ascii=False))


@pytest.mark.skipif(shutil.which('node') is None, reason='需要 node')
def test_board_actually_renders_content(tmp_path):
    for name, make in CASES:
        out = _render(tmp_path, make())
        assert len(out) >= 3, ('状态「%s」只渲染出 %d 个元素'
                               % (name, len(out)))


def _holding_ws_down():
    """持仓中，且 WS 没喂过价（state.price 缺失）。

    这是最要命的组合：卡片其余字段来自 REST，只有浮盈依赖 WS 价格。
    实测过：开仓 78000、现价 82000，本该 +4.8U，旧写法却印出 -93.6U
    ——那正好是整笔名义额，看起来像「全亏光了」。
    """
    return _holding()


@pytest.mark.skipif(shutil.which('node') is None, reason='需要 node')
def test_holding_pnl_is_correct_when_ws_price_missing(tmp_path):
    """浮盈必须用 REST 自带的价格算，不能依赖 WS 的 state.price。"""
    out = _render(tmp_path, _holding_ws_down())
    card = [x for x in out if x['id'] == 'posCard']
    assert card, '持仓卡片没渲染'
    html = card[0]['v']
    assert '-93.6' not in html, ('持仓浮盈用了缺失的 WS 价（按 0 算），'
                                 '印出 -93.6U，等于把整笔名义显示成全亏。'
                                 '卡片：' + html[:400])
    assert '4.800U' in html, ('持仓浮盈应为 4.800U，实际：' + html[:400])
    assert 'pnl-up' in html, ('浮盈方向应为盈利（pnl-up），实际：' + html[:400])


@pytest.mark.skipif(shutil.which('node') is None, reason='需要 node')
def test_holding_card_has_no_missing_fields(tmp_path):
    """目标/已持有 这两个后端不下发的字段，不许印成 NaN / undefined。"""
    out = _render(tmp_path, _holding())
    html = [x for x in out if x['id'] == 'posCard'][0]['v']
    assert 'NaN' not in html, '目标印成了 NaN：' + html[:400]
    assert 'undefined' not in html, '有字段印成 undefined：' + html[:400]
    assert '根 3s' not in html, ('趋势持仓不该用 3s 根数计量：' + html[:400])
    assert ('小时' in html) or ('—' in html), ('已持有既无小时也无占位符：'
                                              + html[:400])

# ---------------- 行情面（页头）----------------
#
# 第 91 轮发现：harness 里的 WebSocket 以前是纯空桩，而页头的 applyState
# 只由 WS 推送触发 —— 也就是说下面这套逻辑此前零覆盖。
# 「健康时喊 WS 静默」正是从这个盲区里漏出去的。
#
# 这里的数据形状来自 feed.snapshot()（feed.py:328），不是编的。

def _feed(**kw):
    """一份最小可用的行情快照，键名照抄 feed.snapshot()。"""
    s = {
        'type': 'state', 'symbol': 'BTCUSDT', 'src': 'WS',
        'price': 77873.0, 'price_ts': 1, 'ws_connected': True,
        'ws_stream': 'trade', 'ws_silent': False,
        'ws_idle_ms': 25, 'ws_reconnects': 0, 'ws_dropped': 0,
        'ws_ticks': 621, 'rest_latency_ms': 3,
        'ticks_total': 621, 'uptime_s': 70,
        'bars': {'3000': {'n': 10, 'o': 77860.0, 'h': 77880.0,
                          'l': 77850.0, 'c': 77873.0, 'v': 1.0}},
    }
    s.update(kw)
    return s


FEED_CASES = [
    ('行情正常', lambda: _feed()),
    ('源是REST降级', lambda: _feed(src='REST', ws_silent=True, ws_idle_ms=325740)),
    ('源全断', lambda: _feed(src='DOWN', price=0.0, ws_connected=False)),
    ('缺ws_idle_ms', lambda: {k: v for k, v in _feed().items() if k != 'ws_idle_ms'}),
    ('缺rest_latency_ms', lambda: {k: v for k, v in _feed().items()
                                   if k != 'rest_latency_ms'}),
    ('缺uptime_s', lambda: {k: v for k, v in _feed().items() if k != 'uptime_s'}),
    ('缺bars', lambda: {k: v for k, v in _feed().items() if k != 'bars'}),
    ('3s根开价是0', lambda: _feed(bars={'3000': {'n': 1, 'o': 0, 'h': 0,
                                             'l': 0, 'c': 0, 'v': 0}})),
    ('缺ws_silent', lambda: {k: v for k, v in _feed().items() if k != 'ws_silent'}),
]


def _render_feed(tmp_path, feed_state):
    p_trade = tmp_path / 'trade.json'
    p_feed = tmp_path / 'feed.json'
    p_trade.write_text(json.dumps(_base_state(), ensure_ascii=False), encoding='utf-8')
    p_feed.write_text(json.dumps(feed_state, ensure_ascii=False), encoding='utf-8')
    r = subprocess.run(['node', HARNESS, str(p_trade), str(p_feed)],
                       capture_output=True, cwd=ROOT, timeout=120)
    assert r.returncode == 0, r.stderr.decode('utf-8', 'replace')[:800]
    return json.loads(r.stdout.decode('utf-8'))


@pytest.mark.skipif(shutil.which('node') is None, reason='需要 node')
@pytest.mark.parametrize('name,make', FEED_CASES,
                         ids=[c[0] for c in FEED_CASES])
def test_header_never_prints_undefined(name, make, tmp_path):
    """行情面任何缺字段都不许把 undefined / NaN / Infinity 印到页头。"""
    out = _render_feed(tmp_path, make())
    hits = [x for x in out if any(b in x['v'] for b in BAD)]
    assert not hits, ('行情状态「%s」下页头印出了 undefined/NaN：' % name
                      + json.dumps(hits[:3], ensure_ascii=False))


@pytest.mark.skipif(shutil.which('node') is None, reason='需要 node')
def test_header_shows_real_numbers_when_healthy(tmp_path):
    """对照：行情正常时页头必须真的有数，不能因为兜底过度而全空。"""
    out = _render_feed(tmp_path, _feed())
    vals = {x['id']: x['v'] for x in out if x['kind'] == 'text'}
    assert vals.get('lastPrice', '').startswith('77873'), (
        '健康行情下现价没印出来：%r' % (vals.get('lastPrice'),))
    assert vals.get('ticks') == '621', 'tick 数没印出来：%r' % (vals.get('ticks'),)
    assert 'ms' in vals.get('restLat', ''), ('REST 延迟没印出来：%r'
                                             % (vals.get('restLat'),))


@pytest.mark.skipif(shutil.which('node') is None, reason='需要 node')
def test_header_survives_feed_without_ws_silent_flag(tmp_path):
    """老版本快照没有 ws_silent 字段时，不能倒过来喊静默。

    缺字段的正确兜底是「按健康处理」——因为真正的告警必须有明确信号，
    不能靠字段缺失来触发（否则升级前线上的老快照会满屏假告警）。
    """
    st = {k: v for k, v in _feed().items() if k != 'ws_silent'}
    out = _render_feed(tmp_path, st)
    chip = [x for x in out if x['id'] == 'wsChip']
    assert chip, '页头没有渲染 WS 状态格'
    assert '静默' not in chip[0]['v'], (
        '缺 ws_silent 字段就喊静默，等于把「不知道」当成「断了」：%r'
        % chip[0]['v'])
