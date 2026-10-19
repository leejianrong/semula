"""The semula command line. A thin client over the admin API (ADR-0005)."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence


def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .app import create_app
    from .config import Settings

    settings = Settings.from_env()
    host = args.host or settings.host
    port = args.port if args.port is not None else settings.port
    uvicorn.run(create_app(settings), host=host, port=port, log_level="info")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="semula", description="Record-replay fake LLM server.")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="Run the server")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.set_defaults(func=_serve)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
