"""本地 TCP 端口转发：经 clash 代理 (127.0.0.1:4780) CONNECT 隧道转发到目标。

用法:
    python tools\\tcp_tunnel.py <local_port> <target_host> <target_port>
    ssh -i key -p <local_port> root@127.0.0.1

原理: 本地监听 → 每来一个连接，经代理建 CONNECT 隧道到目标 → 双线程双向转发
（纯 socket 转发，不经过 ssh 的 ProxyCommand stdio，规避 Windows 上的握手卡顿）。
"""
from __future__ import annotations

import socket
import sys
import threading

PROXY_HOST = "127.0.0.1"
PROXY_PORT = 4780


def _relay(a: socket.socket, b: socket.socket) -> None:
    """a → b 单向转发；a 到达 EOF 时只关闭 b 的写侧（半关闭），让对端正常收尾。"""
    try:
        while True:
            data = a.recv(65536)
            if not data:
                break
            b.sendall(data)
    except OSError:
        pass
    finally:
        try:
            b.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def _handle(client: socket.socket, target_host: str, target_port: int) -> None:
    try:
        proxy = socket.create_connection((PROXY_HOST, PROXY_PORT), timeout=15)
        proxy.sendall(
            f"CONNECT {target_host}:{target_port} HTTP/1.1\r\n"
            f"Host: {target_host}:{target_port}\r\n\r\n".encode()
        )
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = proxy.recv(4096)
            if not chunk:
                return
            buf += chunk
        if not buf.startswith(b"HTTP/1.1 200"):
            return
        # 残留字节（如 sshd 横幅）先发给客户端
        head, _, rest = buf.partition(b"\r\n\r\n")
        if rest:
            client.sendall(rest)
        t1 = threading.Thread(target=_relay, args=(client, proxy), daemon=True)
        t2 = threading.Thread(target=_relay, args=(proxy, client), daemon=True)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
    except OSError:
        pass
    finally:
        try:
            client.close()
        except OSError:
            pass


def main() -> None:
    if len(sys.argv) < 4:
        print("usage: tcp_tunnel.py <local_port> <target_host> <target_port>", file=sys.stderr)
        sys.exit(1)
    local_port, target_host, target_port = int(sys.argv[1]), sys.argv[2], int(sys.argv[3])

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", local_port))
    listener.listen(16)
    print(f"隧道就绪: 127.0.0.1:{local_port} -> {target_host}:{target_port} (经 clash)", flush=True)
    while True:
        client, _ = listener.accept()
        threading.Thread(
            target=_handle, args=(client, target_host, target_port), daemon=True
        ).start()


if __name__ == "__main__":
    main()