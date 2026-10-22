"""The token permission table (ADR-0010): what each scope may call."""

import pytest

from semula.app import create_app
from semula.config import Settings
from semula.permissions import PEOPLE_ONLY, RULES, allows, required_scope

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("method", "path", "scope"),
    [
        ("POST", "/v1/chat/completions", "replay"),
        ("POST", "/v1/projects/shop/chat/completions", "replay"),
        ("GET", "/v1/models", "replay"),
        ("GET", "/api/v1/pending", "record"),
        ("POST", "/api/v1/pending/01ABC/answer", "record"),
        ("PUT", "/api/v1/entries/01ABC", "record"),
        ("DELETE", "/api/v1/entries/01ABC", "admin"),
        ("POST", "/api/v1/candidates/01ABC/approve", "admin"),
        ("PUT", "/api/v1/mode", "admin"),
        ("PATCH", "/api/v1/projects/shop", "admin"),
        ("POST", "/api/v1/import", "admin"),
        ("GET", "/api/v1/projects/shop/export", "admin"),
        ("GET", "/api/v1/pending/", "record"),  # a trailing slash is the same route
    ],
)
def test_each_call_needs_its_scope(method: str, path: str, scope: str) -> None:
    assert required_scope(method, path) == scope


def test_unknown_routes_and_methods_are_closed_to_every_token() -> None:
    for method, path in (("GET", "/api/v1/nope"), ("DELETE", "/api/v1/pending"), ("GET", "/mcp/")):
        assert required_scope(method, path) is None
        assert not allows("admin", required_scope(method, path))


def test_the_scopes_include_the_ones_below() -> None:
    assert allows("admin", "record") and allows("record", "replay") and allows("replay", "replay")
    assert not allows("replay", "record") and not allows("record", "admin")
    assert not allows(None, "replay") and not allows("root", "replay")


def test_a_replay_token_reaches_nothing_under_api() -> None:
    for method, path, scope in RULES:
        if path.startswith("/api"):
            assert scope != "replay", (method, path)


def test_a_record_token_cannot_do_what_a_prompt_injected_agent_must_not() -> None:
    """The ceiling of ADR-0010 table: no delete, approve, reject, settings, mode, import."""
    for method, path in (
        ("DELETE", "/api/v1/entries/X"),
        ("DELETE", "/api/v1/projects/p"),
        ("POST", "/api/v1/candidates/X/approve"),
        ("POST", "/api/v1/candidates/X/reject"),
        ("PATCH", "/api/v1/projects/p"),
        ("PUT", "/api/v1/mode"),
        ("POST", "/api/v1/import"),
    ):
        assert not allows("record", required_scope(method, path)), (method, path)


def test_every_route_of_the_app_has_a_rule(tmp_path: object) -> None:
    """A new route is refused to tokens until it is listed, and this test says so."""
    app = create_app(Settings(data_dir=tmp_path))  # type: ignore[arg-type]
    unlisted = []
    for path, operations in app.openapi()["paths"].items():
        if not path.startswith(("/v1", "/api/v1")) or PEOPLE_ONLY.fullmatch(path):
            continue
        concrete = path.replace("{", "").replace("}", "")  # a placeholder is one path segment
        for method in operations:
            if required_scope(method, concrete) is None:
                unlisted.append(f"{method.upper()} {path}")
    assert unlisted == [], f"routes with no token permission rule: {unlisted}"
