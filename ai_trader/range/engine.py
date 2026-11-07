# -*- coding: utf-8 -*-
"""区间引擎 v9.1（2026-11-03 修订）

相对 v9 的 7 处修改（依据 docs/区间演化规则-规范v10-方案稿.md）：
  M1  突破判据：c < trend(下沿P)  ->  c < key_prev(上一根结束时的关键价K)   [D1]
  M2  循环内新增 key_prev 捕获（在 key 更新之前）                          [D1]
  M3  家族号拆两级：family(反转+1) / segment(同向突破+1)，显示 #fam.seg     [D2]
  M4  TOL 50.0 -> 30.0                                                   [D7]
  M5  软边界 S = 冻结 R 那根（成形那根）的实体收盘                          [D4]
  M6  load_1h 打印输入自证与丢弃桶清单，不再静默                           [C1/C2]
  M7  事件输出增加 参照量名 / 参照量值 / 裕度                              [C3]

状态元组（向后兼容：前 9 位与 v9 完全一致，新字段追加在尾部）
  (ts, lo_b, hi_b, seg_serial, direction, hard, soft, key, tag, family, segment)
  - seg_serial : 全局段序号（v9 的 lin，语义不变，仍是"每换一次家族 +1"）
  - family     : 区间号（只有反转才 +1）
  - segment    : 段号（同向突破 +1，新区间从 1 开始）
  - 下沿/上沿（lo_b/hi_b）仍为 (P, R) 或 (R, P)：下跌 lo_b=P hi_b=R，上涨 lo_b=R hi_b=P

事件元组
  (时间, 事件, 价格, family, segment, 参照量名, 参照量值, 裕度%)
"""
from __future__ import annotations
import calendar, csv, os, sys, time

PKG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))      # …/ai_trader
ROOT = os.path.dirname(PKG)                                             # 仓库根


def _resolve_bars_csv():
    """定位 15m 行情 CSV —— 唯一数据源入口，可被环境变量 RANGE_BARS_CSV 覆盖。

    历史坑（交接文档 §10.6）：本文件曾按「向上退两级」硬拼路径，从
    cryptobot/trend/ 出发退到 cryptobot/，指向并不存在的 cryptobot/data/，
    于是 load_1h 必然 FileNotFoundError —— 同一份代码在本机根本跑不起来。
    改为按候选清单探测，不再依赖目录层级深度。
    """
    env = (os.environ.get("RANGE_BARS_CSV") or "").strip()
    # ⚠️ 2026-11-04 顺序修正（RP11-05）：PKG/data 必须优先于 ROOT/data。
    #   原顺序 [ROOT/data, PKG/data] 埋了个雷：fetch_range_bars.py 的 OUT 是
    #   **PKG/data**（cryptobot/data），而引擎先看 ROOT/data（仓库根 data/）。
    #   只要仓库根存在一份旧的 data/range_bars_15m.csv，新拉的数据就永远读不到 ——
    #   生产上 ROOT/data 恰好不存在所以侥幸没事，本地研究仓则一直读到陈旧文件
    #   （实测：根 data/ 是 60 天 5760 行 / 270KB，PKG/data/ 才是最新）。
    #   改为写入方优先，消除「谁先谁后看运气」。
    cands = ([env] if env else []) + [
        os.path.join(PKG,  "data", "range_bars_15m.csv"),
        os.path.join(ROOT, "data", "range_bars_15m.csv"),
    ]
    for c in cands:
        if c and os.path.exists(c):
            return c
    return env or cands[1]


CSV = _resolve_bars_csv()
TZ = 8 * 3600
TOL = 30.0

