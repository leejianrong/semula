"""The dashboard is usable on a phone and with the keyboard alone (SEM-124).

At 390 px wide no screen may push the page sideways, and the two jobs a person does most can be
finished at that width: answer a parked call, and make a token. The same two jobs are then done
without a mouse, in every engine (Chromium, Firefox, and WebKit where its libraries exist).
"""

from __future__ import annotations

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
playwright_sync = pytest.importorskip("playwright.sync_api")

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


def test_the_mode_sits_in_the_top_bar_and_pause_in_the_logs_own_toolbar(
    site: Site, new_page: Callable[..., Any]
) -> None:
    """Record has one home (beside the project switcher), Live is Pause/Resume above the table,
    and neither is a switch (SEM-132)."""
    page = new_page(site.url, site.cookies)
    page.goto(site.url + SCREENS["log"])
    page.get_by_text("Replay", exact=True).first.wait_for(timeout=30000)
    assert page.get_by_role("switch").count() == 0
    replay = page.get_by_text("Replay", exact=True).first.bounding_box()
    heading = page.get_by_text("Live log", exact=True).last.bounding_box()
    assert replay and heading and replay["y"] < heading["y"], (
        "mode is above the page, in the top bar"
    )
    pause = page.get_by_role("button", name="Pause")
    pause.wait_for()
    table = page.get_by_label("Calls").first.bounding_box()
    box = pause.bounding_box()
    assert box and table and box["y"] < table["y"], "Pause sits above the table it controls"
    pause.click()
    page.get_by_text("Paused", exact=False).first.wait_for()
    page.get_by_role("button", name="Resume").click()
    page.get_by_text("Record", exact=True).first.click()
    for _ in range(50):
        if site.acme.mode_of("shop") == "record":
            break
        page.wait_for_timeout(100)
    assert site.acme.mode_of("shop") == "record"


@pytest.mark.parametrize("screen", ["members", "tokens", "audit", "usage"])
def test_a_workspace_screen_has_no_project_switcher_and_no_mode(
    screen: str, site: Site, new_page: Callable[..., Any]
) -> None:
    """Members, tokens, audit and usage belong to the workspace, so the project and the mode
    (which would change a project by accident) are for project screens only (SEM-148)."""
    page = new_page(site.url, site.cookies)
    page.goto(site.url + SCREENS["log"])
    page.get_by_label("Project", exact=True).wait_for(timeout=30000)
    page.get_by_text("Replay", exact=True).first.wait_for()
    page.goto(site.url + SCREENS[screen])
    page.get_by_role("heading", name=screen.capitalize()).first.wait_for(timeout=30000)
    page.wait_for_timeout(500)
    assert not page.get_by_label("Project", exact=True).is_visible()
    assert not page.get_by_text("Replay", exact=True).first.is_visible()
    assert not page.get_by_text("Record", exact=True).first.is_visible()


def test_a_refusal_is_a_sentence_in_the_error_colour_with_no_code_and_no_trace_when_none(
    site: Site, new_page: Callable[..., Any]
) -> None:
    """An empty error line takes no room, a refused invitation marks its field and says what
    to fix, and the code (invalid_email) never reaches the screen (SEM-149)."""
    page = new_page(site.url, site.cookies)
    page.goto(site.url + SCREENS["members"])
    page.get_by_label("Email", exact=True).wait_for(timeout=30000)
    page.wait_for_timeout(500)
    assert (
        page.locator(".badge.tone-err").evaluate_all(
            "els => els.filter(e => e.getBoundingClientRect().height > 0).length"
        )
        == 0
    ), "an empty error line is drawn"
    page.get_by_label("Email", exact=True).fill("not-an-address")
    page.get_by_role("button", name="Send invitation").click()
    page.get_by_text("That is not an email address.").wait_for(timeout=10000)
    body = page.inner_text("body")
    assert "invalid_email" not in body and "(at email)" not in body
    assert page.get_by_label("Email", exact=True).get_attribute("aria-invalid") == "true"
    page.get_by_role("button", name="Make viewer").first.click()
    page.get_by_text("Make someone else an admin first.", exact=False).wait_for(timeout=10000)
    assert "last_admin" not in page.inner_text("body")


