"""The MCP surface: the same service layer as REST and the CLI (ADR-0005).

Mounted at ``/mcp`` (streamable HTTP). ``semula mcp`` runs a stdio proxy to it for
clients that only speak stdio. Every write here is made as an *agent*, so a pinned
entry is protected unless the agent is told to force it.

The tool descriptions are part of the product: they are what steers an agent toward
answers that replay cleanly. Keep them in step with docs/agents/PLAYBOOK.md.
"""

from __future__ import annotations

import functools
import inspect
import json
from collections.abc import Callable
from typing import Any, TypeVar

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server import create_proxy

from . import __version__, canonical
from .errors import SemulaError
from .scope import Principal, Scope
from .service import Service
from .tokens import LEVEL

T = TypeVar("T")

INSTRUCTIONS = """\
semula is a record-replay fake LLM server. An app's LLM call that has no recorded answer
is parked as a *pending request*. You answer it; semula validates and saves the answer, the
app's held call completes, and every later identical call replays your answer for free.

Typical loop: wait_for_pending -> get_pending -> answer_pending, then repeat. Answer promptly:
held calls time out (default 300 s), though a late answer is still saved.

Write answers the way the real model would, in the format the app's prompt asks for. Replace
anything that changes per call with a template: {{now}}, {{uuid}}, {{request.model}}.
Near-misses wait in a review queue (list_candidates); a human normally decides them.
Errors come back as JSON with a 'code' and 'param'; fix what they name and resubmit.
"""


# The lowest token scope for each tool (ADR-0010): `record` is the agent's own work; `admin` is
# what a human decides. A tool not listed here is refused to tokens, so a new one is closed first.
TOOL_SCOPES = {
    "get_status": "record", "list_projects": "record", "get_project_settings": "record",
    "get_upstream_usage": "record",
    "list_pending": "record", "wait_for_pending": "record", "get_pending": "record",
    "claim_next": "record",
    "answer_pending": "record", "list_entries": "record", "get_entry": "record",
    "entry_history": "record", "upsert_entry": "record", "update_entry": "record",
    "test_match": "record", "list_calls": "record", "log_stats": "record",
    "list_candidates": "record", "get_candidate": "record",
    "set_mode": "admin", "create_project": "admin", "update_project_settings": "admin",
    "delete_entry": "admin", "approve_candidate": "admin", "reject_candidate": "admin",
}  # fmt: skip


def _principal() -> Principal | None:
    """Who is calling this tool: the credential the HTTP request carried (hosted), else nobody
    (local mode, where the server's own scope applies)."""
    try:
        from fastmcp.server.dependencies import get_http_request

        found = getattr(get_http_request().state, "principal", None)
    except RuntimeError:  # not inside an HTTP request: a direct call, as in tests
        return None
    return found if isinstance(found, Principal) else None


def _refuse(code: str, message: str) -> ToolError:
    body = {
        "error": {"message": message, "type": "authentication_error", "param": None, "code": code}
    }
    return ToolError(json.dumps(body))


def _scoped(fn: Callable[..., T]) -> Callable[..., T]:
    """Refuse a token whose scope is below what the tool needs, or that tries to force."""
    needed = TOOL_SCOPES.get(fn.__name__, "admin")

    def check(kwargs: dict[str, Any]) -> None:
        who = _principal()
        if who is None or who.kind != "token":
            return
        level = LEVEL.get(who.scope or "", 0)
        if level < LEVEL[needed]:
            raise _refuse(
                "insufficient_scope",
                f"This token has scope '{who.scope}'; this tool needs '{needed}'.",
            )
        if kwargs.get("force") and who.scope != "admin":
            raise _refuse("insufficient_scope", "Forcing past a pin needs an admin token.")

    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def async_checked(*args: Any, **kwargs: Any) -> Any:
            check(kwargs)
            return await fn(*args, **kwargs)

        return async_checked  # type: ignore[return-value]

    @functools.wraps(fn)
    def checked(*args: Any, **kwargs: Any) -> T:
        check(kwargs)
        return fn(*args, **kwargs)

    return checked


