# -*- coding: utf-8 -*-
"""FREEZE-1 的**变异测试**：故意改坏粗周期锚点，确认回归测试会红。

为什么单独一个工具而不并入 `mut_engulf.py`
-------------------------------------------

`mut_engulf.py` 跑的是 `test_engulf.py` + `test_sizing_migrated.py`，
那两套测试**不喂 5m 序列**，也就永远走不到粗粒度 arming 分支 ——
把锚点改坏了它们照样绿。变异测试挂错测试文件，结果是「全部杀死」的假象。
所以这里单独跑 `test_pending_freeze_tail.py` + `test_pending_anchor_persist.py`。

用法
----

    python tools/mut_freeze.py            # 全跑
    python tools/mut_freeze.py -k 回填     # 只跑名字含关键字的
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

THESIS = os.path.join(ROOT, "ai_trader", "trading", "thesis.py")
STORE = os.path.join(ROOT, "ai_trader", "trading", "pending_thesis_store.py")
ENGINE = os.path.join(ROOT, "ai_trader", "trading", "engine.py")
SELF = os.path.join(ROOT, "ai_trader", "trading", "selfcheck.py")
TESTS = [
    os.path.join(ROOT, "tests", "test_pending_freeze_tail.py"),
    os.path.join(ROOT, "tests", "test_pending_anchor_persist.py"),
    # 改名（15m反包K）的变异体靠这一份咬住
    os.path.join(ROOT, "tests", "test_engulf.py"),
    # 「另一个写入方抹掉锚点」的变异体靠这一份咬住
    os.path.join(ROOT, "tests", "test_selfcheck.py"),
]

# ---------------------------------------------------------------------------
# 每个变异体：(名字, 文件, 原文, 变异后, 为什么这条必须被杀死)
# ---------------------------------------------------------------------------

MUTANTS = [
    # ---- 锚点写的对不对 ----
    ("粗周期_不写时间戳锚点", THESIS,
     "                    armed_at_bar = i\n"
     "                    # ★★ FREEZE-1：粗周期锚点必须是**时间戳**。\n"
     "                    #   只记下标的话，它恒等于缓冲末尾，成交守卫永远进不去。\n"
     "                    armed_bar_ts = int(ts)\n",
     "                    armed_at_bar = i\n",
     "回到事故原形：只有下标，成交永远判不出来（本工具的立项理由）"),

    ("粗周期_锚点写成索引", THESIS,
     "                    armed_bar_ts = int(ts)\n",
     "                    armed_bar_ts = i\n",
     "把下标当时间戳存 → 守卫拿 i 和 i 比，等于没改"),

    ("子根_不写时间戳锚点", THESIS,
     "                armed_bar_ts = int(ts)       # ★ FREEZE-1：粗周期锚点，恒落库\n",
     "",
     "子根分支不写 bar_ts → 那些预期只剩 sub_ts，缓冲滑动后同样会失锚"),

    # ---- 守卫判的对不对 ----
    ("守卫_退回下标判据", THESIS,
     "                elif armed_bar_ts is not None:\n"
     "                    if int(ts) <= int(armed_bar_ts):\n"
     "                        continue\n",
     "",
     "跳过时间戳分支 → 直接退回会漂移的下标判据（事故主因）"),

    ("守卫_只挡早于锚点的桶", THESIS,
     "                    if int(ts) <= int(armed_bar_ts):\n"
     "                        continue\n",
     "                    if int(ts) < int(armed_bar_ts):\n"
     "                        continue\n",
     "把 <= 改成 < → 挂单那一根自己也会被拿 h/l 判成交（真钱事故：孤儿子单）"),

    ("守卫_比较方向写反", THESIS,
     "                    if int(ts) <= int(armed_bar_ts):\n"
     "                        continue\n",
     "                    if int(ts) >= int(armed_bar_ts):\n"
     "                        continue\n",
     "方向写反 → 挂单之后每一根都被跳过，等于没修"),

    ("守卫_比的是计划时间戳", THESIS,
     "                    if int(ts) <= int(armed_bar_ts):\n"
     "                        continue\n",
     "                    if int(ts) <= int(sb_ts):\n"
     "                        continue\n",
     "拿子根 ts 当锚点 → 永远成立，全跳过"),

    # ---- 老行回填 ----
    ("回填_完全不做", STORE,
     "        self.backfill_armed_bar_ts()\n",
     "",
     "生产 30 条历史行都没有这一列 → 下次重启全量冻结（事故行就是老行）"),

    ("回填_用thesis的bar_ts", STORE,
     "                \"  SELECT MIN(a.bar_ts) FROM pending_thesis_actions a\"\n"
     "                \"   WHERE a.thesis_id = pending_theses.thesis_id\"\n"
     "                \"     AND a.kind = 'place_order'\"\n"
     "                \"     AND a.bar_ts IS NOT NULL AND a.bar_ts > 0)\"\n",
     "                \"  SELECT bar_ts FROM pending_theses)\"\n",
     "拿到「末次推进时刻」（生产里比真实挂单根晚 5 小时）→ 历史成交全跳过"),

    ("回填_取最后一次挂单", STORE,
     "  SELECT MIN(a.bar_ts) FROM pending_thesis_actions a",
     "  SELECT MAX(a.bar_ts) FROM pending_thesis_actions a",
     "重挂过的预期会拿到「最后一次」→ 锚点偏晚，中间那段的成交被跳过"),

    ("回填_连终态行也改", STORE,
     "                \"   AND state NOT IN ('CLOSED_TP', 'CLOSED_SL',\"\n"
     "                \"                     'CLOSED_THESIS', 'CANCELLED',\"\n"
     "                \"                     'EXPIRED', 'REJECTED')\")\n",
     "                \"   \")\n",
     "改历史终态行的记录 → 复盘和重放的对账单对不上"),

    ("回填_不要求kind为place_order", STORE,
     "                \"     AND a.kind = 'place_order'\"\n",
     "",
     "close/cancel 的 bar_ts 也会被拿来当锚点 → 锚点跑到成交之后，成交永不判出"),

    # ---- 超期计数 ----
    ("超期_仍用下标差", THESIS,
     "        _elapsed_coarse = None\n"
     "        if armed_bar_ts is not None and _bar_ts > 0:\n"
     "            _elapsed_coarse = (int(ts) - int(armed_bar_ts)) // 900_000\n",
     "        _elapsed_coarse = None\n",
     "退回 i-armed_at_bar → 下标滑动后差值恒 0，超期永不触发"),

    ("超期_除数写错", THESIS,
     "            _elapsed_coarse = (int(ts) - int(armed_bar_ts)) // 900_000\n",
     "            _elapsed_coarse = (int(ts) - int(armed_bar_ts)) // 300_000\n",
     "把 15m 根数按 5m 除 → 容忍窗口放大 3 倍"),

    # ---- 落库 ----
    ("落库_不写armed_bar_ts", STORE,
     "        vals += [row.get(\"state\", \"EXPECTED\"), int(row.get(\"next_index\", 0)),\n"
     "                 row.get(\"armed_at_bar\"), row.get(\"armed_at_sub_ts\"),\n"
     "                 row.get(\"armed_bar_ts\"),\n",
     "        vals += [row.get(\"state\", \"EXPECTED\"), int(row.get(\"next_index\", 0)),\n"
     "                 row.get(\"armed_at_bar\"), row.get(\"armed_at_sub_ts\"),\n"
     "                 None,\n",
     "列在 SQL 里但永远写 NULL → 重启后锚点丢失，冻结复发"),

    ("落库_不补老库列", STORE,
     "        if \"armed_bar_ts\" not in have:\n"
     "            self._conn.execute(\n"
     "                \"ALTER TABLE pending_theses ADD COLUMN armed_bar_ts INTEGER\")\n"
     "            self._conn.commit()\n",
     "",
     "生产库是 11-26 之前建的 → upsert 报「表里没这列」，下单路径每轮 ERROR"),

    ("读回_不交时间戳锚点", STORE,
     "            \"armed_bar_ts\": row.get(\"armed_bar_ts\"),\n",
     "            \"armed_bar_ts\": None,\n",
     "落库了却交不回状态机 → 每次 step 都从零开始，等于没修"),

    # ---- 引擎终态闸（部署后第一分钟撞上的尾修）----
    ("终态闸_不认本轮平仓", ENGINE,
     "                    and not float((self.pos or {}).get(\"qty\") or 0.0)\n"
     "                    and not _closed_now):\n",
     "                    and not float((self.pos or {}).get(\"qty\") or 0.0)):\n",
     "平仓后 self.pos 已空 → 合法终态被判成撒谎 → 回滚 ARMED，"
     "下一轮在空仓上再发 BUY = 反手开多（11-26 18:45 生产实证）"),

    ("终态闸_任何动作都算平仓", ENGINE,
     "            _closed_now = \"close\" in placed\n",
     "            _closed_now = bool(placed)\n",
     "挂单算数 → 理论上「只挂单没成交」也会放行终态",
     # ★ 标成等价是查过可达性之后的结论，不是懒得写测试：
     #   要区分它，需要「pos 为空 + placed 非空 + 没有 close + 落在 CLOSED_*」
     #   同时成立。而走向 CLOSED_* 的唯一路径是先 FILLED，FILLED 时
     #   `_pending_book_fill` 查不到持仓量会失败并停在那里（写测试时实测过），
     #   所以这条组合在当前代码里不可达。保留 `"close" in placed` 是因为它
     #   表达的是**契约**（终态的前提是平过仓），比 `bool(placed)` 精确。
     True),

    # ---- FREEZE-1 收尾：EXIT-1 精确锚点 fill_bar_ts ----
    ("成交锚点_不落库", STORE,
     "                 int(row.get(\"fill_bar_ts\") or 0),\n",
     "                 0,\n",
     "列在但永远写 0 → 重启后只能靠 pos.entry_ts 推断（晚一根）"),

    ("成交锚点_读回不交", STORE,
     "            \"fill_bar_ts\": int(row.get(\"fill_bar_ts\") or 0),\n",
     "            \"fill_bar_ts\": 0,\n",
     "落库了却交不回状态机 → 精确值永远用不上，补列等于白补"),

    ("成交锚点_老库不补列", STORE,
     "        if \"fill_bar_ts\" not in have:\n"
     "            self._conn.execute(\n"
     "                \"ALTER TABLE pending_theses\"\n"
     "                \" ADD COLUMN fill_bar_ts INTEGER NOT NULL DEFAULT 0\")\n"
     "            self._conn.commit()\n",
     "",
     "老库没有这一列 → upsert 直接抛「表里没这列」，下单路径每轮 ERROR"),

    ("成交锚点_推断压过落库", ENGINE,
     "                                    fill_bar_ts=(int(prog.get(\"fill_bar_ts\") or 0)\n"
     "                                                 or self._pending_fill_bar_ts(tid)))\n",
     "                                    fill_bar_ts=self._pending_fill_bar_ts(tid))\n",
     "step 里 kw 压过 p → 无条件传推断值 = 精确值永远赢不了猜测值"),

    ("成交锚点_不写回引擎", ENGINE,
     "                    fill_bar_ts=_p_fill_ts,\n",
     "                    fill_bar_ts=0,\n",
     "状态机给出精确值却丢掉 → 每轮重新推断，重启即失忆"),

    # ---- 另一个写入方（自检落终态）----
    ("落终态_抹掉锚点", SELF,
     "            armed_at_sub_ts=row.get(\"armed_at_sub_ts\"),\n"
     "            armed_bar_ts=row.get(\"armed_bar_ts\"),\n"
     "            fill_bar_ts=int(row.get(\"fill_bar_ts\") or 0),\n",
     "",
     "upsert 是 DO UPDATE，没传的列写成 0 → 自检行（最常被复盘的行）"
     "三个时间戳锚点被静默抹掉"),

    # ---- 策略改名（15m反包K）----
    ("改名_没改常量", "ai_trader/trading/engulf.py",
     "KIND_ENGULF = \"15m反包K\"\n",
     "KIND_ENGULF = \"实体反包三档限价单\"\n",
     "用户定名没落地 → 决策行/统计/订单备注全还是旧名"),
]


def _clear_pycache():
    """删掉仓库里的 __pycache__。

    ★ 不清会假报「存活」：Python 按 (mtime, size) 复用字节码，
      而变异体常与原文件同尺寸、同秒写入。
    """
    for base, dirs, _ in os.walk(ROOT):
        if "__pycache__" in dirs:
            shutil.rmtree(os.path.join(base, "__pycache__"),
                          ignore_errors=True)
            dirs.remove("__pycache__")


def _run_tests():
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    p = subprocess.run(
        [sys.executable, "-m", "pytest", *TESTS, "-q", "--no-header", "-x"],
        cwd=ROOT, env=env, capture_output=True,
        encoding="utf-8", errors="replace")
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-k", default="", help="只跑名字含该关键字的变异体")
    args = ap.parse_args()

    _clear_pycache()
    rc, out = _run_tests()
    if rc != 0:
        print("[先决条件失败] 未变异时测试就不绿，先修测试：")
        print(out[-2000:])
        return 2
    print("[基线] 测试全绿，开始变异\n")

    killed = survived = equiv = 0
    bad = []
    for row in MUTANTS:
        name, path, old, new, why = row[:5]
        is_equiv = bool(row[5]) if len(row) > 5 else False
        if args.k and args.k not in name:
            continue
        with open(path, "r", encoding="utf-8") as f:
            orig = f.read()
        if orig.count(old) != 1:
            print("  !! %-28s 原文匹配 %d 次（应为 1），跳过"
                  % (name, orig.count(old)))
            bad.append(name)
            continue
        mutated = orig.replace(old, new, 1)
        tmp = tempfile.mkdtemp()
        backup = os.path.join(tmp, os.path.basename(path))
        shutil.copy2(path, backup)
        try:
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(mutated)
            _clear_pycache()
            rc, out = _run_tests()
        finally:
            shutil.copy2(backup, path)
            _clear_pycache()
            shutil.rmtree(tmp, ignore_errors=True)
        if rc != 0:
            killed += 1
            print("  ✅ 杀死  %-28s %s" % (name, why))
        elif is_equiv:
            equiv += 1
            print("  ≈  等价  %-28s %s" % (name, why))
        else:
            survived += 1
            print("  ❌ 存活  %-28s %s" % (name, why))

    total = killed + survived + equiv + len(bad)
    print("\n" + "=" * 78)
    print("变异测试：%d 个变异体 · 杀死 %d · 存活 %d · 等价 %d · 配置错误 %d"
          % (total, killed, survived, equiv, len(bad)))
    if survived or bad:
        print("★ 存活的变异体说明那一处逻辑**没有任何测试在保护它**。")
        return 1
    print("全部杀死（等价变异体除外）—— 冻结锚点每一处都有测试兜底。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