def test_the_account_is_one_quiet_row_and_sign_out_is_a_link_at_the_bottom_of_the_rail(
    site: Site, new_page: Callable[..., Any]
) -> None:
    """Name and role sit on one line above the navigation; Sign out is plain text under it, not
    the biggest control in the rail (SEM-150)."""
    page = new_page(site.url, site.cookies)
    page.goto(site.url + SCREENS["log"])
    rail = page.locator("aside.rail")
    rail.get_by_text("Ada", exact=True).wait_for(timeout=30000)
    who = rail.get_by_text("Ada", exact=True).bounding_box()
    role = rail.get_by_text("admin in acme", exact=True).bounding_box()
    live = rail.get_by_role("link", name="Live log").bounding_box()
    out = rail.get_by_role("button", name="Sign out")
    box = out.bounding_box()
    last = rail.get_by_role("link", name="Usage").bounding_box()
    assert who and role and live and box and last
    assert abs(who["y"] - role["y"]) < 12, "the name and the role are one row"
    assert who["y"] + who["height"] < live["y"], "the account row is above the navigation"
    assert box["y"] > last["y"], "Sign out is under the navigation"
    look = out.evaluate(
        "e => { const c = getComputedStyle(e); return [c.borderTopWidth, c.backgroundColor,"
        " c.textDecorationLine]; }"
    )
    assert look[0] == "0px" and look[2] == "underline", f"Sign out is not a text link: {look}"
    assert look[1] in ("rgba(0, 0, 0, 0)", "transparent")
    out.click()
    page.wait_for_timeout(
        1000
    )  # the sign-in ends on the server; the next page load is not signed in
    page.goto(site.url + SCREENS["log"])
    assert "/auth/login" in page.url


def test_the_connection_is_silent_until_it_is_lost_and_then_says_so_in_the_header(
    site: Site, new_page: Callable[..., Any]
) -> None:
    """Nothing about the connection shows while it is up; when it drops, a status with words
    appears in the top bar beside the theme switch, and it goes again on reconnect (SEM-139)."""
    page = new_page(site.url, site.cookies)
    page.add_init_script(
        "window.__streams = []; const ES = window.EventSource;"
        "window.EventSource = class extends ES { constructor(...a) "
        "{ super(...a); window.__streams.push(this); } };"
    )
    page.goto(site.url + SCREENS["log"])
    page.get_by_text("Replay", exact=True).first.wait_for(timeout=30000)
    page.wait_for_timeout(1500)
    assert page.locator(".status:not(.status-head)").count() == 0, "no always-on footer line"
    assert page.locator(".status-head").inner_text().strip() == "", "silent while connected"
    # Cut the live stream the way a dropped network does: new attempts fail and the open one errors.
    page.route("**/api/stream*", lambda route: route.abort())
    page.evaluate("window.__streams.forEach(s => s.dispatchEvent(new Event('error')))")
    status = page.locator(".status-head")
    for _ in range(300):
        if status.inner_text().strip():
            break
        page.wait_for_timeout(100)
    assert status.inner_text().strip(), "the lost status has words"
    head = page.locator(".theme-toggle").bounding_box()
    mine = status.bounding_box()
    assert head and mine and mine["y"] < 80 and abs(mine["y"] - head["y"]) < 40, "in the header"
    page.unroute("**/api/stream*")
    for _ in range(300):
        if not status.inner_text().strip():
            break
        page.wait_for_timeout(100)
    assert not status.inner_text().strip(), "silent again once reconnected"


def call_in(site: Site, token: str, text: str) -> None:
    httpx.post(
        site.url + "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={"model": "m", "messages": [{"role": "user", "content": text}]},
        timeout=30,
    )


def test_the_live_log_has_the_newest_call_at_the_top(
    site: Site, new_page: Callable[..., Any]
) -> None:
    """A log reads newest first, for the calls already there and for each one that arrives
    while the page is open (SEM-141, indah `newest_first`)."""
    token = site.acme.create_token("probe", "replay", project="shop")["token"]
    for text in ("zero call", "one call", "two call"):
        call_in(site, token, text)
    page = new_page(site.url, site.cookies)
    page.goto(site.url + SCREENS["log"])
    rows = page.locator("tbody tr")
    rows.first.wait_for(timeout=30000)
    page.wait_for_timeout(500)
    assert "two call" in rows.first.inner_text()
    assert "zero call" in rows.last.inner_text()
    call_in(site, token, "three call")
    page.get_by_text("three call").first.wait_for(timeout=15000)
    assert "three call" in rows.first.inner_text()


@pytest.mark.parametrize(
    ("path", "name"),
    [
        (SCREENS["review"], "Reject"),
        (SCREENS["usage"], "Delete workspace"),
    ],
)
def test_reject_and_delete_are_set_apart_as_danger_buttons(
    path: str, name: str, site: Site, new_page: Callable[..., Any]
) -> None:
    """Reject and Delete are the dashboard's two ways to throw something away; they use indah's
    danger button, and no screen still hand-styles them as ghost buttons (SEM-141)."""
    page = new_page(site.url, site.cookies)
    page.goto(site.url + path)
    page.get_by_role("button", name=name).wait_for(timeout=30000)
    classes = page.get_by_role("button", name=name).first.get_attribute("class") or ""
    assert "danger" in classes.split(), classes