# ---- 预热裁剪根数：全仓唯一口径，实时管线与回填工具必须共用同一个值 --------------
# 2026-11-05 实测（tools 密集扫描，5 标的 × 69 个切点，每 20 根一个切点）：
#   BTCUSDT 最大 225(中位42/p90 154) · ETHUSDT 419(69/279) · SOLUSDT 229(49/169)
#   BNBUSDT 458(56/218) · XRPUSDT 450(93/313)
# 全部样本的真实上界 = **458 根**（BNB，cut=220）。历史上通用的 24 根差了一个数量级。
#
# 为什么需求这么跳跃（中位 42、个别 458）：
#   引擎状态是路径依赖的。切点若落在某个长寿区间的中间，必须把该区间的「出生」
#   连同上一代区间的边界一起复原，才可能算出同一组四线 —— 实测最长区间寿命 208 根，
#   458 ≈ 2.2 代，与「要两代历史」的解释吻合。所以不能取中位数，必须取上界。
#
# 取 600 = 458 上界 + 31% 余量。代价：少写 300 行历史（约 12 天），换来头部不再脏。
# **改这个值必须同步改回填口径，并重跑 backfill_range_evolution.py** ——
# 两边不一致时，实时管线的 diff_many 会把回填好的行逐批改回去（实测 11 小时改了 15 行）。
WARMUP_1H_BARS = 600
FORM_BARS = 3      # 成形窗口：突破/反转后 3 根 1h 内取反抽极值（规范 v11 §2）
REST_BARS = 1      # 停留门槛：停留 <= REST_BARS 根不切段，> REST_BARS 根才允许切段（规范 v11 §6，用户 2026-11 定 1）
ROLL_RUN = 4       # R 滚动启动门槛：连续逐根收盘走低(高) > ROLL_RUN 根（规范 v11 §7b，用户定 4·口径B）
ROLL_EVERY = 3     # R 滚动重定频率：每 ROLL_EVERY 根 1h 一次           # M4 [D7] 二次确认容差：绝对 30 点（原 50 点）

def ts8(ms): return time.strftime("%Y-%m-%d %H:%M", time.gmtime(ms/1000+TZ))
def lab(s): return calendar.timegm(time.strptime(s, "%Y-%m-%d %H:%M"))*1000 - TZ*1000
def fmt(family, segment): return "#%d.%d" % (family, segment)


def load_1h(verbose=True, csv_path=None):
    """15m -> 1h 完整桶。M6 [C1/C2]：输入自证与丢弃清单必须打印，禁止静默过滤。

    csv_path: 显式指定 CSV。多品种循环里**必须传**，否则会读模块级全局 CSV，
              把上一个品种的数据当成当前品种（静默串味）。
    返回元组为 6 位 (ts, o, h, l, c, volume)：volume 为 4 根 15m 之和，
    老格式 CSV（只有 ts,o,h,l,c 五列）读出来就是 None —— 不伪造 0。
    """
    path = csv_path or CSV
    rows = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            _v = r.get("v") or r.get("volume")
            rows.append((int(r["ts"]), float(r["o"]), float(r["h"]), float(r["l"]), float(r["c"]),
                         float(_v) if _v not in (None, "") else None))
    h = {}
    for ts, o, hi, lo, c, vol in rows:
        k = ts // 3600000 * 3600000
        if k in h:
            v = h[k]; v[1] = max(v[1], hi); v[2] = min(v[2], lo); v[3] = c; v[4] += 1
            if vol is not None:
                v[5] = vol if v[5] is None else v[5] + vol
        else: h[k] = [o, hi, lo, c, 1, vol]
    out = [(k, v[0], v[1], v[2], v[3], v[5]) for k, v in sorted(h.items()) if v[4] == 4]
    dropped = [(k, v[4]) for k, v in sorted(h.items()) if v[4] != 4]
    if verbose:
        sys.stderr.write("[load_1h] 15m 行=%d -> 1h 桶=%d -> 实际使用=%d | 丢弃不完整桶=%d\n"
                         % (len(rows), len(h), len(out), len(dropped)))
        for k, n in dropped:
            sys.stderr.write("[load_1h]   丢弃 %s（只有 %d 根 15m，不足 4 根）\n" % (ts8(k), n))
    return out


LAST = {}      # build() 产物缓存：events / states / cands

