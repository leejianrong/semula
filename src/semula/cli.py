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
from typing import TYPE_CHECKING, Any

from . import __version__

if TYPE_CHECKING:  # httpx is only imported by commands that talk to a server: `serve` starts faster
    import httpx

EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_NOT_FOUND, EXIT_REJECTED, EXIT_AUTH = 0, 1, 2, 3, 4, 5
DEFAULT_URL = "http://127.0.0.1:8080"


# --- plumbing ---------------------------------------------------------------------------
def _call(args: argparse.Namespace, method: str, path: str, **kwargs: Any) -> httpx.Response:
    import httpx

    headers = dict(kwargs.pop("headers", {}))
    key = os.environ.get("SEMULA_API_KEY")
    if key:  # from the environment only, so the secret never appears in argv
        headers["Authorization"] = f"Bearer {key}"
    if getattr(args, "as_human", False):
        headers["X-Semula-Actor"] = "human"
    return httpx.request(
        method, args.url.rstrip("/") + path, headers=headers, **{"timeout": 30.0, **kwargs}
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
NOT_IN_BINARY = (
    "error: `semula {name}` is not in this build (the standalone binary has the HTTP commands "
    "only). {hint}"
)


def _left_out(name: str, hint: str) -> int:
    """The standalone binary leaves the server library out; say where the command lives."""
    print(NOT_IN_BINARY.format(name=name, hint=hint), file=sys.stderr)
    return EXIT_USAGE


SERVER_HINT = "Run the container (docker run ghcr.io/leejianrong/semula) or `pip install semula`."


def _serve(args: argparse.Namespace) -> int:
    try:
        import uvicorn

        from .app import create_app
        from .config import Settings
    except ImportError:
        return _left_out("serve", SERVER_HINT)

    settings = Settings.from_env()
    host = args.host or settings.host
    loopback = host in ("127.0.0.1", "localhost", "::1")
    if not settings.api_key and not settings.hosted and not loopback:
        print(
            f"warning: listening on {host} with no SEMULA_API_KEY; anyone who can reach this "
            "port can read and change recordings.",
            file=sys.stderr,
        )
    port = args.port if args.port is not None else settings.port
    from .db import DatabaseTooNew
    from .embeddings import EmbeddingsUnavailable

    try:
        app = create_app(settings)
    except (EmbeddingsUnavailable, DatabaseTooNew) as exc:  # cannot run: say why, not a traceback
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    # proxy_headers off: uvicorn would trust X-Forwarded-For from loopback only; semula decides
    # for itself, from SEMULA_TRUSTED_PROXIES (proxy.py)
    uvicorn.run(app, host=host, port=port, log_level="info", proxy_headers=False)
    return EXIT_OK


def _pending(args: argparse.Namespace) -> int:
    params = {"status": "all" if args.all else "pending"}
    if args.project:
        params["project"] = args.project
    if args.wait is not None:
        params["wait"] = str(min(args.wait, 60))
    resp = _call(args, "GET", "/api/v1/pending", params=params, timeout=(args.wait or 0) + 10)
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


def _conversations(args: argparse.Namespace) -> int:
    params = {"source": "log" if args.log else "pending", "min_size": str(args.min_size)}
    if args.project:
        params["project"] = args.project
    resp = _call(args, "GET", "/api/v1/conversations", params=params)
    if resp.status_code != 200:
        return _fail(args, resp)
    groups = resp.json()["conversations"]
    lines: list[str] = []
    for group in groups:
        lines.append(f"{group['id']}\t{group['project']}\t{group['size']} calls")
        for call in group["calls"]:
            added = "; ".join(
                f"{m.get('role', '')}: {_one_line(m.get('content'))}" for m in call["added"][-3:]
            )
            mark = "=" if call["repeat"] else "+"
            lines.append(f"  {'  ' * (call['depth'] - 1)}{call['id']}\t{mark}{added[:100]}")
    return _ok(args, groups, "\n".join(lines) if lines else "no conversations")


def _one_line(content: Any) -> str:
    text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    return text.replace("\n", " ")[:60]


def _claim(args: argparse.Namespace) -> int:
    body: dict[str, Any] = {"lease_seconds": args.lease}
    if args.project:
        body["project"] = args.project
    if args.worker:
        body["worker"] = args.worker
    resp = _call(args, "POST", "/api/v1/pending/claim", json=body)
    if resp.status_code != 200:
        return _fail(args, resp)
    claimed = resp.json()["pending"]
    if claimed is None:
        return _ok(args, None, "nothing to claim")
    return _ok(args, claimed, json.dumps(claimed, indent=2))


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


def _project_upstream(args: argparse.Namespace) -> int:
    if args.reset:
        resp = _call(args, "POST", f"/api/v1/projects/{args.id}/upstream/reset")
    else:
        resp = _call(args, "GET", f"/api/v1/projects/{args.id}/upstream")
    if resp.status_code != 200:
        return _fail(args, resp)
    out = resp.json()
    tokens = out["prompt_tokens"] + out["completion_tokens"]
    lines = [
        f"{args.id}: {out['calls']} of {out['max_calls']} upstream calls, "
        f"{tokens} tokens"
        + (f" of {out['max_tokens']}" if out["max_tokens"] is not None else "")
        + ("  (dry run: nothing is forwarded)" if out["dry_run"] else "")
    ]
    lines += [f"would forward x{m['count']}: {m['prompt']}" for m in out.get("would_forward", [])]
    return _ok(args, out, "\n".join(lines))


def _entry_list(args: argparse.Namespace) -> int:
    params: dict[str, Any] = {"limit": args.limit, "offset": args.offset}
    if args.q:
        params["q"] = args.q
    for flag in ("pinned", "has_alias", "tool_calls", "never_hit"):
        value = getattr(args, flag)
        if value is not None:
            params[flag] = str(value).lower()
    for key in ("sort", "order"):
        if getattr(args, key):
            params[key] = getattr(args, key)
    resp = _call(args, "GET", f"/api/v1/projects/{args.project}/entries", params=params)
    if resp.status_code != 200:
        return _fail(args, resp)
    body = resp.json()
    lines = [
        f"{e['id']}\tv{e['version']}\t{'pinned' if e['pinned'] else '-'}\thits={e['hit_count']}"
        f"\t{'tools' if e['has_tools'] else '-'}\taliases={e['alias_count']}"
        f"\t{e['last_message'].replace(chr(10), ' ')[:70]}"
        for e in body["entries"]
    ]
    lines.append(f"({len(body['entries'])} of {body['total']})")
    return _ok(args, body, "\n".join(lines))


def _entry_history(args: argparse.Namespace) -> int:
    params = {"version": args.version} if args.version is not None else None
    resp = _call(args, "GET", f"/api/v1/entries/{args.id}/history", params=params)
    if resp.status_code != 200:
        return _fail(args, resp)
    out = resp.json()
    if args.version is not None:
        rev = out["revision"]
        snap = rev["snapshot"]
        answers = "\n".join(
            f"  [{i}] {json.dumps(r['message'].get('content') or r['message'].get('tool_calls'))}"
            for i, r in enumerate(snap["responses"])
        )
        human = (
            f"v{rev['version']}  {rev['ts']}  {rev['actor']}  {rev['summary']}"
            f"{'  (forced)' if rev['forced'] else ''}\n"
            f"selection {snap['selection']}, {'pinned' if snap['pinned'] else 'not pinned'}\n"
            f"{answers}"
        )
    else:
        human = "\n".join(
            f"v{r['version']}\t{r['ts']}\t{r['actor']}\t{r['summary']}"
            f"{' (forced)' if r['forced'] else ''}"
            for r in out["revisions"]
        )
    return _ok(args, out, human)


def _entry_show(args: argparse.Namespace) -> int:
    resp = _call(args, "GET", f"/api/v1/entries/{args.id}")
    if resp.status_code != 200:
        return _fail(args, resp)
    return _ok(args, resp.json(), json.dumps(resp.json(), indent=2))


def _entry_add(args: argparse.Namespace) -> int:
    try:
        if args.request_file:
            with open(args.request_file, encoding="utf-8") as fh:
                request = json.load(fh)
        else:
            messages = [{"role": "user", "content": args.prompt}]
            if args.system:
                messages.insert(0, {"role": "system", "content": args.system})
            request = {"messages": messages}
        if args.model:
            request["model"] = args.model
        if args.response_file:
            with open(args.response_file, encoding="utf-8") as fh:
                answer: Any = json.load(fh)
        else:
            answer = _payload(args)
    except ValueError as exc:
        return _bad_json(exc)
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    body: dict[str, Any] = {"request": request, "response": answer}
    if args.version is not None:
        body["version"] = args.version
    resp = _call(args, "POST", f"/api/v1/projects/{args.project}/entries", json=body)
    if resp.status_code not in (200, 201):
        code = _fail(args, resp)
        if not args.json and resp.status_code == 409:
            current = resp.json().get("current") or {}
            if "version" in current:
                print(
                    f"entry {current['id']} is at version {current['version']}: "
                    f"pass --version {current['version']} to replace its answer",
                    file=sys.stderr,
                )
        return code
    entry = resp.json()
    created = resp.status_code == 201
    if args.pin:  # the upsert has no pin of its own: set it on the version just written
        pinned = _call(
            args, "PUT", f"/api/v1/entries/{entry['id']}",
            json={"version": entry["version"], "pinned": True},
        )  # fmt: skip
        if pinned.status_code != 200:
            return _fail(args, pinned)
        entry = pinned.json()
    verb = "created" if created else "replaced the answer of"
    human = f"{verb} entry {entry['id']} (version {entry['version']})"
    return _ok(args, {"created": created, "entry": entry}, human)


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


def _embeddings_status(args: argparse.Namespace) -> int:
    resp = _call(args, "GET", "/api/v1/embeddings")
    if resp.status_code != 200:
        return _fail(args, resp)
    out = resp.json()
    if not out["enabled"]:
        human = "embeddings: off (set SEMULA_EMBEDDINGS=on with the 'embeddings' extra installed)"
    else:
        queued = f", {out['backlog']} queued" if out["backlog"] else ""
        human = (
            f"embeddings: on, {out['model']}, {out['indexed']}/{out['entries']} entries indexed"
            f"{queued}"
        )
    return _ok(args, out, human)


def _embeddings_fetch(args: argparse.Namespace) -> int:
    """Download the model once, to the model directory, so the server can start offline."""
    try:
        from .config import Settings
        from .embeddings import EmbeddingsUnavailable, FastEmbedder
    except ImportError:
        return _left_out("embeddings fetch", SERVER_HINT)

    directory = Settings.from_env().resolved_model_dir
    try:
        embedder = FastEmbedder(directory)
    except EmbeddingsUnavailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    return _ok(
        args,
        {"model": embedder.model_id, "dir": str(directory)},
        f"{embedder.model_id} is ready in {directory}",
    )


def _call_line(c: dict[str, Any]) -> str:
    tier = c["tier"] or "-"
    score = f" {c['score']:.2f}" if c["score"] is not None else ""
    latency = f"{c['latency_ms']:.0f}ms" if c["latency_ms"] is not None else "-"
    entry = c["entry_id"] or c["pending_id"] or "-"
    preview = f'  "{c["preview"]}"' if c.get("preview") else ""
    return (
        f"{c['ts'][11:23]}  {c['project']:<12} {c['outcome']:<14} {tier}{score}  {latency}  "
        f"{entry}{preview}"
    )


def _log_stats(args: argparse.Namespace) -> int:
    params = {"window": args.window}
    if args.project:
        params["project"] = args.project
    resp = _call(args, "GET", "/api/v1/log/stats", params=params)
    if resp.status_code != 200:
        return _fail(args, resp)
    out = resp.json()
    totals = out["totals"]
    rate = f"{out['hit_rate'] * 100:.0f}%" if out["hit_rate"] is not None else "-"
    median = f"{out['median_latency_ms']:.1f} ms" if out["median_latency_ms"] is not None else "-"
    human = (
        f"last {out['window']}: {totals['calls']} calls, hit rate {rate}, median {median}, "
        f"parked {out['parked']}\n"
        f"hits {totals['hits']}  misses {totals['misses']}  recorded {totals['recorded']}  "
        f"timed out {totals['timed_out']}"
        + (f"  forwarded {totals['forwarded']}" if totals["forwarded"] else "")
        + (f"  upstream calls {out['upstream']['calls']}" if out["upstream"]["calls"] else "")
    )
    return _ok(args, out, human)


def _log(args: argparse.Namespace) -> int:
    if args.stats:
        return _log_stats(args)
    params: dict[str, Any] = {"limit": args.limit}
    for key in ("project", "outcome"):
        if getattr(args, key):
            params[key] = getattr(args, key)
    resp = _call(args, "GET", "/api/v1/log", params=params)
    if resp.status_code != 200:
        return _fail(args, resp)
    calls = resp.json()["calls"]
    return _ok(args, resp.json(), "\n".join(_call_line(c) for c in calls) or "no calls")


def _tail(args: argparse.Namespace) -> int:
    """Follow calls as they happen. With --json, one JSON object per line. Ctrl-C to stop."""
    import httpx

    params = {"project": args.project} if args.project else {}
    headers = {}
    if os.environ.get("SEMULA_API_KEY"):
        headers["Authorization"] = f"Bearer {os.environ['SEMULA_API_KEY']}"
    seen = 0
    try:
        with httpx.stream(
            "GET",
            args.url.rstrip("/") + "/api/v1/log/stream",
            params=params,
            headers=headers,
            timeout=httpx.Timeout(10.0, read=None),
        ) as resp:
            if resp.status_code != 200:
                resp.read()
                return _fail(args, resp)
            for line in resp.iter_lines():
                if not line.startswith("data: "):
                    continue
                event = json.loads(line[6:])
                print(json.dumps(event) if args.json else _call_line(event), flush=True)
                seen += 1
                if args.count and seen >= args.count:
                    break
    except KeyboardInterrupt:
        pass
    return EXIT_OK


def _review_list(args: argparse.Namespace) -> int:
    params = {"status": "all" if args.all else "open"}
    if args.project:
        params["project"] = args.project
    resp = _call(args, "GET", "/api/v1/candidates", params=params)
    if resp.status_code != 200:
        return _fail(args, resp)
    items = resp.json()["candidates"]
    lines = [
        f"{c['id']}\t{c['status']}\t{c['score']:.2f}\t{c['project']}\t"
        f"{c['last_message'].replace(chr(10), ' ')[:60]}  ~  "
        f"{c['entry_last_message'].replace(chr(10), ' ')[:60]}"
        for c in items
    ]
    return _ok(args, resp.json(), "\n".join(lines) or "no candidates")


def _review_show(args: argparse.Namespace) -> int:
    resp = _call(args, "GET", f"/api/v1/candidates/{args.id}")
    if resp.status_code != 200:
        return _fail(args, resp)
    return _ok(args, resp.json(), json.dumps(resp.json(), indent=2))


def _review_decide(args: argparse.Namespace) -> int:
    resp = _call(args, "POST", f"/api/v1/candidates/{args.id}/{args.decision}")
    if resp.status_code != 200:
        return _fail(args, resp)
    return _ok(args, resp.json(), f"{resp.json()['status']} candidate {args.id}")


def _export(args: argparse.Namespace) -> int:
    from pathlib import Path

    from .exchange import write_bundle

    resp = _call(args, "GET", f"/api/v1/projects/{args.project}/export")
    if resp.status_code != 200:
        return _fail(args, resp)
    counts = write_bundle(resp.json(), Path(args.dir))
    out = {"project": args.project, "dir": args.dir, **counts}
    human = f"exported {counts['entries']} entries and {counts['aliases']} aliases to {args.dir}"
    return _ok(args, out, human)


def _import(args: argparse.Namespace) -> int:
    from pathlib import Path

    from .exchange import BundleError, read_bundle

    try:
        bundle = read_bundle(Path(args.dir))
    except BundleError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_REJECTED
    body: dict[str, Any] = {"bundle": bundle, "force": args.force}
    if args.project:
        body["project"] = args.project
    resp = _call(args, "POST", "/api/v1/import", json=body)
    if resp.status_code != 200:
        return _fail(args, resp)
    out = resp.json()
    e, a = out["entries"], out["aliases"]
    human = (
        f"imported into '{out['project']}'{' (created)' if out['created_project'] else ''}: "
        f"entries +{e['created']} ~{e['updated']} ={e['unchanged']}, "
        f"aliases +{a['created']} ~{a['updated']} ={a['unchanged']}"
    )
    if e["skipped_pinned"]:
        human += f"\nskipped {len(e['skipped_pinned'])} pinned entries (use --force): " + ", ".join(
            e["skipped_pinned"]
        )
    return _ok(args, out, human)


def _mode(args: argparse.Namespace) -> int:
    if args.mode is None:
        resp = _call(args, "GET", "/api/v1/mode")
        if resp.status_code != 200:
            return _fail(args, resp)
        body = resp.json()
        lines = [f"server default   {body['mode']}"]
        lines += [f"project {slug}  {mode}" for slug, mode in sorted(body["projects"].items())]
        return _ok(args, body, "\n".join(lines))
    payload: dict[str, Any] = {"mode": args.mode}
    if args.project:
        payload["project"] = args.project
    resp = _call(args, "PUT", "/api/v1/mode", json=payload)
    if resp.status_code != 200:
        return _fail(args, resp)
    body = resp.json()
    if body["persisted"]:
        human = f"{body['project']}: {body['mode']} (kept across restarts)"
    else:
        human = (
            f"default: {body['mode']} (until the server restarts; "
            "add --project <id> to keep a project's mode)"
        )
    return _ok(args, body, human)


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
        human = (
            f"match: {out['tier']} ({out['score']:.2f}) -> entry {out['entry_id']} "
            f"(would {out['would']})"
        )
    else:
        near = out["nearest"]
        hint = f"; nearest {near['entry_id']} score {near['score']}" if near else ""
        if out["candidates"]:
            hint += f"; {len(out['candidates'])} weak candidate(s) would go to review"
        human = f"no match (would {out['would']}){hint}"
    if out.get("tiers"):
        human += "\ntiers: " + ", ".join(_tier_text(t) for t in out["tiers"])
    return _ok(args, out, human)


def _tier_text(tier: dict[str, Any]) -> str:
    score = f" {tier['score']:.2f}" if "score" in tier else ""
    return f"{tier['tier']} {tier['verdict']}{score}"


def _admin_service(args: argparse.Namespace, workspace: str | None) -> Any:
    """The service on this machine's own database, for work only the operator does: creating
    workspaces and minting the first tokens of a hosted server (ADR-0010). Not over HTTP, so
    it needs the server library and the database settings of the server."""
    from .config import Settings
    from .db import Database
    from .scope import Scope
    from .service import Service

    settings = Settings.from_env()
    service = Service(Database(settings.resolved_database_url), settings)
    return service.for_scope(Scope(workspace)) if workspace else service


def _usage(args: argparse.Namespace) -> int:
    resp = _call(args, "GET", "/api/v1/usage")
    if resp.status_code != 200:
        return _fail(args, resp)
    out = resp.json()
    used, limits = out["used"], out["limits"]
    names = [
        ("projects", "projects"),
        ("entries", "entries"),
        ("bytes", "stored bytes"),
        ("parked", "parked calls"),
        ("tokens", "active tokens"),
    ]
    lines = []
    for key, label in names:
        cap = limits.get(key) if key != "parked" else limits.get("parked_per_workspace")
        lines.append(f"{label:<14}{used[key]:>12} of {cap if cap is not None else 'no limit'}")
    return _ok(args, out, "\n".join(lines))


def _audit(args: argparse.Namespace) -> int:
    params: dict[str, Any] = {"limit": args.limit}
    if args.action:
        params["action"] = args.action
    if args.before:
        params["before_id"] = args.before
    resp = _call(args, "GET", "/api/v1/audit", params=params)
    if resp.status_code != 200:
        return _fail(args, resp)
    events = resp.json()["events"]
    lines = [
        f"{e['id']}\t{e['ts'][:19]}\t{e['principal_kind']}:{e['principal_id']}\t{e['action']}\t"
        f"{e['target'] or '-'}\t{json.dumps(e['detail']) if e['detail'] else ''}"
        for e in events
    ]
    return _ok(args, events, "\n".join(lines) if lines else "no audit events")


def _admin_migrate(args: argparse.Namespace) -> int:
    try:
        from .config import Settings
        from .db import Database, head_revision
    except ImportError:
        return _left_out("admin", SERVER_HINT)
    db = Database(Settings.from_env().resolved_database_url)
    return _ok(args, {"revision": db.revision()}, f"the database is at revision {head_revision()}")


def _admin_workspace(args: argparse.Namespace) -> int:
    try:
        service = _admin_service(args, None)
    except ImportError:
        return _left_out("admin", SERVER_HINT)
    from .errors import SemulaError

    try:
        service.create_workspace(args.id, args.name)
    except SemulaError as exc:
        print(f"error: {exc.code}: {exc.message}", file=sys.stderr)
        return EXIT_REJECTED
    return _ok(args, {"workspace": args.id}, f"created workspace {args.id}")


def _admin_workspace_state(args: argparse.Namespace) -> int:
    """Delete (with the id typed back), restore, or purge workspaces: the operator's side of
    ADR-0011 section 5."""
    try:
        service = _admin_service(args, getattr(args, "workspace", None))
    except ImportError:
        return _left_out("admin", SERVER_HINT)
    from .errors import SemulaError

    try:
        if args.verb == "purge":
            purged = service.purge_workspaces()
            return _ok(args, {"purged": purged}, f"purged {', '.join(purged) or 'nothing'}")
        if args.verb == "delete":
            status = service.delete_workspace(args.confirm)
        else:
            status = service.restore_workspace()
    except SemulaError as exc:
        print(f"error: {exc.code}: {exc.message}", file=sys.stderr)
        return EXIT_REJECTED
    deleted = status["deleted_at"]
    human = (
        f"workspace {status['id']} is deleted; it can be restored until {status['purge_after']}"
        if deleted
        else f"workspace {status['id']} is restored"
    )
    return _ok(args, status, human)


def _admin_invite(args: argparse.Namespace) -> int:
    """Invite someone to a workspace from the operator's side: how the first admin gets in."""
    try:
        service = _admin_service(args, args.workspace)
        from .config import Settings

        public = Settings.from_env().public_url
    except ImportError:
        return _left_out("admin", SERVER_HINT)
    from .errors import SemulaError

    try:
        made = service.create_invitation(args.email, args.role)
    except SemulaError as exc:
        print(f"error: {exc.code}: {exc.message}", file=sys.stderr)
        return EXIT_REJECTED
    link = f"{public}/auth/invite/{made['token']}"
    human = (
        f"invited {made['email']} as {made['role']} until {made['expires_at']}\n{link}\n"
        "Send them the link; it works once, for them, after they sign in."
    )
    return _ok(args, {**{k: v for k, v in made.items() if k != "token"}, "link": link}, human)


def _admin_members(args: argparse.Namespace) -> int:
    try:
        service = _admin_service(args, args.workspace)
    except ImportError:
        return _left_out("admin", SERVER_HINT)
    members = service.list_members()
    lines = [f"{m['person_id']}\t{m['role']}\t{m['email']}\t{m['name']}" for m in members]
    return _ok(args, members, "\n".join(lines) if lines else "no members")


def _admin_recount(args: argparse.Namespace) -> int:
    try:
        service = _admin_service(args, args.workspace)
    except ImportError:
        return _left_out("admin", SERVER_HINT)
    used = service.recount_usage()
    return _ok(args, used, ", ".join(f"{k} {v}" for k, v in used.items()))


def _admin_token(args: argparse.Namespace) -> int:
    try:
        service = _admin_service(args, args.workspace)
    except ImportError:
        return _left_out("admin", SERVER_HINT)
    from .errors import SemulaError

    try:
        if args.verb == "create":
            out = service.create_token(
                args.name, args.scope, project=args.project, expires_at=args.expires
            )
        elif args.verb == "rotate":
            out = service.rotate_token(args.id, args.grace)
        elif args.verb == "revoke":
            out = service.revoke_token(args.id)
        else:
            tokens = service.list_tokens()
            lines = [
                "\t".join(
                    [
                        t["id"],
                        t["prefix"] + "…",
                        t["scope"],
                        t["project"] or "all projects",
                        t["name"],
                        "revoked" if t["revoked_at"] else t["last_used_at"] or "never used",
                    ]
                )
                for t in tokens
            ]
            return _ok(args, tokens, "\n".join(lines) if lines else "no tokens")
    except SemulaError as exc:
        print(f"error: {exc.code}: {exc.message}", file=sys.stderr)
        return EXIT_REJECTED
    secret = out.get("token")
    human = f"{args.verb}d token {out['id']} ({out['scope']}, {out['name']})"
    if secret:
        human += f"\n{secret}\nThis is the only time the token is shown."
    return _ok(args, out, human)


def _mcp(args: argparse.Namespace) -> int:
    try:
        from .mcp_server import run_stdio_proxy
    except ImportError:
        return _left_out(
            "mcp",
            "Point an MCP client that speaks HTTP at <server>/mcp/ instead, "
            "or `pip install semula`.",
        )

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
        description="Mock LLM server powered by your AI subscription.",
        epilog=(__doc__ or "").split("Exit codes", 1)[-1].strip(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"semula {__version__}")
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
    pending.add_argument(
        "--wait",
        type=float,
        metavar="SECONDS",
        default=None,
        help="if nothing waits, hold up to this long (max 60) for a call to be parked",
    )

    add(sub, "show", _show, "Show one pending request in full").add_argument("id")

    conv = add(
        sub,
        "conversations",
        _conversations,
        "Group parked or logged calls whose messages extend an earlier call's",
    )
    conv.add_argument("--project", default=None)
    conv.add_argument("--log", action="store_true", help="group logged calls, not parked ones")
    conv.add_argument(
        "--min-size", type=int, default=2, help="leave out groups smaller than this (default 2)"
    )

    claim = add(
        sub, "claim", _claim, "Claim the oldest unclaimed parked request and print it in full"
    )
    claim.add_argument("--project", default=None)
    claim.add_argument(
        "--lease", type=float, default=300.0, metavar="SECONDS", help="hold it this long (max 3600)"
    )
    claim.add_argument("--worker", default=None, help="a name for this worker, shown on the claim")

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
    pup = add(psub, "upstream", _project_upstream, "Show what a project has forwarded upstream")
    pup.add_argument("id")
    pup.add_argument("--reset", action="store_true", help="count again from zero (admin)")

    entry = sub.add_parser("entry", help="Inspect and edit recorded entries")
    esub = entry.add_subparsers(dest="entry_command", required=True)
    elist = add(esub, "list", _entry_list, "List a project's entries (no bodies)")
    elist.add_argument("project")
    elist.add_argument("--q", default=None, help="text filter")
    elist.add_argument("--limit", type=int, default=50)
    elist.add_argument("--offset", type=int, default=0)
    for flag, meaning in (
        ("pinned", "pinned"),
        ("has-alias", "with an approved alias"),
        ("tool-calls", "whose answer calls a tool"),
        ("never-hit", "never replayed"),
    ):
        elist.add_argument(
            f"--{flag}",
            dest=flag.replace("-", "_"),
            action=argparse.BooleanOptionalAction,
            default=None,
            help=f"only entries {meaning} (--no-{flag}: only the others)",
        )
    elist.add_argument(
        "--sort", choices=["hits", "updated", "created"], help="default: oldest first"
    )
    elist.add_argument("--order", choices=["asc", "desc"], help="default: desc with --sort")
    add(esub, "show", _entry_show, "Show one entry in full").add_argument("id")
    ehist = add(esub, "history", _entry_history, "An entry's change history (newest first)")
    ehist.add_argument("id")
    ehist.add_argument("--version", type=int, default=None, help="show that revision in full")
    eadd = add(esub, "add", _entry_add, "Author an entry (a request and its answer) up front")
    eadd.add_argument("project")
    req_src = eadd.add_mutually_exclusive_group(required=True)
    req_src.add_argument("--prompt", help="the request: one user message with this text")
    req_src.add_argument(
        "--request-file", help="the request as a chat completion body (messages, tools ...)"
    )
    eadd.add_argument("--system", default=None, help="with --prompt: a system message")
    eadd.add_argument("--model", default=None, help="the model name to record the request with")
    ans_src = eadd.add_mutually_exclusive_group(required=True)
    ans_src.add_argument("--text", help="the answer: plain text")
    ans_src.add_argument("--response-file", help="the answer as JSON from a file")
    ans_src.add_argument("--stdin", action="store_true", help="the answer as JSON from stdin")
    eadd.add_argument("--version", type=int, default=None, help="replace the entry at this version")
    eadd.add_argument("--pin", action="store_true", help="pin it so agents leave it alone")
    eadd.add_argument("--as-human", action="store_true", help="write as a human, not an agent")
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

    log = add(sub, "log", _log, "Show recent calls, newest first")
    log.add_argument("--project", default=None)
    log.add_argument("--outcome", default=None, help="hit, miss, recorded, record_timeout")
    log.add_argument("--limit", type=int, default=20)
    log.add_argument("--stats", action="store_true", help="counts, hit rate and median instead")
    log.add_argument("--window", default="30m", help="with --stats: 30m, 2h ... up to 24h")

    tail = add(sub, "tail", _tail, "Follow calls live (one JSON object per line with --json)")
    tail.add_argument("--project", default=None)
    tail.add_argument("--count", type=int, default=0, help="stop after this many calls")

    emb = sub.add_parser("embeddings", help="Optional semantic matching")
    esub2 = emb.add_subparsers(dest="embeddings_command", required=True)
    add(esub2, "status", _embeddings_status, "Is it on, and how many entries are indexed")
    add(esub2, "fetch", _embeddings_fetch, "Download the model once (needs the extra and network)")

    review = sub.add_parser("review", help="Decide near-miss matches")
    rsub = review.add_subparsers(dest="review_command", required=True)
    rlist = add(rsub, "list", _review_list, "List the review queue, best score first")
    rlist.add_argument("--all", action="store_true", help="include decided candidates")
    rlist.add_argument("--project", default=None)
    add(rsub, "show", _review_show, "Show a candidate and the entry it resembles").add_argument(
        "id"
    )
    for decision, help_ in (
        ("approve", "Serve this prompt from the candidate entry from now on"),
        ("reject", "Never pair this prompt with that entry"),
    ):
        decide = add(rsub, decision, _review_decide, help_)
        decide.add_argument("id")
        decide.set_defaults(decision=decision)

    export = add(sub, "export", _export, "Write a project's entries to a directory of JSON files")
    export.add_argument("project")
    export.add_argument("--dir", required=True, help="created if missing; commit it to git")
    imp = add(sub, "import", _import, "Load an exported directory (safe to repeat)")
    imp.add_argument("dir")
    imp.add_argument("--project", default=None, help="import under this id instead of the file's")
    imp.add_argument("--force", action="store_true", help="also overwrite pinned entries")

    mode = add(sub, "mode", _mode, "Show or switch the mode (replay or record)")
    mode.add_argument("mode", nargs="?", choices=["replay", "record"])
    mode.add_argument("--project", default=None, help="set this project's own mode (kept)")

    add(sub, "usage", _usage, "What the workspace holds against its limits")

    audit = add(sub, "audit", _audit, "Show the audit log, newest first (people and the operator)")
    audit.add_argument("--limit", type=int, default=50)
    audit.add_argument("--action", default=None, help="only this action, like project.delete")
    audit.add_argument("--before", type=int, default=None, help="events before this id")

    admin = sub.add_parser("admin", help="Operator tasks on the server's own database (hosting)")
    asub = admin.add_subparsers(dest="admin_command", required=True)
    add(asub, "migrate", _admin_migrate, "Bring the database up to date (run once before replicas)")
    wscreate = add(asub, "workspace-create", _admin_workspace, "Create a workspace")
    wscreate.add_argument("id", help="a slug: lowercase letters, digits, hyphens")
    wscreate.add_argument("--name", default=None)
    for verb, helptext in (
        ("delete", "Delete a workspace: tokens stop at once, purged after 30 days"),
        ("restore", "Bring a deleted workspace back during its 30 days"),
    ):
        ws = add(asub, f"workspace-{verb}", _admin_workspace_state, helptext)
        ws.set_defaults(verb=verb)
        ws.add_argument("workspace", help="the workspace id")
        if verb == "delete":
            ws.add_argument("--confirm", required=True, help="the workspace id again, to confirm")
    purge = add(
        asub,
        "workspace-purge",
        _admin_workspace_state,
        "Hard-delete every workspace whose 30 days are over",
    )
    purge.set_defaults(verb="purge")
    invite = add(asub, "invite", _admin_invite, "Invite someone to a workspace (prints the link)")
    invite.add_argument("--workspace", required=True)
    invite.add_argument("--email", required=True)
    invite.add_argument("--role", choices=["viewer", "editor", "admin"], default="admin")
    members = add(asub, "members", _admin_members, "List a workspace's members")
    members.add_argument("--workspace", required=True)
    recount = add(
        asub, "recount", _admin_recount, "Rebuild a workspace's quota counters from what is stored"
    )
    recount.add_argument("--workspace", required=True)
    for verb, helptext in (
        ("create", "Mint an API token for a workspace (the secret is shown once)"),
        ("list", "List a workspace's API tokens"),
        ("revoke", "Revoke an API token"),
        ("rotate", "Mint a successor; the old token keeps working for a grace period"),
    ):
        tok = add(asub, f"token-{verb}", _admin_token, helptext)
        tok.set_defaults(verb=verb)
        tok.add_argument("--workspace", required=True)
        if verb == "create":
            tok.add_argument("--name", required=True)
            tok.add_argument("--scope", required=True, choices=["replay", "record", "admin"])
            tok.add_argument("--project", default=None, help="only this project (default: all)")
            tok.add_argument(
                "--expires", default=None, help="an ISO-8601 time, e.g. 2027-01-01T00:00:00.000Z"
            )
        if verb in ("revoke", "rotate"):
            tok.add_argument("id")
        if verb == "rotate":
            tok.add_argument(
                "--grace", type=float, default=86400.0, help="seconds the old token still works"
            )

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
    except Exception as exc:
        httpx = sys.modules.get("httpx")  # not loaded means it cannot be an httpx error
        if httpx is None or not isinstance(exc, httpx.ConnectError):
            raise
        print(f"error: cannot reach semula at {args.url}. Is it running?", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
