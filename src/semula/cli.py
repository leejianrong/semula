"""The semula command line. A thin client over the admin API (ADR-0005).

Every command takes ``--json`` (before or after the command name) and then prints
exactly one JSON document to stdout. Errors always go to stderr; with ``--json`` they
are a JSON ``{"error": {...}}`` document.

Exit codes
    0  ok
    1  server unreachable, or an unexpected server error
    2  usage error (bad flags)
    3  not found
    4  rejected: invalid input, a version conflict, or a pinned entry
    5  authentication failed (set SEMULA_API_KEY)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from typing import Any

import httpx

EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_NOT_FOUND, EXIT_REJECTED, EXIT_AUTH = 0, 1, 2, 3, 4, 5
DEFAULT_URL = "http://127.0.0.1:8080"


# --- plumbing ---------------------------------------------------------------------------
def _call(args: argparse.Namespace, method: str, path: str, **kwargs: Any) -> httpx.Response:
    headers = dict(kwargs.pop("headers", {}))
    key = os.environ.get("SEMULA_API_KEY")
    if key:  # from the environment only, so the secret never appears in argv
        headers["Authorization"] = f"Bearer {key}"
    if getattr(args, "as_human", False):
        headers["X-Semula-Actor"] = "human"
    return httpx.request(
        method, args.url.rstrip("/") + path, timeout=30.0, headers=headers, **kwargs
    )


def _fail(args: argparse.Namespace, resp: httpx.Response) -> int:
    try:
        err = resp.json()["error"]
        code, message, param = (
            err.get("code", "error"),
            err.get("message", resp.text),
            err.get("param"),
        )
    except (ValueError, KeyError, TypeError):
        code, message, param = "error", resp.text, None
    if args.json:
        error: dict[str, Any] = {"code": code, "message": message, "param": param}
        error["status"] = resp.status_code
        try:
            for key, value in resp.json().items():
                if key != "error":
                    error[key] = value  # e.g. "current" on a 409, "nearest" on a miss
        except (ValueError, AttributeError):
            pass
        print(json.dumps({"error": error}), file=sys.stderr)
    else:
        where = f" (at {param})" if param else ""
        print(f"error: {code}: {message}{where}", file=sys.stderr)
    return {401: EXIT_AUTH, 404: EXIT_NOT_FOUND, 400: EXIT_REJECTED, 409: EXIT_REJECTED}.get(
        resp.status_code, EXIT_REJECTED if resp.status_code == 422 else EXIT_ERROR
    )


def _ok(args: argparse.Namespace, data: Any, human: str) -> int:
    print(json.dumps(data) if args.json else human)
    return EXIT_OK


def _payload(args: argparse.Namespace) -> Any:
    if getattr(args, "text", None) is not None:
        return {"content": args.text}
    raw = sys.stdin.read() if args.stdin else open(args.file, encoding="utf-8").read()  # noqa: SIM115
    return json.loads(raw)


def _bad_json(exc: ValueError) -> int:
    print(f"error: the input is not valid JSON: {exc}", file=sys.stderr)
    return EXIT_REJECTED


# --- commands ---------------------------------------------------------------------------
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


def _pending(args: argparse.Namespace) -> int:
    params = {"status": "all" if args.all else "pending"}
    if args.project:
        params["project"] = args.project
    resp = _call(args, "GET", "/api/v1/pending", params=params)
    if resp.status_code != 200:
        return _fail(args, resp)
    items = resp.json()["pending"]
    lines = [
        f"{p['id']}\t{p['status']}\t{p['project']}\t{p['last_message'].replace(chr(10), ' ')[:80]}"
        for p in items
    ]
    return _ok(args, items, "\n".join(lines) if lines else "no pending requests")


def _show(args: argparse.Namespace) -> int:
    resp = _call(args, "GET", f"/api/v1/pending/{args.id}")
    if resp.status_code != 200:
        return _fail(args, resp)
    return _ok(args, resp.json(), json.dumps(resp.json(), indent=2))


def _answer(args: argparse.Namespace) -> int:
    try:
        payload = _payload(args)
    except ValueError as exc:
        return _bad_json(exc)
    resp = _call(args, "POST", f"/api/v1/pending/{args.id}/answer", json=payload)
    if resp.status_code != 200:
        return _fail(args, resp)
    out = resp.json()
    return _ok(args, out, f"answered {args.id} -> entry {out['entry_id']}")


def _project_list(args: argparse.Namespace) -> int:
    resp = _call(args, "GET", "/api/v1/projects")
    if resp.status_code != 200:
        return _fail(args, resp)
    projects = resp.json()["projects"]
    lines = [
        f"{p['id']}\t{p['name']}\tentries={p['entries']}\tpending={p['pending']}" for p in projects
    ]
    return _ok(args, projects, "\n".join(lines))


def _project_create(args: argparse.Namespace) -> int:
    resp = _call(args, "POST", "/api/v1/projects", json={"id": args.id, "name": args.name})
    if resp.status_code != 201:
        return _fail(args, resp)
    return _ok(args, resp.json(), f"created project {args.id}")


def _project_delete(args: argparse.Namespace) -> int:
    resp = _call(args, "DELETE", f"/api/v1/projects/{args.id}")
    if resp.status_code != 204:
        return _fail(args, resp)
    return _ok(args, {"deleted": args.id}, f"deleted project {args.id}")


def _project_settings(args: argparse.Namespace) -> int:
    if args.set is None:
        resp = _call(args, "GET", f"/api/v1/projects/{args.id}")
    else:
        try:
            settings = json.loads(args.set)
        except ValueError as exc:
            return _bad_json(exc)
        resp = _call(args, "PATCH", f"/api/v1/projects/{args.id}", json={"settings": settings})
    if resp.status_code != 200:
        return _fail(args, resp)
    settings = resp.json()["settings"]
    return _ok(args, settings, json.dumps(settings, indent=2))


def _entry_list(args: argparse.Namespace) -> int:
    params: dict[str, Any] = {"limit": args.limit, "offset": args.offset}
    if args.q:
        params["q"] = args.q
    resp = _call(args, "GET", f"/api/v1/projects/{args.project}/entries", params=params)
    if resp.status_code != 200:
        return _fail(args, resp)
    body = resp.json()
    lines = [
        f"{e['id']}\tv{e['version']}\t{'pinned' if e['pinned'] else '-'}\thits={e['hit_count']}"
        f"\t{e['last_message'].replace(chr(10), ' ')[:70]}"
        for e in body["entries"]
    ]
    lines.append(f"({len(body['entries'])} of {body['total']})")
    return _ok(args, body, "\n".join(lines))


def _entry_show(args: argparse.Namespace) -> int:
    resp = _call(args, "GET", f"/api/v1/entries/{args.id}")
    if resp.status_code != 200:
        return _fail(args, resp)
    return _ok(args, resp.json(), json.dumps(resp.json(), indent=2))


def _entry_set(args: argparse.Namespace) -> int:
    changes: dict[str, Any] = {"version": args.version}
    if args.text is not None or args.file or args.stdin:
        try:
            answer = _payload(args)
        except ValueError as exc:
            return _bad_json(exc)
        changes["add_response" if args.add else "response"] = answer
    if args.selection:
        changes["selection"] = args.selection
    if args.pin:
        changes["pinned"] = True
    if args.unpin:
        changes["pinned"] = False
    if args.force:
        changes["force"] = True
    resp = _call(args, "PUT", f"/api/v1/entries/{args.id}", json=changes)
    if resp.status_code != 200:
        return _fail(args, resp)
    out = resp.json()
    return _ok(args, out, f"updated {args.id} -> version {out['version']}")


def _entry_delete(args: argparse.Namespace) -> int:
    params: dict[str, Any] = {"version": args.version}
    if args.force:
        params["force"] = "true"
    resp = _call(args, "DELETE", f"/api/v1/entries/{args.id}", params=params)
    if resp.status_code != 204:
        return _fail(args, resp)
    return _ok(args, {"deleted": args.id}, f"deleted entry {args.id}")


def _mode(args: argparse.Namespace) -> int:
    resp = (
        _call(args, "GET", "/api/v1/mode")
        if args.mode is None
        else _call(args, "PUT", "/api/v1/mode", json={"mode": args.mode})
    )
    if resp.status_code != 200:
        return _fail(args, resp)
    return _ok(args, resp.json(), resp.json()["mode"])


def _test_match(args: argparse.Namespace) -> int:
    if args.prompt is not None:
        body: Any = {"prompt": args.prompt}
    else:
        try:
            body = {"request": _payload(args)}
        except ValueError as exc:
            return _bad_json(exc)
    resp = _call(args, "POST", f"/api/v1/projects/{args.project}/test-match", json=body)
    if resp.status_code != 200:
        return _fail(args, resp)
    out = resp.json()
    if out["matched"]:
        human = f"match: {out['tier']} -> entry {out['entry_id']} (would {out['would']})"
    else:
        near = out["nearest"]
        hint = f"; nearest {near['entry_id']} score {near['score']}" if near else ""
        human = f"no match (would {out['would']}){hint}"
    return _ok(args, out, human)


def _mcp(args: argparse.Namespace) -> int:
    from .mcp_server import run_stdio_proxy

    # Fail fast and loudly: a stdio MCP client otherwise just sees a silent dead server.
    health = _call(args, "GET", "/api/v1/mode")  # an authenticated route, so a bad key shows
    if health.status_code != 200:
        return _fail(args, health)

    run_stdio_proxy(args.url, os.environ.get("SEMULA_API_KEY"))
    return EXIT_OK


# --- parser -----------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="semula",
        description="Record-replay fake LLM server.",
        epilog=(__doc__ or "").split("Exit codes", 1)[-1].strip(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--url", default=os.environ.get("SEMULA_URL", DEFAULT_URL), help="server URL (SEMULA_URL)"
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--json", action="store_true", default=argparse.SUPPRESS, help="machine-readable output"
    )

    def add(group: Any, name: str, func: Any, help_: str) -> argparse.ArgumentParser:
        p: argparse.ArgumentParser = group.add_parser(name, help=help_, parents=[common])
        p.set_defaults(func=func)
        return p

    serve = add(sub, "serve", _serve, "Run the server")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)

    add(sub, "mcp", _mcp, "Run an MCP server on stdio that talks to --url")

    pending = add(sub, "pending", _pending, "List parked requests waiting for an answer")
    pending.add_argument("--all", action="store_true", help="include answered requests")
    pending.add_argument("--project", default=None)

    add(sub, "show", _show, "Show one pending request in full").add_argument("id")

    answer = add(sub, "answer", _answer, "Answer a pending request, releasing the held call")
    answer.add_argument("id")
    source = answer.add_mutually_exclusive_group(required=True)
    source.add_argument("--text", help="reply with this plain text")
    source.add_argument("--file", help="read a JSON answer from a file")
    source.add_argument("--stdin", action="store_true", help="read a JSON answer from stdin")

    project = sub.add_parser("project", help="Manage projects")
    psub = project.add_subparsers(dest="project_command", required=True)
    add(psub, "list", _project_list, "List projects")
    pcreate = add(psub, "create", _project_create, "Create a project")
    pcreate.add_argument("id", help="slug: lowercase letters, digits, hyphens")
    pcreate.add_argument("--name", default=None)
    add(psub, "delete", _project_delete, "Delete a project and all its entries").add_argument("id")
    pset = add(psub, "settings", _project_settings, "Show or patch matching settings")
    pset.add_argument("id")
    pset.add_argument("--set", default=None, help="JSON patch, e.g. '{\"tail_n\": 2}'; null resets")

    entry = sub.add_parser("entry", help="Inspect and edit recorded entries")
    esub = entry.add_subparsers(dest="entry_command", required=True)
    elist = add(esub, "list", _entry_list, "List a project's entries (no bodies)")
    elist.add_argument("project")
    elist.add_argument("--q", default=None, help="text filter")
    elist.add_argument("--limit", type=int, default=50)
    elist.add_argument("--offset", type=int, default=0)
    add(esub, "show", _entry_show, "Show one entry in full").add_argument("id")
    eset = add(esub, "set", _entry_set, "Edit an entry (needs its current --version)")
    eset.add_argument("id")
    eset.add_argument("--version", type=int, required=True)
    eset.add_argument("--text", default=None, help="plain-text response")
    eset.add_argument("--file", default=None, help="JSON response from a file")
    eset.add_argument("--stdin", action="store_true", help="JSON response from stdin")
    eset.add_argument("--add", action="store_true", help="add as another response, not replace")
    eset.add_argument("--selection", choices=["first", "sequence", "cycle"])
    eset.add_argument("--pin", action="store_true")
    eset.add_argument("--unpin", action="store_true")
    eset.add_argument("--force", action="store_true", help="override a pin")
    eset.add_argument("--as-human", action="store_true", help="write as a human, not an agent")
    edel = add(esub, "delete", _entry_delete, "Delete an entry (needs its current --version)")
    edel.add_argument("id")
    edel.add_argument("--version", type=int, required=True)
    edel.add_argument("--force", action="store_true")
    edel.add_argument("--as-human", action="store_true")

    mode = add(sub, "mode", _mode, "Show or switch the mode (replay or record)")
    mode.add_argument("mode", nargs="?", choices=["replay", "record"])

    tm = add(sub, "test-match", _test_match, "Which entry would fire for this prompt, and why")
    tm.add_argument("--project", default="default")
    src = tm.add_mutually_exclusive_group(required=True)
    src.add_argument("--prompt", help="a single user message")
    src.add_argument("--file", help="a chat completion request body (JSON file)")
    src.add_argument("--stdin", action="store_true", help="a chat completion body on stdin")
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