def build(bars, soft_overrides=None):
    """soft_overrides: {区间号family: 人工设定的软边界}；下跌区间软上沿不得越过 R，上涨区间镜像。
    注意：全仓库当前无任何调用点传入非空值（见 docs 规范 v10 §9.1）。"""
    family = 0
    segment = 0
    seg_serial = 0
    direction = None
    soft_overrides = soft_overrides or {}
    hard = soft = trend = None
    key = None
    run_lo = run_hi = None
    settled = False
    reb_hi = None
    forming = False
    cand = None
    pend = None            # 成形窗口（规范 v11 §2）：{"p0o","n","R","S","snap"}
    scand = None           # 软边界候选（规范 v11 §5）
    dwell = 0              # 停留计数：收盘没创同向新低/新高的连续根数（规范 v11 §6）
    close_ext = None       # 同向收盘极值（判停留用）
    ext_run = 0            # 连续逐根收盘走低(高)根数 —— 口径B（规范 v11 §7b）
    prev_close = None      # 上一根收盘（口径B 用）
    roll_cnt = 0           # R 滚动重定计数器
    trend_virgin = False   # 顺向边界是否处于新鲜期（规范 v11 §11.1）
    rr_ext = None          # 反弹(回调)极值（§11.2 回落三根）
    rr_guard = None        # 含极值往前数第三根的对侧极值（回落/回升判据）
    events, states = [], []

    def snapshot():
        """保存可回滚的全部区间状态（规范 v11 §3 反包退回用）"""
        return (family, segment, seg_serial, direction, hard, soft, trend, key,
                settled, reb_hi, forming, cand, len(events), len(states), trend_virgin,
                rr_ext, rr_guard)

    def restore(sn):
        nonlocal family, segment, seg_serial, direction, hard, soft, trend, key
        nonlocal settled, reb_hi, forming, cand, trend_virgin, rr_ext, rr_guard
        (family, segment, seg_serial, direction, hard, soft, trend, key,
         settled, reb_hi, forming, cand, ne, ns, trend_virgin,
         rr_ext, rr_guard) = sn
        del events[ne:]
        # 规范 §3「反包 => 退回上一区间，按 P0 那根不产生任何新区间处理」：
        #   「不产生新区间」≠「这几根没有状态」。P0 及成形窗口内的每根都是真实 K 线，
        #   必须各留一行 state，值 = 退回后原区间的冻结值（方案 A）。
        #   旧实现 del states[ns:] 把这几行直接删掉 => 时间序列出现空洞
        #   （60 天样本 24 个缺口，最长连续 3 根），下游按 ts 对齐会静默错位。
        #   事件照旧回滚（幻影区间期间的插针/滚动事件不应留下）。
        _lo, _hi = (hard, trend) if direction == "UP" else (trend, hard)
        for _i in range(ns, len(states)):
            states[_i] = (states[_i][0], _lo, _hi, seg_serial, direction,
                          hard, soft, key, "反包", family, segment)

    def advance_trend(direction, trend, cand, hi, lo, c):
        """成熟期二次确认（规范 v11 §11.1 / §11.3，用户 2026-11 口径）
        候选的产生：由「收盘跌破」/「收盘站上」触发，基准 = **那根的收盘价**（不是插针极值）。
        候选的寿命：**只存活一根** —— 未确认即作废，下沿/上沿保持不动。
        确认的判据（TOL 唯一适用范围）：
            DOWN  下一根收盘不得高于候选（高出允许 TOL）：c_next - TOL <= cand
            UP    下一根收盘不得低于候选（低于允许 TOL）：c_next + TOL >= cand
        单向棘轮：下沿只降不升、上沿只升不降（确认收盘越界时钳位，不再回退）。
        """
        if direction == "DOWN":
            if cand is None:
                if c < trend: return trend, c, None              # 收盘跌破 => 候选
                return trend, None, None
            if c - TOL <= cand:                                  # 下一根确认
                return min(trend, c), None, "下沿确认"
            return trend, None, None                             # 未确认 => 候选作废
        else:
            if cand is None:
                if c > trend: return trend, c, None              # 收盘站上 => 候选
                return trend, None, None
            if c + TOL >= cand:
                return max(trend, c), None, "上沿确认"
            return trend, None, None

    def advance_soft(direction, sft, scand, hi, lo, c, hard):
        """软边界 = 区间上沿/下沿（规范 v11 §5）。
        候选 -> 下一根确认（容差 TOL）-> 取【确认那根的收盘价】（不是候选那根的）。
        只朝硬边界单向推进，永不越过硬边界。"""
        if direction == "DOWN":                     # 软上沿：跟反弹高点，向上不越 R
            if scand is None:
                if sft is None or hi > sft: scand = hi
            elif c >= scand - TOL:
                sft, scand = (c if sft is None else max(sft, c)), None   # 软上沿只升不降
            elif hi > scand:
                scand = hi
            if hard is not None and sft is not None:
                sft = min(sft, hard)
        else:                                       # 软下沿：镜像
            if scand is None:
                if sft is None or lo < sft: scand = lo
            elif c <= scand + TOL:
                sft, scand = (c if sft is None else min(sft, c)), None   # 软下沿只降不升
            elif lo < scand:
                scand = lo
            if hard is not None and sft is not None:
                sft = max(sft, hard)
        return sft, scand

    cands = []                                   # 候选K台账（下一根确认的候选及确认收盘价）
    # 按索引取前 5 位：bars 现在是 6 元组（带 volume），历史调用方可能仍传 5 元组。
    # 用元组解包会在 6 元组上直接 ValueError，故此处按位取，两种形状都吃。
    for i, _bar in enumerate(bars):
        ts, o, hi, lo, c = _bar[0], _bar[1], _bar[2], _bar[3], _bar[4]
        if direction is None:                       # 预热
            if run_lo is not None:
                if c > run_hi:
                    direction = "UP"; hard = soft = None; forming = True
                    trend = hi; key = hi; cand = None
                    family += 1; segment = 1; seg_serial += 1
                    settled = False; reb_hi = hi
                    scand = None; dwell = 0; close_ext = None; ext_run = 0; roll_cnt = 0; prev_close = None; pend = {"p0o": o, "n": 0, "R": None, "S": None, "snap": None}
                    events.append((ts8(ts), "birth", c, family, segment, "prev_hi", run_hi, None))
                    states.append((ts, None, None, seg_serial, direction, None, None, key, "birth", family, segment)); continue
                if c < run_lo:
                    direction = "DOWN"; hard = soft = None; forming = True
                    trend = lo; key = lo; cand = None
                    family += 1; segment = 1; seg_serial += 1
                    settled = False; reb_hi = None
                    scand = None; dwell = 0; close_ext = None; ext_run = 0; roll_cnt = 0; prev_close = None; pend = {"p0o": o, "n": 0, "R": None, "S": None, "snap": None}
                    events.append((ts8(ts), "birth", c, family, segment, "prev_lo", run_lo, None))
                    states.append((ts, None, None, seg_serial, direction, None, None, key, "birth", family, segment)); continue
            run_lo = lo if run_lo is None else min(run_lo, lo)
            run_hi = hi if run_hi is None else max(run_hi, hi)
            states.append((ts, run_lo, run_hi, seg_serial, "PRE", None, None, None, "pre", family, segment)); continue

        # M2 [D1]：先捕获「上一根结束时」的顺向关键价，再做本根的 K 更新
        key_prev = key

        # 顺向关键价 K：每根 min/max，无滞后
        if direction == "DOWN":
            key = lo if key is None else min(key, lo)
        else:
            key = hi if key is None else max(key, hi)

        # 插针更新反向边界（不换家族）
        if not forming and hard is not None:
            if direction == "DOWN" and hi > hard and c <= hard:
                marg = (hi - hard) / hard * 100
                hard = hi
                soft = min(soft, hard) if soft is not None else None
                events.append((ts8(ts), "反向边界插针上移", hi, family, segment, "R", hard, marg))
            elif direction == "UP" and lo < hard and c >= hard:
                marg = (hard - lo) / hard * 100
                hard = lo
                soft = max(soft, hard) if soft is not None else None
                events.append((ts8(ts), "反向边界插针下移", lo, family, segment, "R", hard, marg))

        # ===== 成形窗口（规范 v11 §2/§3）：P0 反包线 + 3 根取极值，反包则退回上一区间 =====
        if pend is not None:
            pend["n"] += 1
            p0o = pend["p0o"]
            breach = (direction == "DOWN" and c > p0o) or (direction == "UP" and c < p0o)
            if breach:
                snap = pend["snap"]
                pend = None
                if snap is None:                      # 出生即失败 => 退回预热态
                    f_old, s_old = family, segment
                    direction = None; hard = soft = trend = key = None
                    forming = False; cand = None; settled = False; reb_hi = None
                    run_lo = lo; run_hi = hi
                    events.append((ts8(ts), "突破失败·反包", c, f_old, s_old, "P0开", p0o, None))
                    states.append((ts, run_lo, run_hi, seg_serial, "PRE", None, None, None, "pre", family, segment)); continue
                restore(snap)                          # 先回滚（会截断事件），再记录反包
                events.append((ts8(ts), "突破失败·反包", c, family, segment, "P0开", p0o, None))
                lo_b, hi_b = (hard, trend) if direction == "UP" else (trend, hard)
                states.append((ts, lo_b, hi_b, seg_serial, direction, hard, soft, key, "", family, segment)); continue
            # 阶段① 下沿直接赋值（不走二次确认）；下硬边界 = 插针极值
            if direction == "DOWN":
                key = lo if key is None else min(key, lo)
                trend = lo if trend is None else min(trend, lo)
                if pend["R"] is None or hi > pend["R"]:
                    pend["R"], pend["S"] = hi, c
            else:
                key = hi if key is None else max(key, hi)
                trend = hi if trend is None else max(trend, hi)
                if pend["R"] is None or lo < pend["R"]:
                    pend["R"], pend["S"] = lo, c
            if pend["n"] >= FORM_BARS:
                hard, soft = pend["R"], pend["S"]
                trend = key                            # 阶段② 成形那刻：下沿 = 下硬边界
                forming = False; settled = True; trend_virgin = True
                pend = None
                events.append((ts8(ts), "区间成形", hard, family, segment, "R", hard, None))
            if family in soft_overrides:
                v = soft_overrides[family]
                soft = min(v, hard) if (direction == "DOWN" and hard) else (max(v, hard) if hard else v)
            lo_b, hi_b = (hard, trend) if direction == "UP" else (trend, hard)
            states.append((ts, lo_b, hi_b, seg_serial, direction, hard, soft, key, ("forming" if forming else ""), family, segment)); continue

        # 停留计数（规范 v11 §6）：收盘没创同向新低/新高 = 停留 1 根
        # 停留（口径A）：收盘没创同向累积新低/新高 => 停留 1 根（切段门槛用）
        new_close_ext = (close_ext is None) or (c < close_ext if direction == "DOWN" else c > close_ext)
        prior_dwell = dwell
        if new_close_ext:
            close_ext = c; dwell = 0
        else:
            dwell += 1
        # 连击（口径B）：逐根收盘走低/走高 => 累计；否则归零（R 滚动启动用）
        _prev_c = prev_close          # 上一根收盘（新鲜期定格用）
        step = (prev_close is None) or (c < prev_close if direction == "DOWN" else c > prev_close)
        ext_run = ext_run + 1 if step else 0
        prev_close = c

        # ===== R 滚动重定（规范 v11 §7b）：连续同向极值 > ROLL_RUN 根后，每 ROLL_EVERY 根重定一次 =====
        # 取法：窗口内有反向K => 反向K里振幅最大那根；无反向K => 振幅最小那根。
        # DOWN 取那根的最高点、UP 取最低点；R 只朝硬边界方向移动，永不反向。
        if ext_run > ROLL_RUN and not forming and hard is not None:
            roll_cnt += 1
            if roll_cnt >= ROLL_EVERY:
                roll_cnt = 0
                win = bars[max(0, i - ROLL_EVERY + 1): i + 1]
                if win:
                    if direction == "DOWN":
                        opp = [b for b in win if b[4] > b[1]]
                        pick = max(opp, key=lambda b: b[2] - b[3]) if opp else min(win, key=lambda b: b[2] - b[3])
                        cand_r = pick[2]
                        if cand_r < hard:
                            marg = (hard - cand_r) / hard * 100
                            hard = cand_r
                            if soft is not None: soft = min(soft, hard)
                            events.append((ts8(ts), "R滚动", cand_r, family, segment, "拾取根", pick[0] if False else cand_r, marg))
                    else:
                        opp = [b for b in win if b[4] < b[1]]
                        pick = max(opp, key=lambda b: b[2] - b[3]) if opp else min(win, key=lambda b: b[2] - b[3])
                        cand_r = pick[3]
                        if cand_r > hard:
                            marg = (cand_r - hard) / hard * 100
                            hard = cand_r
                            if soft is not None: soft = max(soft, hard)
                            events.append((ts8(ts), "R滚动", cand_r, family, segment, "拾取根", cand_r, marg))

        # M1 [D1] 顺向突破 + 停留门槛：停留不足 REST_BARS 根的同向新低只延长本段，不切段
        if not forming and settled:
            if direction == "DOWN" and key_prev is not None and c < key_prev and prior_dwell > REST_BARS:
                snap0 = snapshot()
                marg = (key_prev - c) / key_prev * 100
                segment += 1; seg_serial += 1
                events.append((ts8(ts), "向下突破", c, family, segment, "K", key_prev, marg))
                hard = soft = None; forming = True; cand = None; settled = False
                trend = lo; key = lo; reb_hi = None
                scand = None; dwell = 0; close_ext = None; ext_run = 0; roll_cnt = 0; prev_close = None; pend = {"p0o": o, "n": 0, "R": None, "S": None, "snap": snap0}
                states.append((ts, None, None, seg_serial, "DOWN", None, None, key, "突破", family, segment)); continue
            if direction == "UP" and key_prev is not None and c > key_prev and prior_dwell > REST_BARS:
                snap0 = snapshot()
                marg = (c - key_prev) / key_prev * 100
                segment += 1; seg_serial += 1
                events.append((ts8(ts), "向上突破", c, family, segment, "K", key_prev, marg))
                hard = soft = None; forming = True; cand = None; settled = False
                trend = hi; key = hi; reb_hi = hi
                scand = None; dwell = 0; close_ext = None; ext_run = 0; roll_cnt = 0; prev_close = None; pend = {"p0o": o, "n": 0, "R": None, "S": None, "snap": snap0}
                states.append((ts, None, None, seg_serial, "UP", None, None, key, "突破", family, segment)); continue

        # 反转：收盘越过反向边界 R
        rev = None
        if direction == "DOWN" and hard is not None and c > hard: rev = "UP"
        elif direction == "UP" and hard is not None and c < hard: rev = "DOWN"
        if rev:
            # 参照量必须在重置之前捕获，否则事件里会丢掉 R
            R_before = hard
            snap0 = snapshot()
            marg = ((c - R_before) / R_before * 100) if direction == "DOWN" else ((R_before - c) / R_before * 100)
            family += 1; segment = 1; seg_serial += 1
            direction = rev
            hard = soft = None; forming = True; cand = None
            trend = hi if rev == "UP" else lo
            key = hi if rev == "UP" else lo
            settled = False
            reb_hi = hi if rev == "UP" else lo
            scand = None; dwell = 0; close_ext = None; ext_run = 0; roll_cnt = 0; prev_close = None; pend = {"p0o": o, "n": 0, "R": None, "S": None, "snap": snap0}
            events.append((ts8(ts), "向上反转" if rev == "UP" else "向下反转", c, family, segment, "R", R_before, marg))
            states.append((ts, None, None, seg_serial, rev, None, None, key, "反转", family, segment)); continue

        # 反弹回落三根（§11.2 / §5.3）：每次创新高(低) 记「含它往前数第三根」的对侧极值；
        # 行情继续创新高(低) 就整组刷新；**收盘跌破(站上)该极值即反弹失败**，单根规则、无二次确认。
        # 一次性语义：失败只报一次 —— 报完把 rr_guard 清空（保留 rr_ext），
        # 只有「再创出新高/新低」才重新立判据，避免每两根重复触发同一事件。
        if not trend_virgin and hard is not None and i >= 2:
            if direction == "DOWN":
                if rr_ext is None or hi > rr_ext:
                    rr_ext, rr_guard = hi, bars[i - 2][3]
                elif rr_guard is not None and c < rr_guard:
                    events.append((ts8(ts), "上沿确认", soft, family, segment, "回落三根", rr_guard, None))
                    rr_guard = None
            else:
                if rr_ext is None or lo < rr_ext:
                    rr_ext, rr_guard = lo, bars[i - 2][2]
                elif rr_guard is not None and c > rr_guard:
                    events.append((ts8(ts), "下沿确认", soft, family, segment, "回升三根", rr_guard, None))
                    rr_guard = None

        # 顺向边界（下沿/上沿 P，仅显示）推进
        _cw, _tw = (cand is not None), trend
        if trend_virgin:
            # 新鲜期（§11.1）：逐根直接跟随同向极值，不需下一根确认；
            # 首根收盘同向不再推进 => 定格为「前一根收盘价」，转成熟期。
            _up = (_prev_c is not None) and ((c > _prev_c) if direction == "DOWN" else (c < _prev_c))
            if _prev_c is not None and _up:
                trend = _prev_c
                trend_virgin = False
                cand = None
                rr_ext = None; rr_guard = None                    # 开始跟踪反弹
                note = "下沿定格" if direction == "DOWN" else "上沿定格"
            elif trend is None or (lo < trend if direction == "DOWN" else hi > trend):
                trend = lo if direction == "DOWN" else hi
                cand = None
                note = None
            else:
                note = None
        else:
            trend, cand, note = advance_trend(direction, trend, cand, hi, lo, c)
        if not _cw and cand is not None:
            cands.append(dict(born_ts=ts, kind=("下沿候选" if direction == "DOWN" else "上沿候选"),
                              cand_price=cand, cand_close=c, test_line=_tw, family=family, segment=segment,
                              direction=direction, status="pending", confirm_ts=None, confirm_close=None, applied=None))
        elif _cw and cand is None:
            for _d in reversed(cands):
                if _d["status"] == "pending" and _d["kind"] in ("下沿候选", "上沿候选"):
                    _d.update(status=("confirmed" if note else "void"), confirm_ts=ts,
                              confirm_close=c, applied=(trend if note else None)); break
        if note and note != "候选丢弃":
            events.append((ts8(ts), note, trend, family, segment, "cand", cand if cand else trend, None))
            settled = True
        # 软边界：二次确认推进，取确认那根收盘价，单向逼近硬边界
        _sw, _tw2 = (scand is not None), soft
        soft, scand = advance_soft(direction, soft, scand, hi, lo, c, hard)
        if not _sw and scand is not None:
            cands.append(dict(born_ts=ts, kind=("软上沿候选" if direction == "DOWN" else "软下沿候选"),
                              cand_price=scand, cand_close=c, test_line=_tw2, family=family, segment=segment,
                              direction=direction, status="pending", confirm_ts=None, confirm_close=None, applied=None))
        elif _sw and scand is None:
            for _d in reversed(cands):
                if _d["status"] == "pending" and _d["kind"] in ("软上沿候选", "软下沿候选"):
                    _d.update(status="confirmed", confirm_ts=ts, confirm_close=c, applied=soft); break
        if family in soft_overrides:
            v = soft_overrides[family]
            soft = min(v, hard) if (direction == "DOWN" and hard) else (max(v, hard) if hard else v)
        if direction == "DOWN":
            reb_hi = hi if reb_hi is None else max(reb_hi, hi)
        else:
            reb_hi = lo if reb_hi is None else min(reb_hi, lo)
        lo_b, hi_b = (hard, trend) if direction == "UP" else (trend, hard)
        states.append((ts, lo_b, hi_b, seg_serial, direction, hard, soft, key, "", family, segment))

    LAST.update(events=events, states=states, cands=cands)
    return events, states


