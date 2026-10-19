"""The semula command line. A thin client over the admin API (ADR-0005).

Exit codes: 0 ok, 1 server unreachable or unexpected error, 2 usage error,
3 not found, 4 answer rejected (invalid or already answered).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from typing import Any

import httpx

EXIT_OK, EXIT_ERROR, EXIT_NOT_FOUND, EXIT_REJECTED = 0, 1, 3, 4
DEFAULT_URL = "http://127.0.0.1:8080"


def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .app import create_app
    from .config import Settings

    settings = Settings.from_env()
    host = args.host or settings.host
    if not settings.api_key and host not in ("127.0.0.1", "localhost", "::1"):
        print(
            f"warning: listening on {host} with no SEMULA_API_KEY; anyone who can reach this "
            "port can read and change recordings.",
            file=sys.stderr,
        )
    port = args.port if args.port is not None else settings.port
    uvicorn.run(create_app(settings), host=host, port=port, log_level="info")
    return EXIT_OK


def _call(args: argparse.Namespace, method: str, path: str, **kwargs: Any) -> httpx.Response:
    headers = dict(kwargs.pop("headers", {}))
    key = os.environ.get("SEMULA_API_KEY")
    if key:  # from the environment only, so the secret never appears in argv
        headers["Authorization"] = f"Bearer {key}"
    return httpx.request(
        method, args.url.rstrip("/") + path, timeout=30.0, headers=headers, **kwargs
    )


def _fail(args: argparse.Namespace, resp: httpx.Response) -> int:
    try:
        err = resp.json()["error"]
        code, message = err.get("code", "error"), err.get("message", resp.text)
        param = err.get("param")
    except (ValueError, KeyError, TypeError):
        code, message, param = "error", resp.text, None
    if args.json:
        print(
            json.dumps({"error": {"code": code, "message": message, "param": param}}),
            file=sys.stderr,
        )
    else:
        where = f" (at {param})" if param else ""
        print(f"error: {code}: {message}{where}", file=sys.stderr)
    if resp.status_code == 404:
        return EXIT_NOT_FOUND
    if resp.status_code in (409, 422):
        return EXIT_REJECTED
    return EXIT_ERROR


def _pending(args: argparse.Namespace) -> int:
    resp = _call(
        args, "GET", "/api/v1/pending", params={"status": "all" if args.all else "pending"}
    )
    if resp.status_code != 200:
        return _fail(args, resp)
    items = resp.json()["pending"]
    if args.json:
        print(json.dumps(items))
    elif not items:
        print("no pending requests")
    else:
        for p in items:
            last = p["last_message"].replace("\n", " ")[:80]
            print(f"{p['id']}\t{p['status']}\t{p['project']}\t{last}")
    return EXIT_OK


def _show(args: argparse.Namespace) -> int:
    resp = _call(args, "GET", f"/api/v1/pending/{args.id}")
    if resp.status_code != 200:
        return _fail(args, resp)
    print(json.dumps(resp.json(), indent=2))
    return EXIT_OK


def _answer(args: argparse.Namespace) -> int:
    if args.text is not None:
        payload: Any = {"content": args.text}
    else:
        raw = sys.stdin.read() if args.stdin else open(args.file, encoding="utf-8").read()  # noqa: SIM115
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            print(f"error: the answer is not valid JSON: {exc}", file=sys.stderr)
            return EXIT_REJECTED
    resp = _call(args, "POST", f"/api/v1/pending/{args.id}/answer", json=payload)
    if resp.status_code != 200:
        return _fail(args, resp)
    out = resp.json()
    print(json.dumps(out) if args.json else f"answered {args.id} -> entry {out['entry_id']}")
    return EXIT_OK


def _project_list(args: argparse.Namespace) -> int:
    resp = _call(args, "GET", "/api/v1/projects")
    if resp.status_code != 200:
        return _fail(args, resp)
    projects = resp.json()["projects"]
    if args.json:
        print(json.dumps(projects))
    else:
        for p in projects:
            print(f"{p['id']}\t{p['name']}\tentries={p['entries']}\tpending={p['pending']}")
    return EXIT_OK


def _project_create(args: argparse.Namespace) -> int:
    resp = _call(args, "POST", "/api/v1/projects", json={"id": args.id, "name": args.name})
    if resp.status_code != 201:
        return _fail(args, resp)
    print(json.dumps(resp.json()) if args.json else f"created project {args.id}")
    return EXIT_OK


def _project_delete(args: argparse.Namespace) -> int:
    resp = _call(args, "DELETE", f"/api/v1/projects/{args.id}")
    if resp.status_code != 204:
        return _fail(args, resp)
    print(json.dumps({"deleted": args.id}) if args.json else f"deleted project {args.id}")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="semula", description="Record-replay fake LLM server.")
    parser.add_argument(
        "--url", default=os.environ.get("SEMULA_URL", DEFAULT_URL), help="server URL (SEMULA_URL)"
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="Run the server")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.set_defaults(func=_serve)

    pending = sub.add_parser("pending", help="List parked requests waiting for an answer")
    pending.add_argument("--all", action="store_true", help="include answered requests")
    pending.set_defaults(func=_pending)

    show = sub.add_parser("show", help="Show one pending request in full")
    show.add_argument("id")
    show.set_defaults(func=_show)

    answer = sub.add_parser("answer", help="Answer a pending request, releasing the held call")
    answer.add_argument("id")
    source = answer.add_mutually_exclusive_group(required=True)
    source.add_argument("--text", help="reply with this plain text")
    source.add_argument("--file", help="read a JSON answer from a file")
    source.add_argument("--stdin", action="store_true", help="read a JSON answer from stdin")
    answer.set_defaults(func=_answer)

    project = sub.add_parser("project", help="Manage projects")
    psub = project.add_subparsers(dest="project_command", required=True)
    plist = psub.add_parser("list", help="List projects")
    plist.set_defaults(func=_project_list)
    pcreate = psub.add_parser("create", help="Create a project")
    pcreate.add_argument("id", help="slug: lowercase letters, digits, hyphens")
    pcreate.add_argument("--name", default=None)
    pcreate.set_defaults(func=_project_create)
    pdelete = psub.add_parser("delete", help="Delete a project and all its entries")
    pdelete.add_argument("id")
    pdelete.set_defaults(func=_project_delete)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except httpx.ConnectError:
        print(f"error: cannot reach semula at {args.url}. Is it running?", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
