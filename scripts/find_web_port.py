from __future__ import annotations

import socket
import sys


def port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) != 0


def main() -> int:
    preferred = 8000
    for port in range(preferred, preferred + 10):
        if port_available(port):
            print(port)
            return 0
    print(preferred, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