def _guard(fn: Callable[..., T]) -> Callable[..., T]:
    """Turn a SemulaError into a ToolError whose text is the JSON the agent should act on."""

    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return await fn(*args, **kwargs)
            except SemulaError as exc:
                raise ToolError(json.dumps(exc.body())) from exc

        return async_wrapper  # type: ignore[return-value]

    @functools.wraps(fn)  # keeps the signature visible, so FastMCP builds the tool schema
    def wrapper(*args: Any, **kwargs: Any) -> T:
        try:
            return fn(*args, **kwargs)
        except SemulaError as exc:
            raise ToolError(json.dumps(exc.body())) from exc

    return wrapper


def build_mcp(base: Service) -> FastMCP:
    mcp: FastMCP = FastMCP("semula", instructions=INSTRUCTIONS, version=__version__)

    class PerCall:
        """The service as the current call sees it: bound to the workspace and project of the
        token that made the HTTP request when hosted, the server's own scope otherwise."""

        def __getattr__(self, name: str) -> Any:
            who = _principal()
            scoped = base.for_scope(Scope(who.workspace_id, who)) if who else base
            return getattr(scoped, name)

    service: Service = PerCall()  # type: ignore[assignment]

    def tool(fn: Callable[..., Any]) -> Any:
        return mcp.tool(_guard(_scoped(fn)))

    @tool
    def get_status() -> dict[str, Any]:
        """Show the server's default mode, its version and the projects, each with its own mode
        ('replay' or 'record'), entry count and pending count. Start here to see whether anything
        is waiting."""
        return {
            "version": __version__,
            "mode": service.default_mode,
            "projects": service.list_projects(),
            "embeddings": service.embeddings_status(),
            "usage": None if service.project_limit else service.usage(),
        }

    @tool
    def set_mode(mode: str, project: str | None = None) -> dict[str, Any]:
        """Switch a project between 'record' (unknown prompts are parked for you to answer) and
        'replay' (unknown prompts fail fast; nothing is parked). With 'project' the choice is
        kept across restarts. Without it you move the server's default, which projects that have
        not chosen follow, and that is forgotten when the server restarts."""
        return service.set_mode(mode, project)

    @tool
    def list_projects() -> list[dict[str, Any]]:
        """List projects with entry and pending counts. A project keeps one app's recordings
        apart from another's."""
        return service.list_projects()

    @tool
    def create_project(id: str, name: str | None = None) -> dict[str, Any]:
        """Create a project. 'id' is a slug: lowercase letters, digits and hyphens. Apps select
        it with the X-Semula-Project header or the /v1/projects/<id>/ URL prefix."""
        return service.create_project(id, name)

    @tool
    def get_project_settings(id: str) -> dict[str, Any]:
        """Show a project's matching and recording settings with their defaults."""
        return service.get_project(id)["settings"]  # type: ignore[no-any-return]

    @tool
    def get_upstream_usage(project: str) -> dict[str, Any]:
        """How many calls and tokens a project has forwarded to the real upstream provider
        (record-through), against its caps, and, in a dry run, the prompts it would have
        forwarded ('would_forward')."""
        return {
            **service.upstream_usage_of(project),
            "would_forward": service.upstream_dry_run_misses(project),
        }

    @tool
    def update_project_settings(id: str, settings: dict[str, Any]) -> dict[str, Any]:
        """Patch a project's settings; a null value resets that setting. Keys: models, tail_n
        (match only the last N messages), scrub_patterns (regexes to ignore when matching),
        include_params (request params that count, e.g. ['temperature']), record_timeout,
        redact_patterns (regexes for secrets: stored as [REDACTED] and ignored when matching),
        redact_defaults (also redact common API keys and tokens; default true),
        bm25_serve_threshold (0-1 word overlap at which a close match is served; default 1, so
        near-misses go to review),
        bm25_review_threshold (0-1 floor for the review queue; default 0.6).
        Changing a matching setting re-keys the project's existing entries."""
        return service.update_project(id, settings=settings)["settings"]  # type: ignore[no-any-return]

    @tool
    def list_pending(
        project: str | None = None, include_answered: bool = False
    ) -> list[dict[str, Any]]:
        """List parked requests waiting for an answer, oldest first. Each has an id, project and
        the last message. Call get_pending for the full request before answering."""
        return service.list_pending(None if include_answered else "pending", project)

    @tool
    async def wait_for_pending(
        project: str | None = None, timeout_seconds: float = 30
    ) -> list[dict[str, Any]]:
        """Wait for the next parked request. Returns the pending list (as list_pending does) as
        soon as one is parked, at once if some already wait, or an empty list after
        timeout_seconds (at most 60). This is the loop's resting state: call it, answer what it
        returns, call it again. An empty list means nothing came; just call it again."""
        if not 0 <= timeout_seconds <= 60:
            raise ToolError(
                json.dumps(
                    {
                        "error": {
                            "code": "invalid_request",
                            "param": "timeout_seconds",
                            "message": "timeout_seconds must be 0 to 60.",
                        }
                    }
                )
            )
        return await service.wait_for_pending(project, timeout_seconds)

    @tool
    def claim_next(
        project: str | None = None, lease_seconds: float = 300, worker: str | None = None
    ) -> dict[str, Any] | None:
        """Take the oldest parked request nobody holds and read it in one call: the same view
        get_pending gives (messages, tools, 'shape'), plus 'claim' with when your hold ends. Other
        claim_next calls skip it until then, so several workers can share a queue. Returns null
        when nothing is claimable. Answer with answer_pending before the lease ends; a lease that
        runs out frees the call for someone else (lease_seconds up to 3600)."""
        return service.claim_next(project, lease_seconds, worker)

    @tool
    def get_pending(id: str) -> dict[str, Any]:
        """Read one pending request in full: the messages and any tools the app offered. Read the
        system prompt for the output format the app expects. 'shape' lists each offered tool with
        its argument schema and a skeleton answer that passes validation (fill in the arguments
        and send it), and 'max_tokens' when the app capped the length. It never writes the
        content for you."""
        return service.get_pending(id)

    @tool
    def answer_pending(id: str, answer: dict[str, Any] | list[Any]) -> dict[str, Any]:
        """Answer a pending request. This releases the app's held call and saves the answer for
        replay. Send one of:
          {"content": "text"}                                   a plain text reply
          {"message": {"content": null, "tool_calls": [         a tool call; only tools the
              {"function": {"name": "...", "arguments": {...}}}]}}  request offered, arguments must
                                                                satisfy that tool's JSON schema
          {"chunks": ["Hel", "lo"]}                             a stream with these chunk boundaries
        'answer' must be an object or list: a bare string is rejected, so wrap plain text as
        {"content": "..."}.
        Match the format the app's prompt demands (if it asks for JSON, reply with valid JSON).
        Use templates for values that change per call: {{now}}, {{uuid}}, {{request.model}}; never
        paste a real timestamp or id. On a 422 error the call stays held: fix the field named in
        'param' and submit again."""
        return {"entry_id": service.answer_pending(id, answer), "pending_id": id}

    @tool
    def list_entries(
        project: str,
        q: str | None = None,
        limit: int = 50,
        offset: int = 0,
        pinned: bool | None = None,
        has_alias: bool | None = None,
        tool_calls: bool | None = None,
        never_hit: bool | None = None,
        sort: str | None = None,
        order: str | None = None,
    ) -> dict[str, Any]:
        """List a project's recorded entries without their bodies: id, version, pinned flag, hit
        count, alias_count, has_tools (an answer calls a tool) and the last message. 'q' filters
        by text; 'pinned', 'has_alias', 'tool_calls' and 'never_hit' filter by flag (false picks
        the opposite). 'sort' is hits, updated or created (most first; 'order' asc or desc).
        'total' counts the filtered set. Use get_entry for the full content."""
        return service.list_entries(
            project,
            q,
            limit,
            offset,
            pinned=pinned,
            has_alias=has_alias,
            tool_calls=tool_calls,
            never_hit=never_hit,
            sort=sort,
            order=order,
        )

    @tool
    def get_entry(id: str) -> dict[str, Any]:
        """Read one entry in full: the request, every stored response, selection policy,
        version and pinned flag. You need its 'version' to change or delete it."""
        return service.get_entry(id)

    @tool
    def entry_history(id: str, version: int | None = None) -> dict[str, Any]:
        """An entry's change history, newest first: version, time, actor (agent or human), a short
        summary and whether an agent forced past a pin. The latest 50 are kept. Pass 'version' to
        read that revision in full (the responses, selection and pin as that write left them), for
        example to compare with the current entry after a version_conflict."""
        return service.entry_history(id, version)

    @tool
    def upsert_entry(
        project: str,
        request: dict[str, Any],
        response: dict[str, Any] | list[Any],
        version: int | None = None,
    ) -> dict[str, Any]:
        """Author an entry without waiting for a miss: 'request' is a chat completion body,
        'response' is an answer in the same forms as answer_pending. If an entry already matches
        the request you must pass its current 'version' to replace the response (otherwise you get
        entry_exists with the current entry). Use templates for per-call values."""
        entry, created = service.upsert_entry(project, request, response, version=version)
        return {"created": created, "entry": entry}

    @tool
    def update_entry(
        id: str, version: int, changes: dict[str, Any], force: bool = False
    ) -> dict[str, Any]:
        """Change an entry. 'version' must be the entry's current version, else you get
        version_conflict plus the current entry: re-read it and apply your change to that. Keys in
        'changes': response (replace with one), responses (replace the list), add_response,
        selection ('first' | 'sequence' | 'cycle'), pinned. A pinned entry was fixed by a human:
        you get entry_pinned and must not pass force=true unless a human told you to."""
        return service.update_entry(id, version, changes, actor="agent", force=force)

    @tool
    def delete_entry(id: str, version: int, force: bool = False) -> dict[str, str]:
        """Delete an entry at its current version. Pinned entries are refused unless forced;
        do not force unless a human told you to."""
        service.delete_entry(id, version, actor="agent", force=force)
        return {"deleted": id}

    @tool
    def test_match(
        project: str = "default", prompt: str | None = None, request: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Ask what would happen to a request now, without serving it or counting a hit: which
        tier matched (exact, normalised, alias or bm25), the entry and its score, and on a miss
        the weak 'candidates' that would go to review and the nearest recorded entry. 'tiers'
        lists a verdict for each of exact, normalised, alias, words and meaning (no, match,
        candidate, skipped, off) with an optional score and a short reason. Pass
        'prompt' (one user message) or a full chat 'request'."""
        if request is None:
            if prompt is None:
                raise SemulaError(400, "invalid_request", "Pass 'prompt' or 'request'.")
            request = {"messages": [{"role": "user", "content": prompt}]}
        if not isinstance(request.get("messages"), list):
            raise SemulaError(400, "invalid_request", "The request needs a 'messages' list.")
        return service.test_match(canonical.from_openai(request), project)

    @tool
    def list_calls(
        project: str | None = None, outcome: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Recent calls through semula, newest first: time, project, mode, outcome (hit, miss,
        recorded, record_timeout), tier (exact, normalised, alias, bm25), score, latency, the
        entry and a 'preview' (the last message, redacted, at most 120 characters). Use it to
        see why an app's calls are missing or timing out."""
        return service.list_calls(project, outcome, limit)

    @tool
    def log_stats(project: str | None = None, window: str = "30m") -> dict[str, Any]:
        """How calls are going: per-minute counts (hits, misses, recorded, timed_out) for the
        last 'window' (like 30m or 2h, up to 24h), the hit_rate (hits over all calls), the
        median_latency_ms of replayed calls and 'parked', how many requests wait for an answer
        right now. A low hit rate or a growing parked count says where to look next."""
        return service.log_stats(project, window)

    @tool
    def list_candidates(
        project: str | None = None, include_decided: bool = False
    ) -> list[dict[str, Any]]:
        """List the review queue: prompts that nearly matched a recorded entry but not closely
        enough to serve. Each has a word-overlap score, the new prompt's last message and the
        entry's. Best score first. Nothing was served from these; the app's call was a miss."""
        return service.list_candidates(project, None if include_decided else "open")

    @tool
    def get_candidate(id: str) -> dict[str, Any]:
        """Read one candidate in full: the new request, and the entry (request and responses) it
        resembles. Compare them closely before deciding."""
        return service.get_candidate(id)

    @tool
    def approve_candidate(id: str) -> dict[str, Any]:
        """Say the new prompt means the same as the entry: that prompt will now be answered from
        the entry. Only do this when you have read both and the answer is right for the new
        prompt, or a human told you to. A wrong approval silently serves a wrong answer."""
        return service.approve_candidate(id)

    @tool
    def reject_candidate(id: str) -> dict[str, Any]:
        """Say the prompts differ. That pairing never reappears in the queue and is never served.
        Answer the pending request for the new prompt instead."""
        return service.reject_candidate(id)

    return mcp


def run_stdio_proxy(url: str, api_key: str | None) -> None:
    """Serve MCP on stdio, forwarding every call to a running semula over HTTP."""
    from fastmcp.client.transports import StreamableHttpTransport

    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    transport = StreamableHttpTransport(url.rstrip("/") + "/mcp/", headers=headers)
    create_proxy(transport, name="semula").run()
