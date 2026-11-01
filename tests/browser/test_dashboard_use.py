"""The dashboard is usable on a phone and with the keyboard alone (SEM-124).

At 390 px wide no screen may push the page sideways, and the two jobs a person does most can be
finished at that width: answer a parked call, and make a token. The same two jobs are then done
without a mouse, in every engine (Chromium, Firefox, and WebKit where its libraries exist).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

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
PHONE = (390, 844)
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


class Site:
    def __init__(self, url: str, cookies: dict[str, str], acme: Service) -> None:
        self.url = url
        self.cookies = cookies
        self.acme = acme

    def park(self, text: str) -> str:
        return str(
            self.acme.open_pending(
                canonical.from_openai({"messages": [{"role": "user", "content": text}]}), "shop"
            )
        )


@pytest.fixture
def site(tmp_path: Path) -> Iterator[Site]:
    db = fresh_database_url(tmp_path)
    service = Service(Database(db), Settings(data_dir=tmp_path, hosted=True))
    service.create_workspace("acme")
    acme = service.for_scope(Scope("acme"))
    ada = service.sign_in_identity(
        Identity("https://idp.test", "ada", "ada@example.com", True, "Ada")
    )
    service.accept_invitation(
        acme.create_invitation("ada@example.com", "admin")["token"], ada["id"]
    )
    secret, _ = service.create_session(ada["id"])
    acme.create_project("shop")
    acme.upsert_entry(
        "shop",
        {"messages": [{"role": "user", "content": "a recorded prompt"}]},
        {"content": "a recorded answer"},
    )
    with running_server(
        tmp_path,
        hosted=True,
        secret=SECRET,
        auth_providers={"google": PROVIDER},
        database_url=db,
        mode="replay",
    ) as url:
        yield Site(url, {"semula_session": secret}, acme)


def open_on_phone(new_page: Callable[..., Any], site: Site, path: str) -> Any:
    page = new_page(site.url, site.cookies, PHONE)
    page.goto(site.url + path)
    page.wait_for_selector("h1, h2", timeout=20000)
    page.wait_for_timeout(800)  # the first screen is built and patched in
    return page


@pytest.mark.parametrize("screen", list(SCREENS))
def test_no_screen_is_wider_than_a_phone(
    screen: str, site: Site, new_page: Callable[..., Any]
) -> None:
    page = open_on_phone(new_page, site, SCREENS[screen])
    width = page.evaluate("document.documentElement.scrollWidth")
    assert width <= PHONE[0], f"{screen} is {width}px wide in a {PHONE[0]}px window"


def test_the_sign_in_pages_fit_a_phone_too(site: Site, new_page: Callable[..., Any]) -> None:
    page = new_page(site.url, None, PHONE)
    for path in ("/auth/login", "/auth/device?code=ABCD-EFGH"):
        page.goto(site.url + path)
        assert page.evaluate("document.documentElement.scrollWidth") <= PHONE[0], path


def see(page: Any, text: str) -> None:
    """Wait for ``text`` on the page, and show what the page said instead if it never comes."""
    try:
        page.get_by_text(text).first.wait_for(timeout=30000)
    except Exception as exc:
        raise AssertionError(
            f"{text!r} never appeared; the page shows:\n{page.inner_text('body')[-1500:]}"
        ) from exc


def answer_by_mouse(page: Any, text: str) -> None:
    page.wait_for_selector("tbody tr", timeout=30000)
    page.locator("tbody tr").first.click()
    editor = page.locator("[contenteditable=true], textarea").first
    editor.click()
    page.keyboard.type(text)
    page.get_by_role("button", name="Send answer").click()


def mint_by_mouse(page: Any, name: str) -> None:
    page.get_by_placeholder("ci runner").fill(name)
    page.get_by_role("button", name="Create token").click()


def test_a_parked_call_is_answered_and_a_token_made_at_phone_width(
    site: Site, new_page: Callable[..., Any]
) -> None:
    site.park("what is two plus two")
    page = open_on_phone(new_page, site, SCREENS["pending"])
    answer_by_mouse(page, "four")
    see(page, "Answered")
    assert site.acme.list_pending("pending", "shop") == []
    page = open_on_phone(new_page, site, SCREENS["tokens"])
    mint_by_mouse(page, "from a phone")
    see(page, "smla_live_")
    assert "from a phone" in [t["name"] for t in site.acme.list_tokens()]


TAB_UNTIL = """(want) => {
  const e = document.activeElement;
  if (!e) return false;
  const text = (e.getAttribute('aria-label') || e.innerText || e.getAttribute('placeholder') || '');
  return want.tag === e.tagName.toLowerCase()
    && (!want.text || text.includes(want.text))
    && (!want.inside || !!e.closest(want.inside));
}"""


def tab_to(page: Any, tag: str, text: str = "", inside: str = "", limit: int = 80) -> None:
    """Press Tab until focus is on the thing wanted: a keyboard user's way to it."""
    want = {"tag": tag, "text": text, "inside": inside}
    seen = []
    for _ in range(limit):
        page.keyboard.press("Tab")
        if page.evaluate(TAB_UNTIL, want):
            return
        seen.append(
            page.evaluate(
                "(() => { const e = document.activeElement; return e ? e.tagName + ':' + "
                "(e.getAttribute('aria-label') || e.innerText || '').slice(0, 20) : 'none' })()"
            )
        )
    raise AssertionError(f"Tab never reached {want}; it visited {seen}")


# Note: the keyboard path is walked with Tab and Enter, but the text is inserted in one input
# event rather than typed key by key. Typed fast, a text box's own sends (after a pause, and on
# blur) race: `by keyboard` came out as `by keyboar` or `by keyboa` in about one run in four, and
# an answer went empty. See docs/INDAH-FINDINGS.md.
def test_a_parked_call_is_answered_and_a_token_made_with_the_keyboard_alone(
    site: Site, new_page: Callable[..., Any]
) -> None:
    site.park("what is two plus two")
    page = open_on_phone(new_page, site, SCREENS["pending"])
    page.wait_for_selector("tbody tr", timeout=30000)
    # into the table, pick the first row, then on to the answer box
    tab_to(page, "table")
    page.keyboard.press("ArrowDown")
    page.keyboard.press("Enter")
    page.get_by_text("Select a parked call to read it.").wait_for(state="detached", timeout=15000)
    tab_to(page, "div", "Answer")  # the editor
    page.keyboard.insert_text("four")
    page.keyboard.press("Control+m")  # Tab indents in the editor: this is how a keyboard leaves
    tab_to(page, "button", "Send answer")
    page.keyboard.press("Enter")
    see(page, "Answered")
    assert site.acme.list_pending("pending", "shop") == []

    page = open_on_phone(new_page, site, SCREENS["tokens"])
    tab_to(page, "input")
    page.keyboard.insert_text("by keyboard")  # one input event: see the note
    tab_to(page, "button", "Create token")
    page.keyboard.press("Enter")
    see(page, "smla_live_")
    assert "by keyboard" in [t["name"] for t in site.acme.list_tokens()]
