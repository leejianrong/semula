"""What each scope of API token may call (ADR-0010 section 2).

One table, read by the middleware: (method, path pattern, the lowest scope allowed). A route
with no rule is refused to every token, so a new route is closed until someone decides, and a
test fails until it is listed. ``record`` is deliberately less than a local agent can do: a
recorded prompt is untrusted text the recording agent reads, so a prompt-injected agent must not
be able to delete a human's pinned fix, approve a near match into serving, or change settings.
"""

from __future__ import annotations

import re

from .tokens import LEVEL

RULES: tuple[tuple[str, str, str], ...] = (
    # The data plane, for an app's own client.
    ("POST", r"/v1/(projects/[^/]+/)?chat/completions", "replay"),
    ("GET", r"/v1/(projects/[^/]+/)?models(/.+)?", "replay"),
    # MCP: Streamable HTTP. Each tool has its own scope (mcp_server.TOOL_SCOPES); a record token
    # may open a session, and a replay token reaches nothing here.
    ("POST", r"/mcp", "record"),
    ("GET", r"/mcp", "record"),
    ("DELETE", r"/mcp", "record"),
    # Reading, and the agent's recording work.
    ("GET", r"/api/v1/projects", "record"),
    ("GET", r"/api/v1/projects/[^/]+", "record"),
    ("GET", r"/api/v1/projects/[^/]+/entries", "record"),
    ("GET", r"/api/v1/projects/[^/]+/upstream", "record"),
    ("POST", r"/api/v1/projects/[^/]+/entries", "record"),
    ("GET", r"/api/v1/entries/[^/]+", "record"),
    ("GET", r"/api/v1/entries/[^/]+/history", "record"),
    ("PUT", r"/api/v1/entries/[^/]+", "record"),
    ("POST", r"/api/v1/projects/[^/]+/test-match", "record"),
    ("GET", r"/api/v1/pending", "record"),
    ("GET", r"/api/v1/pending/[^/]+", "record"),
    ("GET", r"/api/v1/conversations", "record"),
    ("POST", r"/api/v1/pending/claim", "record"),
    ("POST", r"/api/v1/pending/[^/]+/answer", "record"),
    ("GET", r"/api/v1/log", "record"),
    ("GET", r"/api/v1/log/stats", "record"),
    ("GET", r"/api/v1/log/stream", "record"),
    ("GET", r"/api/v1/candidates", "record"),
    ("GET", r"/api/v1/candidates/[^/]+", "record"),
    ("GET", r"/api/v1/mode", "record"),
    ("GET", r"/api/v1/embeddings", "record"),
    ("GET", r"/api/v1/usage", "record"),
    # What a human decides: destroying, approving into serving, settings, mode, moving data.
    ("POST", r"/api/v1/projects", "admin"),
    ("PATCH", r"/api/v1/projects/[^/]+", "admin"),
    ("DELETE", r"/api/v1/projects/[^/]+", "admin"),
    ("DELETE", r"/api/v1/entries/[^/]+", "admin"),
    ("POST", r"/api/v1/candidates/[^/]+/approve", "admin"),
    ("POST", r"/api/v1/candidates/[^/]+/reject", "admin"),
    ("PUT", r"/api/v1/mode", "admin"),
    ("POST", r"/api/v1/projects/[^/]+/upstream/reset", "admin"),
    ("GET", r"/api/v1/projects/[^/]+/export", "admin"),
    ("POST", r"/api/v1/import", "admin"),
)
_COMPILED = tuple((m, re.compile(p), s) for m, p, s in RULES)

# Never for a token, whatever its scope: only a person (or local mode) manages credentials.
PEOPLE_ONLY = re.compile(r"/api/v1/(tokens|audit)(/.*)?")

PROJECT_IN_PATH = re.compile(r"/(?:api/v1|v1)/projects/([^/]+)")


def required_scope(method: str, path: str) -> str | None:
    """The lowest token scope that may call ``method path``, or None if no token may."""
    for rule_method, pattern, scope in _COMPILED:
        if rule_method == method.upper() and pattern.fullmatch(path.rstrip("/") or "/"):
            return scope
    return None


def allows(token_scope: str | None, needed: str | None) -> bool:
    return token_scope in LEVEL and needed in LEVEL and LEVEL[token_scope] >= LEVEL[needed]