def test_delete_entry_is_a_danger_button_apart_from_save(
    site: Site, new_page: Callable[..., Any]
) -> None:
    page = new_page(site.url, site.cookies)
    page.goto(site.url + entry_path(site))
    button = page.get_by_role("button", name="Delete entry")
    button.wait_for(timeout=30000)
    assert "danger" in (button.get_attribute("class") or "").split()


def test_the_design_faces_render_and_no_font_comes_from_another_host(
    site: Site, new_page: Callable[..., Any]
) -> None:
    """Body is Hanken Grotesk, headings Bricolage Grotesque, data IBM Plex Mono, each really
    loaded; every request goes to our own origin (SEM-135)."""
    page = new_page(site.url, site.cookies)
    requests: list[str] = []
    page.on("request", lambda r: requests.append(r.url))
    response = page.goto(site.url + SCREENS["log"])
    policy = response.headers.get("content-security-policy", "")
    assert "font-src 'self' data:;" in policy, "the policy still allows fonts from nowhere else"
    page.get_by_text("Replay", exact=True).first.wait_for(timeout=30000)
    page.get_by_text("Calls", exact=True).first.wait_for(timeout=30000)
    for _ in range(150):  # the strict policy forbids wait_for_function's string eval
        if page.evaluate(
            "[...document.fonts].some(f => f.family.includes('Hanken') && f.status === 'loaded')"
        ):
            break
        page.wait_for_timeout(100)
    first = page.evaluate(
        """() => {
          const family = (el) => getComputedStyle(el).fontFamily.split(',')[0].replace(/"/g, '');
          const heading = [...document.querySelectorAll('h1, h2')].find(h => h.offsetParent);
          const mono = document.querySelector('th, td.mono, .t-mono');
          return {
            body: family(document.body),
            heading: heading && family(heading),
            mono: mono && family(mono),
            loaded: [...document.fonts].filter(f => f.status === 'loaded').map(f => f.family),
          };
        }"""
    )
    assert first["body"] == "Hanken Grotesk", first
    assert first["heading"] == "Bricolage Grotesque", first
    assert first["mono"] == "IBM Plex Mono", first
    assert any("Hanken" in f for f in first["loaded"]), first
    assert any("Bricolage" in f for f in first["loaded"]), first
    assert any("Plex Mono" in f for f in first["loaded"]), first
    own = site.url.split("//", 1)[1]
    elsewhere = [u for u in requests if not u.startswith(("data:", "blob:")) and own not in u]
    assert elsewhere == [], f"requests to another host: {elsewhere}"
    assert any(u.endswith(".woff2") and "/api/fonts/" in u for u in requests), requests


FILLED = """() => [...document.querySelectorAll('button')]
  .filter(b => !b.closest(
      '.theme-toggle, .segmented, [role=radiogroup], table, header, .select, .field')
    && !b.hasAttribute('aria-haspopup') && !b.hasAttribute('aria-expanded')
    && !b.matches('.ghost, .tonal, .danger') && b.offsetParent !== null && b.innerText.trim())
  .map(b => b.innerText.trim())"""


@pytest.mark.parametrize("screen", ["log", "pending", "review", "entries", "playground"])
def test_each_screen_has_at_most_one_filled_button(
    screen: str, site: Site, new_page: Callable[..., Any]
) -> None:
    """The one action of a view is drawn filled; every other button is ghost or tonal."""
    page = new_page(site.url, site.cookies)
    page.goto(site.url + SCREENS[screen])
    page.wait_for_selector("h1", timeout=20000)
    page.wait_for_timeout(800)
    filled = page.evaluate(FILLED)
    assert len(filled) <= 1, f"{screen} has several filled buttons: {filled}"


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
    page.keyboard.insert_text(text)
    # Send is disabled until the server holds the text (the editor's input and a click are two
    # requests with no order); click() waits for it to enable, as a person would.
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


SLOW_INPUT = """
const send = window.fetch;
window.fetch = (url, init) => {
  if (init && typeof init.body === 'string' && init.body.includes('"event":"input"'))
    return new Promise((done) => setTimeout(() => done(send(url, init)), 700));
  return send(url, init);
};
"""


