"""Text an attacker can store must never run in another person's dashboard (SEM-114).

A hosted dashboard shows other people's text to a person who trusts it: prompts and answers that
an app wrote, names a person chose at a sign-in provider, the email of an invitation, the name of
a token, a project, a workspace. Every one of those fields is planted here with a payload that
tries seven ways to run script, then every screen is opened in Chromium as an admin and checked:
nothing ran, nothing was built from the text, no link carries a script, and the page's security
policy was never asked to block anything. Each screen must also *show* the payload as text, so
a screen that silently drops it cannot pass for safe.
"""

from __future__ import annotations

import json
import time
import urllib.parse
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from semula import canonical
from semula.config import Settings
from semula.db import Database
from semula.oidc import Identity, Provider
from semula.scope import Scope
from semula.service import Service
from tests.support.db import fresh_database_url
from tests.support.server import running_server

pytestmark = pytest.mark.browser

PROVIDER = Provider("google", "oidc", "client", "secret", issuer="https://idp.test")
SECRET = "a-secret-of-at-least-sixteen-bytes"  # noqa: S105
COOKIE = "semula_session"  # http: no __Host- prefix
VECTORS = (
    "<script>window.__xss='script'</script>",
    "<img src=x onerror=\"window.__xss='img'\">",
    '"><svg onload="window.__xss=\'svg\'">',
    "[link](javascript:window.__xss='md')",
    "<a href=\"javascript:window.__xss='a'\">anchor</a>",
    "</script><script>window.__xss='breakout'</script>",
    "'+(window.__xss='quote')+'",
    "[sign in again](https://evil.example/login)",
)


def payload(tag: str) -> str:
    """A visible marker, then every vector. The marker is how a test knows the text was shown."""
    return f"XSS{tag} " + " ".join(VECTORS)


def short(tag: str) -> str:
    """For an email address: 254 characters at most, and lower case once stored."""
    return f"xss{tag}" + VECTORS[1] + VECTORS[2]


def tiny(tag: str) -> str:
    """For a name limited to 80 characters."""
    return f"xss{tag}<img src=x onerror=window.__xss=1>"


class Seeded:
    def __init__(self, url: str, cookies: dict[str, str], markers: dict[str, list[str]]) -> None:
        self.url = url
        self.cookies = cookies
        self.markers = markers


def plant(tmp_path: Path) -> tuple[Settings, str, dict[str, str], dict[str, list[str]], Any]:
    db_url = fresh_database_url(tmp_path)
    service = Service(Database(db_url), Settings(data_dir=tmp_path, hosted=True))
    service.create_workspace("acme", name=payload("workspace"))
    acme = service.for_scope(Scope("acme"))
    ada = service.sign_in_identity(
        Identity("https://idp.test", "ada", "ada@example.com", True, payload("person"))
    )
    invite = acme.create_invitation("ada@example.com", "admin")
    service.accept_invitation(invite["token"], ada["id"])
    secret, _ = service.create_session(ada["id"])
    # a second person whose name and email are hostile: everyone else sees them in Members
    mallory = service.sign_in_identity(
        Identity("https://idp.test", "mal", f"m{short('email')}@example.com", True, payload("name"))
    )
    again = acme.create_invitation(f"m{short('email')}@example.com", "viewer")
    service.accept_invitation(again["token"], mallory["id"])
    acme.create_invitation(f"waiting{short('invitee')}@example.com", "editor")
    acme.create_project("shop", name=payload("project"))
    acme.create_token(tiny("token"), "record", project="shop")

    tools = [
        {
            "type": "function",
            "function": {
                "name": "lookup",
                "description": payload("tooldesc"),
                "parameters": {
                    "type": "object",
                    "properties": {"q": {"type": "string", "description": payload("param")}},
                },
            },
        }
    ]
    request = {
        "model": payload("model"),
        "tools": tools,
        "messages": [
            {"role": "system", "content": payload("system")},
            {"role": "user", "content": payload("user")},
            {"role": "assistant", "content": payload("assistant")},
            {"role": "user", "content": payload("last")},
        ],
    }
    acme.open_pending(canonical.from_openai(request), "shop")
    entry_request = {
        "messages": [{"role": "user", "content": payload("entryprompt")}],
        "tools": tools,
    }
    acme.upsert_entry(
        "shop",
        entry_request,
        {
            "content": payload("entryanswer"),
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": json.dumps({"q": payload("args")})},
                }
            ],
        },
    )
    markers = {
        "log": ["XSSentryprompt", "XSSmiss"],
        "pending": ["XSSsystem", "XSSuser", "XSSlast"],
        "review": ["XSSentryprompt"],
        "playground": ['Nearest recorded prompt: "XSSentryprompt'],
        "entries": ["XSSentryprompt", "XSSentryanswer"],
        "members": ["XSSname", "xssemail", "xssinvitee"],
        "tokens": ["xsstoken"],
        "audit": ["project.create", "invitation.create"],
        "usage": ["Delete this workspace"],
    }
    settings = Settings(
        data_dir=tmp_path,
        hosted=True,
        secret=SECRET,
        auth_providers={"google": PROVIDER},
        database_url=db_url,
        mode="replay",
    )
    return settings, secret, {COOKIE: secret}, markers, acme


