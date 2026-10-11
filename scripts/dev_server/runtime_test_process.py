"""native試験だけで使う自己終了型Python親子。Minecraftや既存サーバーを起動しない。"""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time


def main():
    root = Path(sys.argv[2])
    child = sys.argv[1] == "child"
    tcp = socket.socket()
    udp = socket.socket(type=socket.SOCK_DGRAM)
    tcp.bind(("127.0.0.1", 0))
    tcp.listen()
    udp.bind(("127.0.0.1", 0))
    (root / ("child.json" if child else "parent.json")).write_text(
        json.dumps({"pid": os.getpid(), "tcp": tcp.getsockname()[1], "udp": udp.getsockname()[1]}))
    if not child:
        subprocess.Popen([sys.argv[3], "-B", __file__, "child", str(root), sys.argv[3]], stdin=subprocess.DEVNULL)
    deadline = time.monotonic() + 15
    while not (root / ("child-exit" if child else "parent-exit")).exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    tcp.close()
    udp.close()


if __name__ == "__main__":
    main()