def test_send_waits_for_the_typed_text_to_reach_the_server(
    site: Site, new_page: Callable[..., Any]
) -> None:
    """The editor's text and the Send click travel as separate requests (SEM-151). With the text
    held back for 700 ms, Send must stay disabled until it lands, then send it: never answer
    "Write an answer first." beside text the page shows."""
    site.park("what is two plus two")
    page = open_on_phone(new_page, site, SCREENS["pending"])
    page.context.add_init_script(SLOW_INPUT)
    page.reload()
    page.wait_for_selector("tbody tr", timeout=30000)
    page.locator("tbody tr").first.click()
    page.locator("[contenteditable=true], textarea").first.click()
    send = page.get_by_role("button", name="Send answer")
    assert send.is_disabled(), "nothing written yet"
    page.keyboard.insert_text("four")
    send.click()  # waits until the server has the text and enables the button
    see(page, "Answered")
    assert "Write an answer first" not in page.inner_text("body")
    assert site.acme.list_pending("pending", "shop") == []


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
    # Send enables once the server holds the text; Tab skips it until then
    playwright_sync.expect(page.get_by_role("button", name="Send answer")).to_be_enabled()
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


def test_the_skip_link_is_the_first_stop_and_moves_focus_into_the_page(
    site: Site, new_page: Callable[..., Any]
) -> None:
    """One keypress passes the navigation (SEM-137): the link shows when focused, then Enter
    lands focus in the main region, so the next Tab is the first control of the screen."""
    page = open_on_phone(new_page, site, SCREENS["log"])
    page.keyboard.press("Tab")
    link = page.get_by_role("link", name="Skip to content")
    box = link.bounding_box()
    assert box and 0 <= box["x"] < PHONE[0] and 0 <= box["y"] < PHONE[1], "it is on screen"
    page.keyboard.press("Enter")
    assert page.evaluate("document.activeElement.id") == "main"
    assert page.evaluate("location.hash") == "", "a hash would be read as an app path"
    page.keyboard.press("Tab")
    assert page.evaluate("!!document.activeElement.closest('.main')"), "the next Tab is in the page"
    page.keyboard.press("Shift+Tab")
    page.keyboard.press("Shift+Tab")
    assert page.get_by_role("link", name="Skip to content").bounding_box() is not None


RING = """() => { const c = getComputedStyle(document.activeElement);
  return { outline: c.outlineStyle, width: parseFloat(c.outlineWidth),
           glow: c.boxShadow !== 'none', color: c.outlineColor }; }"""


def test_every_control_shows_the_same_focus_ring(site: Site, new_page: Callable[..., Any]) -> None:
    """Navigation links, buttons and the table all show a ring of 2 px or more in the accent
    colour, never the browser's thin default (SEM-137)."""
    page = new_page(site.url, site.cookies)
    page.goto(site.url + SCREENS["tokens"])
    page.get_by_text("Tokens", exact=True).first.wait_for(timeout=30000)
    seen = {}
    for _ in range(40):
        page.keyboard.press("Tab")
        tag = page.evaluate("document.activeElement.tagName.toLowerCase()")
        if tag == "body":
            break
        ring = page.evaluate(RING)
        label = page.evaluate("document.activeElement.innerText || ''")[:20]
        seen[f"{tag}:{label}"] = ring
        if tag in ("a", "button"):
            assert ring["outline"] == "solid" and ring["width"] >= 2, (tag, label, ring)
        elif tag in ("input", "select", "textarea"):
            assert ring["glow"] or ring["width"] >= 2, (tag, label, ring)
    assert any(k.startswith("a:Members") for k in seen), seen  # the nav links were walked


def test_g_then_a_letter_goes_there_and_question_mark_lists_the_keys(
    site: Site, new_page: Callable[..., Any]
) -> None:
    page = new_page(site.url, site.cookies)
    page.goto(site.url + SCREENS["log"])
    page.get_by_text("Press ? for shortcuts").wait_for(timeout=30000)
    page.keyboard.press("g")
    page.keyboard.press("p")
    page.wait_for_url("**/projects/shop/pending")
    page.keyboard.press("?")
    page.get_by_text("Go to review").wait_for(timeout=5000)


# --- the phone layout (SEM-136) -------------------------------------------------------------

SMALL = """() => [...document.querySelectorAll('a,button,input,select,textarea')]
  .filter(e => !e.closest('select'))  // a select's inner button is drawn by the select itself
  .map(e => [e, e.getBoundingClientRect()])
  .filter(([e, r]) => r.width && r.height && r.right > 0 && r.left < innerWidth
                      && getComputedStyle(e).visibility !== 'hidden')
  .filter(([e, r]) => r.width < 44 || r.height < 44)
  .map(([e, r]) => (e.innerText || e.getAttribute('aria-label') || e.tagName).trim().slice(0, 20)
                   + ' ' + Math.round(r.width) + 'x' + Math.round(r.height))"""


