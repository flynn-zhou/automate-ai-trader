# -*- coding: utf-8 -*-
"""健康自检的故障注入台：验证那个「从来没响过的监控」真的会响。

为什么需要它：
  deploy/healthcheck.sh 上线以来每次都说 OK。一个从来没响过的监控，
  可能是工作正常，也可能是它根本是瞎的——两者在日志里长得一模一样。
  脚本自己写着「哨兵是瞎的，比没有更危险」。

测两条真正致命的路径：
  1. 进程不在了       -> 能不能自己拉起来（2026-11-02 已验证）
  2. 进程活着但不干活  -> 会不会真报「数据停滞」、计数会不会累加、
                         重启节流会不会生效、告警会不会落进研究日志
                         （这条以前从没被触发过）

做法：完全不碰线上库。把两个库复制到隔离目录、时间戳整体平移 20 分钟，
把脚本里的 DEST 换成隔离目录，并在该目录软链 .venv/bin/python 和 ai_trader 包
（$PY 和 import 都依赖 DEST，漏了任何一条，测到的都会是「路径缺失」而非「停滞」）。
计数文件只在建台时预置一次 LAST_RESTART=now，保证 30 分钟节流生效、
实验绝不会重启线上服务。

用法（服务器上）：
    cd /home/cryptobot/ai-trader && .venv/bin/python tools/hcfault.py
"""
import os
import shutil
import sqlite3
import subprocess
import sys
import time

REAL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
T = '/tmp/hc_fault_test'
SHIFT_MS = 1200000          # 时间戳整体推老 20 分钟
RUNS = 4


def build():
    shutil.rmtree(T, ignore_errors=True)
    for d in ('data', 'logs', '.venv/bin'):
        os.makedirs(os.path.join(T, d))
    os.symlink(os.path.join(REAL, '.venv/bin/python'),
               os.path.join(T, '.venv/bin/python'))
    os.symlink(os.path.join(REAL, 'ai_trader'), os.path.join(T, 'ai_trader'))
    for name in ('market.db', 'market_trade.db'):
        shutil.copy2(os.path.join(REAL, 'data', name),
                     os.path.join(T, 'data', name))
    # bars 有 UNIQUE(interval_ms, open_time)：整体平移不撞，组内相对关系不变。
    c = sqlite3.connect(os.path.join(T, 'data/market.db'))
    c.execute('UPDATE bars SET open_time = open_time - ?'
              ' WHERE interval_ms = 3000', (SHIFT_MS,))
    c.commit()
    c.close()
    c = sqlite3.connect(os.path.join(T, 'data/market_trade.db'))
    c.execute('UPDATE decisions SET ts = ts - ?', (SHIFT_MS,))
    c.commit()
    c.close()
    # 以【二进制】读原脚本，绝不让文本模式的自动换行规范化掺进来。
    # 2026-11-02 事故 5：我用编辑工具把脚本改成了 CRLF，推上服务器后
    # 那个每 5 分钟跑一次的健康自检直接崩了（command not found + 语法错）。
    # 而当时这个工具用文本模式读脚本：读的时候 CRLF 被悄悄换成 LF，
    # 于是它跑得通、还报「全部通过」——验证工具修好了它正在验证的东西。
    # 现在先验字节：有 CR 就是部署事故，直接失败，不许自愈。
    raw = open(os.path.join(REAL, 'deploy/healthcheck.sh'), 'rb').read()
    crs = raw.count(bytes([13]))
    assert crs == 0, (
        'deploy/healthcheck.sh 有 %d 个 CR 字节（CRLF 行尾）。'
        '这种脚本在 Linux 上根本跑不起来，先修文件，不要绕过。' % crs)
    src = raw.decode('utf-8')
    mut = src.replace('DEST=/home/cryptobot/ai-trader', 'DEST=' + T, 1)
    assert mut != src, 'DEST 替换失败，拒绝在错误目标上做实验'
    with open(os.path.join(T, 'hc.sh'), 'w', encoding='utf-8',
              newline=chr(10)) as fh:
        fh.write(mut)
    # 计数文件只在建台时写一次。
    # 第一版我在每次运行前重写它，计数永远回到 0、永远累加不起来——
    # 实验自己把要验证的东西擦掉了，还会显示成「计数逻辑坏了」。
    with open(os.path.join(T, 'logs/.stale_state'), 'w') as f:
        f.write('0 %d' % int(time.time()))


def run_once():
    # 不重置计数文件：计数必须能跨调用累加，否则测不到节流。
    r = subprocess.run(['bash', os.path.join(T, 'hc.sh')],
                       capture_output=True, text=True, timeout=300)
    with open(os.path.join(T, 'logs/.stale_state')) as f:
        cnt = f.read().strip()
    return r.returncode, (r.stdout + r.stderr).strip(), cnt


def main():
    build()
    print('隔离库就绪，时间戳整体推老 %d 分钟' % (SHIFT_MS // 60000))
    print()
    seen_stale = False
    counts = []
    outs = []
    for i in range(1, RUNS + 1):
        code, out, cnt = run_once()
        counts.append(int(cnt.split()[0]))
        outs.append(out)
        print('第 %d 次  exit=%d  计数=%s' % (i, code, cnt))
        print('   %s' % out.replace(chr(10), ' | ')[:230])
        if '停滞' in out:
            seen_stale = True

    c = sqlite3.connect(os.path.join(T, 'data/market_trade.db'))
    alerts = c.execute('SELECT COUNT(*) FROM journal').fetchone()[0]
    last = c.execute('SELECT kind, title, body FROM journal'
                     ' ORDER BY id DESC LIMIT 1').fetchone()
    c.close()
    print()
    print('隔离库 journal 行数:', alerts)
    if last:
        print('最新一行:', last[0], '|', last[1], '|', str(last[2])[:70])

    online = subprocess.run(['systemctl', 'is-active', 'ai-trader'],
                            capture_output=True, text=True).stdout.strip()
    healed_lie = any(('已自愈' in o) and ('未重启' not in o) for o in outs)
    # 语法自检：脚本若是坏的（比如缺个 fi），前面那些「检测不到停滞」
    # 会被误读成脚本没问题。先确认它自己能被 bash 解析。
    # 注意：这个变量必须在下面的 checks 列表【之前】算好。
    syn = subprocess.run(['bash', '-n', os.path.join(REAL, 'deploy/healthcheck.sh')],
                         capture_output=True, text=True)
    checks = [
        ('数据停滞被检测到', seen_stale),
        ('计数逐次累加', counts == list(range(1, RUNS + 1))),
        ('节流期间没重启（计数越过 3 仍在涨）', counts[-1] > 3),
        ('数据停滞时不谎称已自愈', not healed_lie),
        ('告警落进了研究日志', alerts > 0),
        ('线上服务未被实验波及', online == 'active'),
        ('脚本本身语法正确(bash -n)', syn.returncode == 0),
    ]
    print()
    print('线上服务状态:', online)
    ok = True
    for name, good in checks:
        print('  [%s] %s' % ('OK' if good else '失败', name))
        ok = ok and good
    shutil.rmtree(T, ignore_errors=True)
    print()
    print('结论:', '全部通过' if ok else '有检查未通过')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())