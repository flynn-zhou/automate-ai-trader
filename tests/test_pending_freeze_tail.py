# -*- coding: utf-8 -*-
"""FREEZE-1：粗周期挂单锚点必须是时间戳，不能是数组下标。

事故（2026-11-26，真钱）
------------------------

`上涨三引线假突破限价单|down|1795656300000|60880.8000` 在 01:57 成交，
到 17:00 仍然是 `state=ARMED`、`filled_ts=0`，而交易所那张单早已 FILLED、
引擎已有一笔 0.01 空单（trade id=36）。生产库现场：

    state=ARMED  filled_ts=0  armed_at_bar=399  next_index=400
    armed_at_sub_ts=NULL      15m 缓冲长度 = 400

根因链（每一环都有代码位置）：

1. 粗粒度 arming 分支只写 `armed_at_bar = i`，**不写** `armed_at_sub_ts`。
2. 成交守卫 `if armed_at_sub_ts is not None: … elif i <= armed_at_bar: continue`
   —— 于是退回**下标**比较。
3. 粗粒度挂单一定发生在「当前最新那根 15m 收盘」，所以 `armed_at_bar`
   恒等于缓冲末尾下标。deque 每来一根新 K 整体左移，
   那个下标**永远**指着队尾 → `i <= armed_at_bar` 恒真 → 每根都被跳过。
4. 成交永远判不出来 → 状态永久停在 ARMED → `_close_position`
   （止损/止盈/保本棘轮/恐慌盘/缩量停滞）一次都进不去。
5. `_manage()` 在 `strategy=pending` 下被 `_step` 提前 return 掉，
   守卫又「只记录不执行」→ **唯一活着的出场只剩交易所地板单**，
   止盈 59487.6 没有任何执行者。

★ 为什么测试必须喂 5m 序列（`entry_bars`）：
  没有 5m 时 `seq = [bar]`，子循环里 `sb is bar` 成立 → 走**子根**分支
  （会设 `armed_at_sub_ts`）；有 5m 时 `seq = subs`（不含 15m 那根），
  子循环永远 `sb is not bar` → 落到**粗粒度**分支，而那条分支
  只写 `armed_at_bar`、不写 `sub_ts`。生产库 `sub_ts` 全 NULL 正是这个原因。
  只喂 15m 的测试会「碰巧通过」而掩盖真正的缺陷。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_trader.trading import pending_thesis_store as PTS   # noqa: E402
from ai_trader.trading import thesis as T                   # noqa: E402

BAR15 = 900_000
BAR5 = 300_000
START = 1795656300000


class _Bar:
    __slots__ = ("ts", "o", "h", "l", "c", "v")

    def __init__(self, ts, o, h, l, c, v=1.0):
        self.ts = int(ts)
        self.o, self.h, self.l, self.c, self.v = o, h, l, c, v

    @property
    def open_time(self):
        return self.ts


def _mk_thesis():
    """三引线空单：向下限价，价格涨到 order_price 才成交。"""
    return T.Thesis(
        thesis_id="上涨三引线假突破限价单|down|1795656300000|60880.8000",
        kind="上涨三引线假突破限价单", direction=T.DOWN,
        order_price=60880.8, stop_price=61822.35, target_price=59487.6,
        order_type=T.ORDER_LIMIT, horizon_bars=12, stale_distance=941.55,
        surprise_pts=500.0, family=T.FAMILY_REVERSAL)


def _make_15m(n, high=60520.0, low=60480.0):
    """n 根 15m，默认远离触发价（不成交、不破功）。"""
    return [_Bar(START + i * BAR15, 60500.0, high, low, 60500.0)
            for i in range(n)]


def _make_5m(n, high=60520.0, low=60480.0):
    """给每根 15m 造 3 根 5m 子 K（合计 3n 根）。"""
    out = []
    for i in range(n):
        for k in range(3):
            ts = START + i * BAR15 + k * BAR5
            out.append(_Bar(ts, 60500.0, high, low, 60500.0))
    return out


def _claim_only_at_last(n):
    """只有第 n-1 根成立 —— 让挂单那根**正好是缓冲末尾**。

    ★ 这是事故的关键几何：`armed_at_bar` == len(bars) - 1。
      第一根就成立的话会在第 0 根挂单，走不到队尾那个场景。
    """
    def _claim(i, bars, book=None):
        return (i == n - 1), {}
    return _claim


def _arm_at_tail(n=40):
    """跑到「刚挂单、且挂单那根是缓冲最后一根」。

    Returns: (thesis, bars15, bars5, res, prog)
    """
    bars = _make_15m(n)
    b5 = _make_5m(n)
    th = _mk_thesis()
    res, prog = T.step(th, bars, None, _claim_only_at_last(n),
                       progress=None, entry_bars=b5)
    return th, bars, b5, res, prog


def _fill_tail(n):
    """造一根价格穿过触发价（60880.8）的 15m + 它的 3 根 5m。"""
    b15 = _Bar(START + n * BAR15, 60600.0, 60950.0, 60550.0, 60700.0)
    b5 = [_Bar(START + n * BAR15 + k * BAR5, 60600.0, 60950.0, 60550.0, 60700.0)
          for k in range(3)]
    return b15, b5


# ------------------------------------------------------------------ 锚点本身


def test_粗周期挂单必须落armed_bar_ts():
    """挂单那一刻的粗周期锚点是时间戳，不是下标。"""
    n = 40
    th, bars, b5, res, prog = _arm_at_tail(n)

    assert res.state == T.ARMED, (
        "前置条件：这批 K 应该走到 ARMED，实际 %s" % (res.state,))
    assert prog["armed_at_sub_ts"] is None, (
        "前置条件：喂了 5m 之后必须落到**粗粒度**分支（这才是生产路径），"
        "实际 sub_ts=%r —— 说明测试几何退回了子根分支"
        % (prog["armed_at_sub_ts"],))
    assert prog["armed_at_bar"] == n - 1, (
        "前置条件：事故几何就是 armed_at_bar 恰好等于缓冲末尾下标，实际 %r"
        % (prog["armed_at_bar"],))
    assert prog["armed_bar_ts"] == START + (n - 1) * BAR15, (
        "粗周期挂单必须把挂单那根 15m 的 open_time 写成 armed_bar_ts，"
        "实际 %r —— 只写下标会让成交守卫永久跳过每一根（事故根因）"
        % (prog["armed_bar_ts"],))


# ------------------------------------------------------------------ 核心回归


def test_挂单根在队尾也能判出成交():
    """★ 核心回归：**定长缓冲左移之后**，新一根 15m 必须能判出成交。

    ★★ 几何必须是「定长滑动」，否则测试是空的：

      生产缓冲是 `deque(maxlen=400)`。挂单那根在队尾（下标 399），
      来新 K 时旧 K 从头部丢弃、新 K 补在队尾 —— 下标**还是 399**。
      于是旧守卫 `i <= armed_at_bar(399)` 对每一根都成立 → 全跳过。
      如果测试改成「追加一根让缓冲变长」（下标 40 > 39），旧守卫
      照样能过 —— 那就等于什么都没测。

    所以：先跑到队尾挂单，再**丢头补尾**保持长度不变，把成交那根
    放在下标 = armed_at_bar 的位置，并强制从头重扫（生产里游标正是
    被清回重算的）。旧代码在这一步必然停在 ARMED。
    """
    n = 40
    th, bars, b5, res, prog = _arm_at_tail(n)
    assert res.state == T.ARMED
    assert prog["armed_at_bar"] == n - 1, "前置条件：挂单根在队尾"

    b15, f5 = _fill_tail(n)
    # ★ 定长滑动：丢头一根、补尾一根 → 长度仍是 n，成交那根落在下标 n-1
    slid15 = bars[1:] + [b15]
    slid5 = b5[3:] + f5
    assert len(slid15) == n, "前置条件：缓冲长度不变（模拟 deque maxlen）"
    assert slid15[-1] is b15, "前置条件：成交那根位于队尾"
    fill_idx = slid15.index(b15)
    assert fill_idx == prog["armed_at_bar"], (
        "前置条件：成交那根的下标必须**恰好等于** armed_at_bar=%r，"
        "这才是事故的死锁几何（实际 %r）"
        % (prog["armed_at_bar"], fill_idx))
    assert int(b15.ts) > int(prog["armed_bar_ts"]), (
        "前置条件：成交那根的时间戳晚于挂单锚点")

    prog2 = dict(prog)
    prog2["next_index"] = 0        # 生产里游标被清回重算 → 从头重扫

    res2, prog2 = T.step(th, slid15, None, _claim_only_at_last(n),
                         progress=prog2, entry_bars=slid5)

    assert res2.state != T.ARMED, (
        "价格已穿过触发价，定长缓冲左移后状态机必须判出成交（或直接走到"
        "出场），实际停在 %s —— 这就是 FREEZE-1 的冻结："
        "`i <= armed_at_bar(%r)` 在 i=%r 上仍为真，于是每根都被跳过"
        % (res2.state, prog["armed_at_bar"], fill_idx))
    assert prog2["state"] != T.ARMED, "落库状态也不许还留在 ARMED"


def test_时间戳锚点对缓冲左移不敏感():
    """把同一批 K 前面削掉几根（模拟 deque 滑动），结论必须不变。

    ★ 下标判据在这条上翻车：削掉前面几根后，同一个 armed_at_bar
      指向的是**另一根** K。时间戳不会。
    """
    n = 40
    th, bars, b5, res, prog = _arm_at_tail(n)
    assert res.state == T.ARMED

    cut = 5
    b15, f5 = _fill_tail(n)
    prog2 = dict(prog)
    prog2["next_index"] = 0        # 强制从头重扫（等价于游标被清回重算）

    res2, _ = T.step(th, bars[cut:] + [b15], None, _claim_only_at_last(n),
                     progress=prog2, entry_bars=b5[cut * 3:] + f5)

    assert res2.state != T.ARMED, (
        "缓冲左移后必须仍然能判出成交，实际 %s —— 说明判据还依赖下标"
        % (res2.state,))


def test_老行回填从place_order取挂单那一根(tmp_path):
    """11-26 之前落库的行没有 armed_bar_ts，必须从 `place_order` action 回填。

    ★ 数据来源必须是 `pending_thesis_actions.bar_ts`（kind='place_order'），
      **不能**用 `pending_theses.bar_ts` —— 后者是「末次推进到的根」，
      会一直往前移。生产实证：活着的 ARMED 行
      `pending_theses.bar_ts=1795688100000`，真实挂单根是
      `1795657200000`，差 5 小时；拿后者当锚点会把 5 小时的历史成交
      全部跳过 —— 换个姿势继续冻结。
    """
    import sqlite3

    path = str(tmp_path / "p.db")
    st = PTS.PendingThesisStore(path)
    try:
        th = _mk_thesis()
        plan_ts = START
        arm_ts = START + BAR15          # 真实的挂单那一根（比计划根晚一根）

        # 老行：armed_at_bar 有值、armed_bar_ts 为 NULL，
        # 并且 bar_ts 已经被推进到很晚（模拟「末次推进时刻」）
        st.upsert_thesis(PTS.row_of(
            th, state=T.ARMED, next_index=400,
            armed_at_bar=399, armed_at_sub_ts=None,
            armed_bar_ts=None, armed_ref_close=60500.0,
            bar_ts=START + 53 * BAR15))
        # place_order action 记录真实挂单根
        st._conn.execute(
            "INSERT INTO pending_thesis_actions"
            " (action_id, thesis_id, kind, state_after, bar_ts, price,"
            "  payload, applied, created_ts)"
            " VALUES (?, ?, 'place_order', 'ARMED', ?, 60880.8, '{}', 1, ?)",
            ("%s:%d:place_order" % (th.thesis_id, arm_ts), th.thesis_id,
             arm_ts, arm_ts))
        st._conn.commit()

        n = st.backfill_armed_bar_ts()
        assert n == 1, "必须回填 1 行，实际 %d" % (n,)

        got = st._conn.execute(
            "SELECT armed_bar_ts FROM pending_theses WHERE thesis_id=?",
            (th.thesis_id,)).fetchone()
        assert got["armed_bar_ts"] == arm_ts, (
            "回填必须取 place_order 的 bar_ts=%d（挂单那一根），"
            "实际 %r —— 用 thesis.bar_ts 会晚 5 小时，历史成交全被跳过"
            % (arm_ts, got["armed_bar_ts"]))
        assert got["armed_bar_ts"] != START + 53 * BAR15, (
            "拿到「末次推进时刻」了 —— 那不是挂单根")

        # 幂等：再跑一次不重复回填、也不改值
        assert st.backfill_armed_bar_ts() == 0, "已回填的行不许再动"
    finally:
        st.close()


def test_回填不动终态行(tmp_path):
    """终态行的锚点已经不影响任何判决，改动历史只会让复盘对不上账。"""
    path = str(tmp_path / "p.db")
    st = PTS.PendingThesisStore(path)
    try:
        th = _mk_thesis()
        st.upsert_thesis(PTS.row_of(
            th, state=T.CLOSED_TP, next_index=400,
            armed_at_bar=399, armed_bar_ts=None, armed_ref_close=60500.0,
            bar_ts=START))
        st._conn.execute(
            "INSERT INTO pending_thesis_actions"
            " (action_id, thesis_id, kind, state_after, bar_ts, price,"
            "  payload, applied, created_ts)"
            " VALUES (?, ?, 'place_order', 'ARMED', ?, 1.0, '{}', 1, ?)",
            ("a1", th.thesis_id, START + BAR15, START))
        st._conn.commit()

        assert st.backfill_armed_bar_ts() == 0, "终态行不许回填"
        got = st._conn.execute(
            "SELECT armed_bar_ts FROM pending_theses WHERE thesis_id=?",
            (th.thesis_id,)).fetchone()
        assert got["armed_bar_ts"] is None
    finally:
        st.close()


def test_打开库就自动回填(tmp_path):
    """★ 回填必须挂在 `__init__` 上，不能靠调用方记得调。

    ★ 这一条是变异测试逼出来的：第一版测试自己显式调
      `st.backfill_armed_bar_ts()`，于是「把 __init__ 里那行删掉」
      这个变异体**存活** —— 服务重启后老行照样冻结，而测试全绿。
      真实路径是「重启服务 → 引擎构造 store」，没人会记得额外调一次。
    """
    import sqlite3

    path = str(tmp_path / "p.db")
    th = _mk_thesis()
    arm_ts = START + BAR15

    # 先用一个 store 把「老行 + place_order」写好
    st0 = PTS.PendingThesisStore(path)
    try:
        st0.upsert_thesis(PTS.row_of(
            th, state=T.ARMED, next_index=400,
            armed_at_bar=399, armed_at_sub_ts=None,
            armed_bar_ts=None, armed_ref_close=60500.0, bar_ts=START))
        st0._conn.execute(
            "INSERT INTO pending_thesis_actions"
            " (action_id, thesis_id, kind, state_after, bar_ts, price,"
            "  payload, applied, created_ts)"
            " VALUES (?, ?, 'place_order', 'ARMED', ?, 1.0, '{}', 1, ?)",
            ("a1", th.thesis_id, arm_ts, START))
        st0._conn.commit()
    finally:
        st0.close()

    # ★ 这里**只构造**，不显式调回填 —— 模拟服务重启
    st1 = PTS.PendingThesisStore(path)
    try:
        got = st1._conn.execute(
            "SELECT armed_bar_ts FROM pending_theses WHERE thesis_id=?",
            (th.thesis_id,)).fetchone()
        assert got["armed_bar_ts"] == arm_ts, (
            "打开库时就必须自动回填（服务重启没人会手动调），实际 %r —— "
            "不回填的话老行重启后又会永久冻结在 ARMED"
            % (got["armed_bar_ts"],))
    finally:
        st1.close()


def test_重挂时取最早那次挂单(tmp_path):
    """挂单可能失败重试，锚点必须是**最早**那次（真正的 arming 时刻）。

    ★ 取最后一次（MAX）会让锚点偏晚 → 两次挂单之间那段的成交被跳过。
    """
    path = str(tmp_path / "p.db")
    st = PTS.PendingThesisStore(path)
    try:
        th = _mk_thesis()
        first, second = START + BAR15, START + 9 * BAR15
        st.upsert_thesis(PTS.row_of(
            th, state=T.ARMED, next_index=400,
            armed_at_bar=399, armed_bar_ts=None, armed_ref_close=60500.0,
            bar_ts=START + 20 * BAR15))
        for i, ts in enumerate((first, second)):
            st._conn.execute(
                "INSERT INTO pending_thesis_actions"
                " (action_id, thesis_id, kind, state_after, bar_ts, price,"
                "  payload, applied, created_ts)"
                " VALUES (?, ?, 'place_order', 'ARMED', ?, 1.0, '{}', 1, ?)",
                ("a%d" % i, th.thesis_id, ts, START))
        st._conn.commit()

        assert st.backfill_armed_bar_ts() == 1
        got = st._conn.execute(
            "SELECT armed_bar_ts FROM pending_theses WHERE thesis_id=?",
            (th.thesis_id,)).fetchone()
        assert got["armed_bar_ts"] == first, (
            "重挂过的预期必须取**最早**那次挂单 %d 当锚点，实际 %r —— "
            "取最后一次会把两次挂单之间那段的成交全部跳过"
            % (first, got["armed_bar_ts"]))
    finally:
        st.close()


def test_回填只认place_order(tmp_path):
    """`close` / `cancel` 的 bar_ts 语义完全不同，绝不能拿来当锚点。

    ★ 它们的时间戳可能**早于**挂单（cancel 在计划那根就写了），
      不过滤 kind 的话 MIN 会挑到它 → 锚点跑到挂单之前，
      守卫于是把「挂单那一桶」也当成交判（孤儿单）。
    """
    path = str(tmp_path / "p.db")
    st = PTS.PendingThesisStore(path)
    try:
        th = _mk_thesis()
        arm_ts = START + BAR15
        st.upsert_thesis(PTS.row_of(
            th, state=T.ARMED, next_index=400,
            armed_at_bar=399, armed_bar_ts=None, armed_ref_close=60500.0,
            bar_ts=START + 20 * BAR15))
        # 一条更早的 close（MIN 会挑到它），一条正确的 place_order
        for aid, kind, ts in (("c1", "close", START),
                              ("p1", "place_order", arm_ts)):
            st._conn.execute(
                "INSERT INTO pending_thesis_actions"
                " (action_id, thesis_id, kind, state_after, bar_ts, price,"
                "  payload, applied, created_ts)"
                " VALUES (?, ?, ?, 'ARMED', ?, 1.0, '{}', 1, ?)",
                (aid, th.thesis_id, kind, ts, START))
        st._conn.commit()

        assert st.backfill_armed_bar_ts() == 1
        got = st._conn.execute(
            "SELECT armed_bar_ts FROM pending_theses WHERE thesis_id=?",
            (th.thesis_id,)).fetchone()
        assert got["armed_bar_ts"] == arm_ts, (
            "锚点必须来自 place_order（%d），实际 %r —— 拿到 close 的 %d "
            "会让挂单那一桶被当成交判，变成孤儿单"
            % (arm_ts, got["armed_bar_ts"], START))
    finally:
        st.close()


def test_本轮平过仓就不许回滚成ARMED(tmp_path):
    """★★ 引擎级回归：`self.pos` 为空 **不能**单独证明「状态机在撒谎」。

    2026-11-26 18:45:17 生产实证（解冻后第一分钟）：
      修复让状态机正确判出成交并真实平仓
      （BUY 0.01 @ 60484.3，净 +3.54U / +0.376R，地板单也撤了），
      而 `_pending_close` 末尾会把 `self.pos` 清空 → 引擎的
      「自相矛盾终态拦截」看到空仓 → 把**合法终态**回滚成 ARMED。

    危害不是「多跑一轮」：仓位没了却回到 ARMED，下一轮会把同一段历史 K
    再判一次成交 —— 空仓上的 BUY 就是**反手开多**。

    ★ 判据必须是「本轮是否真的执行过 close」（`placed` 里有 "close"）。

    ★ 隔离手法：把 `_pending_close` 换成「成功平仓」的替身（只清空 pos）。
      这样守卫拿到的输入与生产那一刻**完全一致**（pos 空 + placed 有
      close），而不需要把交易所/关账/结算整条链拖进测试 ——
      那些环节另有测试，混在一起失败时根本分不清是谁坏了。
    """
    import asyncio
    import types

    from ai_trader.trading import pending_thesis_store as _PTS
    from tests.test_pending_wiring import _engine

    eng, _store = _engine(tmp_path, dry=False)

    price = 61000.0
    th = T.Thesis(
        thesis_id="freeze1|down|%d|%.1f" % (START, price), kind="x",
        direction=T.DOWN, order_price=price, stop_price=price * 1.02,
        target_price=price * 0.996, order_type=T.ORDER_LIMIT,
        family=T.FAMILY_REVERSAL, surprise_pts=0.0, horizon_bars=12)

    # 已经成交持仓：pos 是引擎的仓位事实
    eng.pos = {"side": "short", "qty": 0.01, "entry": price,
               "stop": price * 1.02, "target": price * 0.996,
               "open_reason": "挂单成交", "paper": 0, "fee": 0.0,
               "entry_ts": START, "bars": 0, "lots": []}
    eng.pending.upsert_thesis(_PTS.row_of(
        th, state=T.FILLED, next_index=0, bar_ts=START,
        armed_at_bar=0, armed_bar_ts=START, armed_ref_close=price,
        ambiguous=False, note=""))

    async def _fake_close(act, thesis_obj):
        """模拟「平仓成功」的**唯一**可观察后果：仓位事实被清空。"""
        eng.pos = None

    eng._pending_close = _fake_close

    def _bars(n=6):
        out = []
        for i in range(n):
            t = START + i * BAR15
            # 一路下跌穿过止盈价 → 状态机必须给出 close
            px = price - 60.0 * (i + 1)
            out.append(types.SimpleNamespace(
                open_time=t, ts=t, open=px + 20.0, high=px + 20.0,
                low=px - 20.0, close=px, volume=1.0, interval_ms=BAR15))
        return out

    b15 = _bars()
    b5 = []
    for b in b15:
        for k in range(3):
            t = b.ts + k * BAR5
            b5.append(types.SimpleNamespace(
                open_time=t, ts=t, open=b.open, high=b.high, low=b.low,
                close=b.close, volume=1.0, interval_ms=BAR5))

    asyncio.get_event_loop().run_until_complete(
        eng._step_pending({BAR15: b15, BAR5: b5}, price, 100.0,
                          b15[-1].ts))

    row = dict(eng.pending._conn.execute(
        "SELECT * FROM pending_theses WHERE thesis_id=?",
        (th.thesis_id,)).fetchone())
    assert row["state"] in (T.CLOSED_TP, T.CLOSED_SL, T.CLOSED_THESIS), (
        "本轮已经执行过 close（仓位是被我们自己平掉的），终态必须成立；"
        "实际停在 %r —— 回滚成 ARMED 之后，空仓上再发一次 BUY 就是反手开多"
        % (row["state"],))
    assert eng.pos is None, "前置条件：平仓后仓位应为空"
    assert not [r for r in eng.db.risks
                if r[0] == "pending_terminal_without_position"], (
        "合法终态不该被记成「自相矛盾」风险事件")


def test_粗周期超期用时间戳计数():
    """超期撤单也不许用下标差：下标差在定长缓冲里**恒为 0** → 永不超期。

    ★ 几何必须是定长滑动（同核心回归）：如果让缓冲变长，`i - armed_at_bar`
      照样能到 12，旧逻辑也能过 —— 变异测试实测过「超期_仍用下标差」
      在这一版几何下存活。定长时 i 最大就是 armed_at_bar，差值恒 0。
    """
    n = 40
    th, bars, b5, res, prog = _arm_at_tail(n)
    assert res.state == T.ARMED
    assert prog["armed_at_bar"] == n - 1, "前置条件：挂单根在队尾"

    # 定长滑动：丢 13 根头、补 13 根尾 → 长度仍是 n。
    # 最后一根距挂单锚点 13 根 15m（≥ horizon_bars=12）→ 必须超期。
    # 价格不动（不成交、不破功）。
    shift = 13
    slid15 = bars[shift:] + [
        _Bar(START + (n + k) * BAR15, 60500.0, 60520.0, 60480.0, 60500.0)
        for k in range(shift)]
    slid5 = b5[shift * 3:]
    for k in range(shift):
        ts15 = START + (n + k) * BAR15
        for j in range(3):
            slid5.append(_Bar(ts15 + j * BAR5, 60500.0, 60520.0, 60480.0, 60500.0))
    assert len(slid15) == n, "前置条件：缓冲长度不变"
    assert slid15.index(slid15[-1]) - prog["armed_at_bar"] <= 0, (
        "前置条件：定长缓冲里 (下标 - armed_at_bar) 必须 <= 0，"
        "否则下标差判据也能过，测不到东西")

    prog2 = dict(prog)
    prog2["next_index"] = 0        # 生产里游标被清回重算

    res2, _ = T.step(th, slid15, None, None,
                     progress=prog2, entry_bars=slid5)
    assert res2.state in (T.EXPIRED, T.CANCELLED), (
        "挂钩超过 horizon_bars 根仍未成交必须超期，实际 %s —— "
        "用下标差的话这里恒为 0，永远撤不掉" % (res2.state,))


def test_挂单那一根自己不许判成交():
    """挂单发生在那一根**收盘之后**，它的 h/l 属于过去。

    ★ `<=` 改成 `<` 会让挂单那一桶里**后面的 5m 子根**也被拿 h/l 判成交 ——
      真钱事故形态：本地推出 FILLED，交易所那张单还是 NEW，变成孤儿单。

    ★ 几何必须是「**重放**挂单那一桶」：正常推进时挂单是在子循环**之后**
      才发生的（状态还是 EXPECTED），那时判不到 ARMED 分支。只有生产里
      `next_index` 被清回重算、而 `state=ARMED` 存进 resume 时，
      才会从桶头重扫 —— 变异体正是在这个场景下才现形。
    """
    n = 40
    # 每根 15m 的最高价都穿过触发价（60880.8），所以「跳过判据」一旦失效
    # 就会立刻在挂单那一桶的子根上判出成交。
    bars = _make_15m(n, high=60950.0, low=60550.0)
    b5 = _make_5m(n, high=60950.0, low=60550.0)
    th = _mk_thesis()
    res, prog = T.step(th, bars, None, _claim_only_at_last(n),
                       progress=None, entry_bars=b5)
    assert res.state == T.ARMED, "前置条件：应走到 ARMED，实际 %s" % (res.state,)
    assert prog["armed_bar_ts"] == START + (n - 1) * BAR15, "前置条件：锚点"

    prog2 = dict(prog)
    prog2["next_index"] = 0        # 生产里游标被清回重算 → 从桶头重扫

    res2, _ = T.step(th, bars, None, _claim_only_at_last(n),
                     progress=prog2, entry_bars=b5)

    fill_transitions = [t for t in res2.transitions
                        if t.to_state in (T.FILLED, T.CLOSED_TP, T.CLOSED_SL)]
    assert not fill_transitions, (
        "重放时挂单那一桶的子根被拿 h/l 判出了成交 → 交易所那头没单，"
        "变成孤儿单（把 <= 改成 < 就会这样）：%r"
        % ([t.to_state for t in fill_transitions],))
    assert res2.state == T.ARMED, (
        "挂单那一桶全部属于过去，重放后必须仍是 ARMED，实际 %s" % (res2.state,))


def test_超期按15m根数而不是5m根数():
    """超期阈值是**15m 根数**（horizon_bars=12 → 3 小时）。

    ★ 除数写成 300_000 会把阈值按 5m 算，等于容忍窗口放大 3 倍：
      挂单 4 根 15m（= 12 根 5m）就被判超期，而正确行为是还没到。
    """
    n = 40
    th, bars, b5, res, prog = _arm_at_tail(n)
    assert res.state == T.ARMED
    assert th.horizon_bars == 12, "前置条件：容忍 12 根"

    # 只走 4 根 15m（= 12 根 5m）：15m 口径下 4 < 12，不该超期。
    shift = 4
    slid15 = bars[shift:] + [
        _Bar(START + (n + k) * BAR15, 60500.0, 60520.0, 60480.0, 60500.0)
        for k in range(shift)]
    slid5 = b5[shift * 3:]
    for k in range(shift):
        ts15 = START + (n + k) * BAR15
        for j in range(3):
            slid5.append(_Bar(ts15 + j * BAR5, 60500.0, 60520.0, 60480.0, 60500.0))

    prog2 = dict(prog)
    prog2["next_index"] = 0

    res2, _ = T.step(th, slid15, None, None,
                     progress=prog2, entry_bars=slid5)
    assert res2.state == T.ARMED, (
        "只走了 4 根 15m（12 根 5m）就判超期 → 超期除数写成了 5m，"
        "容忍窗口被放大 3 倍（实际 %s）" % (res2.state,))


def test_子根分支也落时间戳锚点():
    """子根分支（没有 5m 时 `sb is bar`）同样要写 armed_bar_ts。

    ★ 它平时靠 `armed_at_sub_ts` 工作，但那是**第二道**锚点；
      sub_ts 一旦缺失/丢列，bar_ts 是唯一的兜底。
    """
    n = 40
    bars = _make_15m(n)
    th = _mk_thesis()
    # 不喂 entry_bars → seq = [bar] → 走子根分支
    res, prog = T.step(th, bars, None, _claim_only_at_last(n), progress=None)

    assert res.state == T.ARMED, "前置条件：应走到 ARMED，实际 %s" % (res.state,)
    assert prog["armed_at_sub_ts"] is not None, (
        "前置条件：子根分支必须设 sub_ts（这才叫子根分支）")
    assert prog["armed_bar_ts"] == START + (n - 1) * BAR15, (
        "子根分支也必须写粗周期时间戳锚点，实际 %r —— "
        "sub_ts 一旦缺失，没有 bar_ts 就会退回会漂移的下标判据"
        % (prog["armed_bar_ts"],))


def test_两条锚点都缺时必须留痕():
    """连 armed_bar_ts 都没有（极老数据）时，不许静默冻结。

    ★ 冻结的表现是「什么都不发生」—— 和「没到条件」在日志里长得一模一样。
      所以层内必须把 `freeze_anchor_missing` 交出去，由引擎记风险事件。
    """
    n = 40
    bars = _make_15m(n)
    b5 = _make_5m(n)
    th = _mk_thesis()

    prog_old = {
        "state": T.ARMED, "next_index": 0,
        "armed_at_bar": n - 1, "armed_at_sub_ts": None,
        "armed_bar_ts": None,
        "armed_ref_close": 60500.0,
    }
    res, prog = T.step(th, bars, None, None, progress=prog_old, entry_bars=b5)

    assert prog["armed_bar_ts"] is None, (
        "拿不到可靠来源时锚点必须保持 None（不许猜、更不许赋 0），实际 %r"
        % (prog["armed_bar_ts"],))
    assert res.freeze_anchor_missing is True, (
        "退回会漂移的下标判据时必须留痕（引擎据此记风险事件）")


# ------------------------------------------------------------------ 落库


def test_锚点列落库并读回(tmp_path):
    """armed_bar_ts 必须真的进库、再原样交回状态机。"""
    st = PTS.PendingThesisStore(str(tmp_path / "p.db"))
    try:
        row = PTS.row_of(_mk_thesis(), state=T.ARMED, next_index=5,
                         armed_at_bar=399, armed_bar_ts=1795657200000,
                         armed_ref_close=1.0, bar_ts=START)
        st.upsert_thesis(row)
        got = st._conn.execute(
            "SELECT armed_bar_ts FROM pending_theses WHERE thesis_id=?",
            (row["thesis_id"],)).fetchone()
        assert got["armed_bar_ts"] == 1795657200000, (
            "粗周期锚点没落库：%r" % (got["armed_bar_ts"],))

        loaded = dict(st._conn.execute(
            "SELECT * FROM pending_theses WHERE thesis_id=?",
            (row["thesis_id"],)).fetchone())
        prog = PTS.progress_of(loaded)
        assert prog["armed_bar_ts"] == 1795657200000
        assert "plan_bar_ts" not in prog, (
            "progress_of **不许**再交 plan_bar_ts：`pending_theses.bar_ts` "
            "是「末次推进到的根」（生产里比真实挂单根晚 5 小时），"
            "拿它当锚点会把历史成交全跳过")
    finally:
        st.close()


def test_成交锚点落库并读回(tmp_path):
    """★ FREEZE-1 收尾：`fill_bar_ts`（EXIT-1 的精确锚点）必须落库。

    改之前：`row_of()` 没这个参数、`upsert_thesis()` 没这一列、
    `progress_of()` 也不返回 → `thesis.step()` 里
    `p.get("fill_bar_ts")` 是**死代码**，恒 None，每轮都退回
    引擎按 `pos.entry_ts` 推断（晚一根）。
    """
    st = PTS.PendingThesisStore(str(tmp_path / "p.db"))
    try:
        row = PTS.row_of(_mk_thesis(), state=T.FILLED, next_index=5,
                         armed_at_bar=399, armed_bar_ts=1795657200000,
                         fill_bar_ts=1795659000000,
                         armed_ref_close=1.0, bar_ts=START)
        st.upsert_thesis(row)
        got = st._conn.execute(
            "SELECT fill_bar_ts FROM pending_theses WHERE thesis_id=?",
            (row["thesis_id"],)).fetchone()
        assert got["fill_bar_ts"] == 1795659000000, (
            "成交锚点没落库：%r" % (got["fill_bar_ts"],))

        loaded = dict(st._conn.execute(
            "SELECT * FROM pending_theses WHERE thesis_id=?",
            (row["thesis_id"],)).fetchone())
        prog = PTS.progress_of(loaded)
        assert prog["fill_bar_ts"] == 1795659000000, (
            "progress_of 不交回成交锚点 → 重启后 step 只能靠推断")
    finally:
        st.close()


def test_引擎优先用落库的精确成交锚点(tmp_path, monkeypatch):
    """★★ 落库里那个**精确**锚点必须压过引擎的推断。

    ★ 为什么单独一条：`thesis.step()` 里是
        `if not kw.get("fill_bar_ts"): kw["fill_bar_ts"] = p.get(...)`
      —— **kw 压过 p**。所以引擎那边如果无条件传推断值，
      就算补了列也白补（「精确值」永远赢不了「猜测值」）。
      这条测试钉的就是那个优先级。
    """
    import types

    from ai_trader.trading import pending_thesis_store as _PTS
    from ai_trader.trading import thesis as _T
    from tests.test_pending_wiring import _engine

    eng, _store = _engine(tmp_path, dry=False)

    # 落库的精确锚点：第 2 根 5m
    DB_BAR = START + 2 * BAR5
    # 推断值：pos.entry_ts 对齐后是第 5 根 5m —— 两者必须不同才测得出优先级
    POS_TS = START + 5 * BAR5 + 12345

    price = 61000.0
    th = T.Thesis(thesis_id="fill|down|%d|%.1f" % (START, price), kind="x",
                  direction=T.DOWN, order_price=price,
                  stop_price=price * 1.02, target_price=price * 0.996,
                  order_type=T.ORDER_LIMIT, family=T.FAMILY_REVERSAL,
                  surprise_pts=0.0)
    eng.pending.upsert_thesis(_PTS.row_of(
        th, state=T.FILLED, next_index=0, bar_ts=START,
        armed_at_bar=0, armed_bar_ts=START, fill_bar_ts=DB_BAR,
        armed_ref_close=price, ambiguous=False, note=""))
    eng.pos = {"side": "short", "qty": 0.01, "entry": price,
               "stop": price * 1.02, "target": price * 0.996,
               "open_reason": "挂单成交", "paper": 0, "fee": 0.0,
               "entry_ts": POS_TS, "bars": 0, "lots": [],
               "thesis_id": th.thesis_id}

    seen = {}
    real_step = _T.step

    def _spy(*a, **kw):
        seen.setdefault("fill_bar_ts", kw.get("fill_bar_ts"))
        return real_step(*a, **kw)

    monkeypatch.setattr(_T, "step", _spy)

    def _bars(n=6):
        """★ 必须给 5m：`_step_pending` 是先按 5m 收盘推进的（L3982），
        只给 15m 会直接 return，spy 一次都进不去 —— 那样测试会「假绿」。"""
        out = []
        for i in range(n):
            t = START + i * BAR15
            out.append(types.SimpleNamespace(
                open_time=t, ts=t, open=price, high=price + 10,
                low=price - 10, close=price, volume=1.0,
                interval_ms=BAR15))
        return out

    import asyncio
    b15 = _bars()
    b5 = []
    for b in b15:
        for k in range(3):
            t = b.ts + k * BAR5
            b5.append(types.SimpleNamespace(
                open_time=t, ts=t, open=price, high=price + 10,
                low=price - 10, close=price, volume=1.0,
                interval_ms=BAR5))
    asyncio.get_event_loop().run_until_complete(
        eng._step_pending({BAR15: b15, BAR5: b5}, price, 100.0,
                          b5[-1].ts))

    assert seen.get("fill_bar_ts") == DB_BAR, (
        "引擎传下去的是 %r，落库的精确锚点是 %r。"
        "推断值压过落库值 → EXIT-1 的「收盘跌破」永远晚一根"
        % (seen.get("fill_bar_ts"), DB_BAR))


def test_老库补armed_bar_ts列且不丢行(tmp_path):
    """生产库是 11-26 之前建的，必须 ALTER 补列，且历史行一行不少。

    ★ 这里**同时**剥掉 `fill_bar_ts`：两列是同一批加的，
      而「SCHEMA 改了但老库不会自动长列」这个坑对两列一视同仁 ——
      漏一条，`upsert_thesis` 就会在下单路径上每轮抛 ERROR。
    """
    import sqlite3

    path = str(tmp_path / "old.db")
    raw = sqlite3.connect(path)
    schema = PTS.SCHEMA.replace("    armed_bar_ts    INTEGER,\n", "")
    schema = schema.replace("    fill_bar_ts    INTEGER NOT NULL DEFAULT 0,\n", "")
    raw.executescript(schema)
    raw.execute("INSERT INTO pending_theses (thesis_id, state, next_index)"
                " VALUES ('legacy-1', 'ARMED', 400)")
    raw.commit()
    cols_before = {r[1] for r in raw.execute(
        "PRAGMA table_info(pending_theses)")}
    assert "armed_bar_ts" not in cols_before, "前置条件：老库没有这一列"
    assert "fill_bar_ts" not in cols_before, "前置条件：老库没有这一列"
    raw.close()

    st = PTS.PendingThesisStore(path)
    try:
        cols = {r[1] for r in st._conn.execute(
            "PRAGMA table_info(pending_theses)")}
        assert "armed_bar_ts" in cols, "打开老库时必须补 armed_bar_ts 列"
        assert "fill_bar_ts" in cols, "打开老库时必须补 fill_bar_ts 列"
        rows = st._conn.execute(
            "SELECT thesis_id, state FROM pending_theses").fetchall()
        assert len(rows) == 1 and rows[0]["thesis_id"] == "legacy-1", (
            "补列不能丢历史行")
    finally:
        st.close()


def test_成交锚点经引擎写回库(tmp_path):
    """★★ 状态机算出的 `fill_bar_ts` 必须**经引擎写回库**。

    ★ 为什么单测 store 不够：`row_of()`、`upsert_thesis()` 都对了，
      引擎那一行漏传（`fill_bar_ts=0`）照样全绿 ——
      「单测过、接线错」正是本仓反复踩的坑。
      所以这里走完整 `_step_pending`，最后回库看值。
    """
    import asyncio
    import types

    from ai_trader.trading import pending_thesis_store as _PTS
    from tests.test_pending_wiring import _engine

    eng, _store = _engine(tmp_path, dry=False)
    eng.pos = None                      # 空仓：成交要找交易所落账，这里只验锚点

    price = 61000.0
    th = T.Thesis(thesis_id="fillio|down|%d|%.1f" % (START, price),
                  kind="x", direction=T.DOWN, order_price=price,
                  stop_price=price * 1.02, target_price=price * 0.996,
                  order_type=T.ORDER_LIMIT, family=T.FAMILY_REVERSAL,
                  surprise_pts=0.0, horizon_bars=12)
    eng.pending.upsert_thesis(_PTS.row_of(
        th, state=T.ARMED, next_index=0, bar_ts=START,
        armed_at_bar=0, armed_bar_ts=START, armed_ref_close=price,
        ambiguous=False, note=""))

    b15, b5 = [], []
    for i in range(6):
        t = START + i * BAR15
        # 第 2 根起最高价穿过挂单价 → 成交
        hi = price + 60.0 if i >= 1 else price - 50.0
        b15.append(types.SimpleNamespace(
            open_time=t, ts=t, open=price, high=hi, low=price - 90.0,
            close=price - 30.0, volume=1.0, interval_ms=BAR15))
    for b in b15:
        for k in range(3):
            t = b.ts + k * BAR5
            b5.append(types.SimpleNamespace(
                open_time=t, ts=t, open=b.open, high=b.high, low=b.low,
                close=b.close, volume=1.0, interval_ms=BAR5))

    asyncio.get_event_loop().run_until_complete(
        eng._step_pending({BAR15: b15, BAR5: b5}, price, 100.0, b5[-1].ts))

    row = dict(eng.pending._conn.execute(
        "SELECT * FROM pending_theses WHERE thesis_id=?",
        (th.thesis_id,)).fetchone())
    assert row["fill_bar_ts"], (
        "成交锚点没写回库（%r）→ 重启后只能靠 pos.entry_ts 推断，晚一根"
        % (row["fill_bar_ts"],))
    # 必须落在 5m 桶边界上（锚点的口径就是「成交那根 5m 的开盘时刻」）
    assert int(row["fill_bar_ts"]) % BAR5 == 0, (
        "锚点没对齐 5m 桶：%r" % (row["fill_bar_ts"],))
    assert int(row["fill_bar_ts"]) >= START