@pytest.fixture(scope="module")
def seeded(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Seeded]:
    tmp_path = tmp_path_factory.mktemp("xss")
    settings, _, cookies, markers, acme = plant(tmp_path)
    # a near miss must wait for review, not be served: lift the serving threshold out of reach
    acme.update_project(
        "shop", settings={"bm25_serve_threshold": 1.0, "bm25_review_threshold": 0.2}
    )
    replay = acme.create_token("probe", "replay", project="shop")["token"]
    with running_server(
        tmp_path,
        hosted=True,
        secret=SECRET,
        auth_providers={"google": PROVIDER},
        database_url=settings.database_url,
        mode="replay",
    ) as url:
        for text in (
            payload("entryprompt"),  # a hit: the recorded prompt, word for word
            payload("entryprompt") + " and some more words",  # a near miss: goes to review
            payload("miss"),  # a miss
        ):
            httpx.post(
                url + "/v1/chat/completions",
                headers={"Authorization": f"Bearer {replay}"},
                json={"model": "m", "messages": [{"role": "user", "content": text}]},
                timeout=30,
            )
        yield Seeded(url, cookies, markers)


SCREENS = {
    "log": "/ui/projects/shop/log",
    "pending": "/ui/projects/shop/pending",
    "entries": "/ui/projects/shop/entries",
    "review": "/ui/projects/shop/review",
    "playground": "/ui/projects/shop/playground",
    "members": "/ui/workspace/members",
    "tokens": "/ui/workspace/tokens",
    "audit": "/ui/workspace/audit",
    "usage": "/ui/workspace/usage",
}

DOM_AUDIT = """
() => {
  const out = {handlers: [], scriptLinks: [], built: [], inlineScripts: [], foreignLinks: []};
  for (const el of document.querySelectorAll('*')) {
    for (const a of el.attributes) {
      if (a.name.toLowerCase().startsWith('on')) out.handlers.push(el.tagName + '@' + a.name);
      if (['href', 'src', 'action', 'formaction', 'xlink:href'].includes(a.name.toLowerCase())
          && /^\\s*(javascript|data|vbscript):/i.test(a.value)
          && !(el.tagName === 'LINK' && /icon/i.test(el.getAttribute('rel') || ''))) {
        out.scriptLinks.push(el.tagName + '@' + a.name + '=' + a.value.slice(0, 40));
      }
    }
    if (el.tagName === 'A' && el.href && new URL(el.href).origin !== location.origin) {
      out.foreignLinks.push(el.href.slice(0, 60));
    }
    if (el.tagName === 'IMG' && el.getAttribute('src') === 'x') out.built.push('img src=x');
    if (el.tagName === 'SVG' && el.getAttribute('onload')) out.built.push('svg onload');
    if (el.tagName === 'SCRIPT' && !el.src && /__xss/.test(el.textContent)) {
      out.inlineScripts.push(el.textContent.slice(0, 60));
    }
  }
  return out;
}
"""


def settle(page: Any, markers: list[str], timeout: float = 20.0) -> None:
    """Wait until every marker is visible text, so what is audited is what was shown."""
    deadline = time.monotonic() + timeout
    for marker in markers:
        while True:
            body = page.inner_text("body")
            if marker in body:
                break
            if time.monotonic() > deadline:
                raise AssertionError(f"{marker!r} never appeared; the page shows:\n{body[:3000]}")
            page.wait_for_timeout(100)


CLEAN: dict[str, list[str]] = {
    "handlers": [],
    "scriptLinks": [],
    "built": [],
    "inlineScripts": [],
    "foreignLinks": [],
}


def audit(page: Any) -> None:
    page.wait_for_timeout(300)  # a late handler would fire now
    assert page.evaluate("window.__xss") is None, "a payload ran"
    assert page.dialogs == [], f"a dialog opened: {page.dialogs}"
    found = page.evaluate(DOM_AUDIT)
    assert found == CLEAN, found
    assert page.evaluate("window.__csp") == [], "the policy had to block something"
    assert page.console_errors == [], page.console_errors


def open_screen(new_page: Callable[..., Any], seeded: Seeded, path: str) -> Any:
    page = new_page(seeded.url, seeded.cookies)
    page.goto(seeded.url + path)
    return page


def select_first_row(page: Any) -> None:
    """A list screen shows a call or an entry in full once a row is chosen."""
    page.wait_for_selector("tbody tr", timeout=15000)
    page.locator("tbody tr").first.click()


