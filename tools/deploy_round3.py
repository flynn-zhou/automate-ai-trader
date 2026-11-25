# -*- coding: utf-8 -*-
"""把本轮改动传到生产，并做双向 md5 校验。

只上传我本轮改过的文件；不改动、不删除任何其它文件。
用法：python tools/deploy_round3.py
"""
import hashlib
import io
import os
import subprocess
import sys

KEY = r"D:\workspace\automate-k-trader.pem"
PORT = "2222"
HOST = "root@127.0.0.1"
REMOTE = "/home/cryptobot/ai-trader"

FILES = [
    "ai_trader/trading/engine.py",
    "ai_trader/trade_db.py",
    "ai_trader/web/app.py",
    "ai_trader/web/templates/dash_base.html",
    "ai_trader/web/templates/dash_decisions.html",
]

SSH = ["ssh", "-p", PORT, "-i", KEY, "-o", "StrictHostKeyChecking=no",
       "-o", "BatchMode=yes", HOST]
SCP = ["scp", "-P", PORT, "-i", KEY, "-o", "StrictHostKeyChecking=no",
       "-o", "BatchMode=yes"]


def md5(b):
    return hashlib.md5(b).hexdigest()


def run(cmd, capture=True):
    p = subprocess.run(cmd, stdout=subprocess.PIPE if capture else None,
                       stderr=subprocess.STDOUT)
    return p.returncode, (p.stdout or b"").decode("utf-8", "replace")


def main():
    ok = True
    for rel in FILES:
        local = rel.replace("/", os.sep)
        if not os.path.exists(local):
            print("[MISS] 本地不存在:", rel)
            ok = False
            continue
        local_md5 = md5(io.open(local, "rb").read())
        rc, out = run(SCP + [local, "%s:%s/%s" % (HOST, REMOTE, rel)])
        if rc != 0:
            print("[FAIL] 上传失败 %s\n%s" % (rel, out))
            ok = False
            continue
        # ★ 必须 `cd` 到仓库目录再算：ssh 的默认 cwd 是 root 家目录，
        #   直接 `md5sum ai_trader/...` 会得到 "No such file"，
        #   而错误信息的第一段恰好长得像哈希值 —— 校验就变成了瞎的。
        rc, out = run(SSH + ["cd %s && md5sum %s" % (REMOTE, rel)])
        remote_md5 = (out.split() or [""])[0]
        same = (remote_md5 == local_md5)
        print("[%s] %-52s local=%s remote=%s"
              % ("OK" if same else "MISMATCH", rel, local_md5, remote_md5))
        if not same:
            ok = False

    # 语法检查（在远端跑编译，避免"传上去但语法错"导致服务起不来）
    rc, out = run(SSH + [
        "cd %s && .venv/bin/python -m py_compile "
        "ai_trader/trading/engine.py ai_trader/trade_db.py ai_trader/web/app.py "
        "&& echo COMPILE_OK" % REMOTE])
    print("[compile]", out.strip()[-300:])
    if "COMPILE_OK" not in out:
        ok = False

    print("\n=== 结果:", "全部一致" if ok else "有问题，不要重启 ===")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