@pytest.mark.parametrize("screen", list(SCREENS))
def test_every_control_on_a_phone_is_at_least_44_px(
    screen: str, site: Site, new_page: Callable[..., Any]
) -> None:
    page = open_on_phone(new_page, site, SCREENS[screen])
    small = page.evaluate(SMALL)
    assert small == [], f"{screen}: controls under 44 px: {small}"


@pytest.mark.parametrize(
    ("screen", "names"),
    [("members", ["Remove", "Revoke"]), ("tokens", ["Revoke"])],
)
def test_remove_and_revoke_rows_are_danger_buttons_and_still_44_px_on_a_phone(
    screen: str, names: list[str], site: Site, new_page: Callable[..., Any]
) -> None:
    """Remove (a member) and Revoke (an invitation, a token) throw something away, so they are
    indah's danger button; as card rows at 390 px they keep the 44 px target (SEM-152)."""
    site.acme.create_invitation("new@example.com", "viewer")
    site.acme.create_token("ci", "replay")
    page = open_on_phone(new_page, site, SCREENS[screen])
    page.wait_for_selector("tbody tr", timeout=30000)
    for name in names:
        buttons = page.get_by_role("button", name=name)
        assert buttons.count() >= 1, name
        for i in range(buttons.count()):
            classes = (buttons.nth(i).get_attribute("class") or "").split()
            assert "danger" in classes, (name, classes)
    assert page.evaluate(SMALL) == []
    assert len(page.evaluate(FILLED)) <= 1


def entry_path(site: Site) -> str:
    (found,) = [e for e in site.acme.list_entries("shop")["entries"]]
    return f"/ui/projects/shop/entries/{found['id']}"


def test_the_entry_editor_does_not_scroll_sideways_on_a_phone(
    site: Site, new_page: Callable[..., Any]
) -> None:
    page = new_page(site.url, site.cookies, PHONE)
    page.goto(site.url + entry_path(site))
    see(page, "Save answer")
    page.wait_for_timeout(800)
    assert page.evaluate("document.documentElement.scrollWidth") <= PHONE[0]


def test_tables_are_cards_on_a_phone_and_tables_on_a_desktop(
    site: Site, new_page: Callable[..., Any]
) -> None:
    """Every value is labelled with its column and nothing is cut off at 390 px; at 1440 px the
    same table is a table again."""
    site.acme.create_token("deploy", "replay")
    page = open_on_phone(new_page, site, SCREENS["tokens"])
    page.wait_for_selector("tbody tr td[data-label]", timeout=15000)
    cell = page.locator("tbody tr td[data-label='Scope']").first
    assert cell.evaluate("e => getComputedStyle(e).display") == "flex"
    assert page.evaluate("getComputedStyle(document.querySelector('thead')).display") == "none"
    off = page.evaluate(
        "[...document.querySelectorAll('tbody button')].filter(b => "
        "b.getBoundingClientRect().right > innerWidth).map(b => b.innerText)"
    )
    assert off == [], f"cut off at the right: {off}"
    desktop = new_page(site.url, site.cookies)
    desktop.goto(site.url + SCREENS["tokens"])
    desktop.wait_for_selector("tbody tr", timeout=30000)
    assert desktop.evaluate("getComputedStyle(document.querySelector('thead')).display") != "none"


def test_the_diff_opens_unified_on_a_phone_and_split_on_a_desktop(
    tmp_path: Path, new_page: Callable[..., Any]
) -> None:
    import httpx

    from tests.integration.test_lexical_match import BASE, WEAK, ask

    with running_server(tmp_path, mode="replay") as url:
        base = url + "/api/v1/projects/default/entries"
        httpx.post(base, json={"request": ask(BASE), "response": {"content": "recorded"}})
        httpx.post(url + "/v1/chat/completions", json=ask(WEAK))
        for size, want in ((PHONE, "Unified"), ((1440, 900), "Split")):
            page = new_page(url, None, size)
            page.goto(url + "/ui/projects/default/review")
            page.locator("tbody tr").first.click()
            pressed = page.locator(".diff-mode button[aria-pressed=true]")
            pressed.wait_for(timeout=30000)
            assert pressed.inner_text() == want, size
            page.wait_for_timeout(300)  # the choice is made once, not on every patch
            assert pressed.inner_text() == want, size
