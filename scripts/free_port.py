#!/usr/bin/env python3
"""Print the first free local port at or above the one given (default 8080). Stdlib only."""

from __future__ import annotations

import socket
import sys


def free_port(start: int = 8080) -> int:
    port = start
    while port < 65536:
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                port += 1
                continue
            return port
    raise SystemExit(f"no free port at or above {start}")


if __name__ == "__main__":
    print(free_port(int(sys.argv[1]) if len(sys.argv) > 1 else 8080))