@pytest.mark.parametrize("screen", list(SCREENS))
def test_a_screen_never_runs_stored_text(
    screen: str, seeded: Seeded, new_page: Callable[..., Any]
) -> None:
    page = open_screen(new_page, seeded, SCREENS[screen])
    if screen in ("pending", "entries", "review"):
        select_first_row(page)
    if screen == "playground":
        page.locator(".cm-content").first.click()
        page.keyboard.insert_text(payload("entryprompt"))
        page.wait_for_timeout(500)
        page.get_by_role("button", name="Test it").click()
    settle(page, seeded.markers.get(screen, []))
    audit(page)


def test_the_address_is_not_echoed_as_markup(seeded: Seeded, new_page: Callable[..., Any]) -> None:
    hostile = urllib.parse.quote(VECTORS[1], safe="")
    for path in (f"/ui/projects/{hostile}/log", f"/ui/{hostile}", f"/ui/workspace/{hostile}"):
        page = open_screen(new_page, seeded, path)
        page.wait_for_timeout(1500)
        audit(page)


def test_the_sign_in_page_does_not_echo_its_query(
    seeded: Seeded, new_page: Callable[..., Any]
) -> None:
    hostile = urllib.parse.quote(VECTORS[1], safe="")
    page = new_page(seeded.url)
    for path in (f"/auth/login?next={hostile}&error={hostile}", f"/auth/invite/{hostile}"):
        page.goto(seeded.url + path)
        page.wait_for_timeout(500)
        audit(page)
    assert httpx.get(seeded.url + "/auth/login", timeout=10).status_code == 200


CANARY = """<!doctype html><title>canary</title>
<img src=x onerror="window.__xss='img'">
<a href="javascript:window.__xss=1">js</a><a href="https://evil.example/">out</a>
<div id=b></div>
<script>window.__xss = 'inline'</script>
"""


def test_the_audit_itself_catches_each_thing_it_looks_for(new_page: Callable[..., Any]) -> None:
    """A check that never fails proves nothing: feed it a page with every hazard, under a policy
    that blocks inline script like the dashboard's."""
    page = new_page("http://canary.test")
    page.route(
        "http://canary.test/",
        lambda route: route.fulfill(
            body=CANARY,
            content_type="text/html",
            headers={"Content-Security-Policy": "script-src 'none'"},
        ),
    )
    page.goto("http://canary.test/")
    page.evaluate("document.getElementById('b').innerHTML = '<svg onload=1></svg>'")
    page.wait_for_timeout(300)
    found = page.evaluate(DOM_AUDIT)
    assert found["handlers"], "an on* attribute went unnoticed"
    assert found["scriptLinks"], "a javascript: link went unnoticed"
    assert found["foreignLinks"], "a link to another site went unnoticed"
    assert found["built"], "an element built from text went unnoticed"
    assert page.evaluate("window.__csp"), "a blocked script went unnoticed"
    with pytest.raises(AssertionError):
        audit(page)


FRAMER = (
    '<!doctype html><title>attacker</title><iframe id=f src="{target}" width=600 height=400>'
    "</iframe>"
)


@pytest.mark.parametrize("target", ["/auth/login", "/ui/projects/shop/log"])
def test_another_site_cannot_frame_the_sign_in_page_or_the_dashboard(
    target: str, seeded: Seeded, new_page: Callable[..., Any]
) -> None:
    """Clickjacking: a page on another origin that shows ours in a frame to trick a click.
    ``localhost`` and ``127.0.0.1`` are different origins on the same machine, which keeps the
    browser's own rules about public pages reaching private addresses out of the way."""
    attacker = seeded.url.replace("127.0.0.1", "localhost")
    page = new_page(attacker, seeded.cookies)
    page.route(
        attacker + "/framer",
        lambda route: route.fulfill(
            body=FRAMER.format(target=seeded.url + target), content_type="text/html"
        ),
    )
    page.goto(attacker + "/framer")
    page.wait_for_timeout(2500)
    inner = [f for f in page.frames if f != page.main_frame]
    assert inner, "the attacker page has no frame"
    shown = [f for f in inner if f.url.startswith(seeded.url) and f.locator("body *").count()]
    assert not shown, f"our page was shown inside another site: {[f.url for f in shown]}"


def test_the_framing_check_sees_a_page_that_can_be_framed(
    seeded: Seeded, new_page: Callable[..., Any]
) -> None:
    """The check above passes when nothing is shown, so show it something that can be framed."""
    attacker = seeded.url.replace("127.0.0.1", "localhost")
    page = new_page(attacker)
    page.route(
        attacker + "/framer",
        lambda route: route.fulfill(
            body=FRAMER.format(target=seeded.url + "/health"), content_type="text/html"
        ),
    )
    page.goto(attacker + "/framer")
    page.wait_for_timeout(2000)
    inner = [f for f in page.frames if f != page.main_frame and f.url.startswith(seeded.url)]
    assert inner and inner[0].locator("body *").count(), "a frameable page was not shown"