def range_at(bars, when, soft_overrides=None):
    ev, st = build(bars, soft_overrides)
    t = lab(when) if isinstance(when, str) else when
    best = None
    for s in st:
        if s[0] <= t: best = s
        else: break
    return best, ev


def main(argv=None):
    """命令行自检入口。抽成函数是为了让 tools/ 垫片可以直接委托，避免复制一份逻辑。"""
    argv = list(sys.argv[1:] if argv is None else argv)
    verbose = "-q" not in argv
    bars = load_1h(verbose=verbose)
    args = [a for a in argv if not a.startswith("-")]
    w = (args or ["2026-10-23 00:00"])[0]
    s, ev = range_at(bars, w)

    print("== 反转链（每次区间号 +1）==")
    for e in ev:
        if "反转" in e[1]:
            print("  %s  %-8s 价 %.1f  区间%s  裕度 %s"
                  % (e[0], e[1], e[2], fmt(e[3], e[4]), ("%.4f%%" % e[7]) if e[7] is not None else "—"))

    print("\n== %s 时点的区间 ==" % w)
    if s:
        ts, lo_b, hi_b, lin, d, hard, soft, key, tag, fam, seg = s
        print("  方向        %s" % d)
        print("  区间号      %s" % fmt(fam, seg))
        print("  下沿        %s  (顺向边界 P，仅显示)" % (("%.1f" % lo_b) if lo_b else "未成形"))
        print("  上沿        %s  (反向边界 R，反转触发线)" % (("%.1f" % hi_b) if hi_b else "未成形"))
        print("  反向边界    %s" % (("%.1f" % hard) if hard else "未成形"))
        print("  软边界      %s" % (("%.1f" % soft) if soft else "未成形"))
        print("  顺向关键价  %s  (%s，判突破用这个)"
              % (("%.1f" % key) if key else "未定", "最低点关键价" if d == "DOWN" else "最高点关键价"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
