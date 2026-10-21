"""The MCP surface: the same service layer as REST and the CLI (ADR-0005).

Mounted at ``/mcp`` (streamable HTTP). ``semula mcp`` runs a stdio proxy to it for
clients that only speak stdio. Every write here is made as an *agent*, so a pinned
entry is protected unless the agent is told to force it.

The tool descriptions are part of the product: they are what steers an agent toward
answers that replay cleanly. Keep them in step with docs/agents/PLAYBOOK.md.
"""

from __future__ import annotations

import functools
import json
from collections.abc import Callable
from typing import Any, TypeVar

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server import create_proxy

from . import __version__, canonical
from .errors import SemulaError
from .service import Service

T = TypeVar("T")

INSTRUCTIONS = """\
semula is a record-replay fake LLM server. An app's LLM call that has no recorded answer
is parked as a *pending request*. You answer it; semula validates and saves the answer, the
app's held call completes, and every later identical call replays your answer for free.

Typical loop: list_pending -> get_pending -> answer_pending, then repeat. Answer promptly:
held calls time out (default 300 s), though a late answer is still saved.

Write answers the way the real model would, in the format the app's prompt asks for. Replace
anything that changes per call with a template: {{now}}, {{uuid}}, {{request.model}}.
Near-misses wait in a review queue (list_candidates); a human normally decides them.
Errors come back as JSON with a 'code' and 'param'; fix what they name and resubmit.
"""


def _guard(fn: Callable[..., T]) -> Callable[..., T]:
    """Turn a SemulaError into a ToolError whose text is the JSON the agent should act on."""

    @functools.wraps(fn)  # keeps the signature visible, so FastMCP builds the tool schema
    def wrapper(*args: Any, **kwargs: Any) -> T:
        try:
            return fn(*args, **kwargs)
        except SemulaError as exc:
            raise ToolError(json.dumps(exc.body())) from exc

    return wrapper


def build_mcp(service: Service) -> FastMCP:
    mcp: FastMCP = FastMCP("semula", instructions=INSTRUCTIONS, version=__version__)

    def tool(fn: Callable[..., Any]) -> Any:
        return mcp.tool(_guard(fn))

    @tool
    def get_status() -> dict[str, Any]:
        """Show the server's mode ('replay' or 'record'), version and the projects with their
        entry and pending counts. Start here to see whether anything is waiting."""
        return {
            "version": __version__,
            "mode": service.mode,
            "projects": service.list_projects(),
            "embeddings": service.embeddings_status(),
        }

    @tool
    def set_mode(mode: str) -> dict[str, str]:
        """Switch between 'record' (unknown prompts are parked for you to answer) and 'replay'
        (unknown prompts fail fast; nothing is parked). Not persisted across restarts."""
        return {"mode": service.set_mode(mode)}

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
    def get_pending(id: str) -> dict[str, Any]:
        """Read one pending request in full: the messages and any tools the app offered. Read the
        system prompt for the output format the app expects, and the tool definitions if any."""
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
        Match the format the app's prompt demands (if it asks for JSON, reply with valid JSON).
        Use templates for values that change per call: {{now}}, {{uuid}}, {{request.model}}; never
        paste a real timestamp or id. On a 422 error the call stays held: fix the field named in
        'param' and submit again."""
        return {"entry_id": service.answer_pending(id, answer), "pending_id": id}

    @tool
    def list_entries(
        project: str, q: str | None = None, limit: int = 50, offset: int = 0
    ) -> dict[str, Any]:
        """List a project's recorded entries without their bodies: id, version, pinned flag, hit
        count and the last message. 'q' filters by text. Use get_entry for the full content."""
        return service.list_entries(project, q, limit, offset)

    @tool
    def get_entry(id: str) -> dict[str, Any]:
        """Read one entry in full: the request, every stored response, selection policy,
        version and pinned flag. You need its 'version' to change or delete it."""
        return service.get_entry(id)

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
        recorded, record_timeout), tier (exact, normalised, alias, bm25), score, latency and the
        entry. Use it to see why an app's calls are missing or timing out."""
        return service.list_calls(project, outcome, limit)

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
