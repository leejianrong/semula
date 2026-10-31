"""The frame around every screen: navigation, project, mode, and the routes."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from functools import partial
from typing import Any

import indah

from ..service import SLUG, Service
from .pages.entries import entries_page
from .pages.entry import entry_page
from .pages.log import log_page
from .pages.pending import pending_page
from .pages.playground import playground_page
from .pages.review import review_page
from .pages.workspace import audit_page, members_page, tokens_page, usage_page
from .state import Dash, blocking, cancel, every

EMPTY_STATS: dict[str, Any] = {
    "hit_rate": None,
    "median_latency_ms": None,
    "parked": 0,
    "totals": {"forwarded": 0},
    "upstream": {"calls": 0},
}


class Tab:
    """Everything one open tab knows: the project in the URL, the queue counts, the mode."""

    def __init__(
        self,
        ctx: indah.Context,
        dash: Dash,
        service: Service,
        who: indah.Principal | None = None,
        public_url: str = "",
    ) -> None:
        self.ctx = ctx
        self.dash = dash
        self.service = service  # as the person acting (hosted), else the server itself
        self.who = who  # the signed-in person when hosted; None locally, where all is allowed
        self.public_url = public_url  # for links an admin sends (an invitation)
        # an address that cannot be a project name (indah refuses it as a link) opens the default
        self.project = indah.computed(lambda: _project_in(ctx.params.value.get("project")))
        self.projects: list[str] = dash.project_ids()
        self.counts: indah.Signal[dict[str, int]] = indah.Signal({"pending": 0, "review": 0})
        # Bumped when this project's pending or review queue moves, for screens that list it.
        self.queues_moved: indah.Signal[int] = indah.Signal(0)
        self.entries_moved: indah.Signal[int] = indah.Signal(0)  # same, for the entries list
        self.stats: indah.Signal[dict[str, Any]] = indah.Signal(EMPTY_STATS)
        # Whether *this project* records. ``_applied`` is what the service last said, so the
        # switch handler can tell a viewer's click from an update that came from the service.
        self._applied = self.service.default_mode
        self.recording: indah.Signal[bool] = indah.Signal(self._applied == "record")
        self._stale = asyncio.Event()
        self._stale.set()  # the first refresh is due now
        self._follow_events(ctx)
        every(ctx, 1.0, self._refresh)

    def can(self, perm: str) -> bool:
        """May the person here ``view``, ``edit`` or ``manage``? Locally everything is allowed."""
        return True if self.who is None else bool(self.ctx.can(perm))

    @property
    def administers(self) -> bool:
        """A signed-in admin: members, tokens, audit and usage are hosted-only screens."""
        return self.who is not None and self.can("manage")

    def only(self, perm: str, build: Callable[[], indah.Component]) -> list[indah.Component]:
        """``build()`` for someone who may ``perm``, else nothing: a control that is only hidden is
        still sent to the browser, so a restricted one is not built at all (indah, ADR-0035)."""
        return [build()] if self.can(perm) else []

    def _follow_events(self, ctx: indah.Context) -> None:
        # A ring, not ``ctx.latest``: a signal ignores a value equal to the one it holds, and
        # the service announces "queues moved" with the same event every time.
        ring = ctx.tail(self.dash.events, limit=50)
        calls = ctx.latest(self.dash.calls, initial=None)

        def on_events(kind: str, rows: list[dict[str, Any]]) -> None:
            if kind == "replace":
                return
            for event in rows:
                if event.get("type") == "mode" and event["project"] == self.project.peek():
                    self._applied = event["mode"]
                    self.recording.set(event["mode"] == "record")
                if event.get("type") == "queues" and event["project"] == self.project.peek():
                    self.queues_moved.set(self.queues_moved.peek() + 1)
                if event.get("type") == "entries" and event["project"] == self.project.peek():
                    self.entries_moved.set(self.entries_moved.peek() + 1)
                self._stale.set()  # a default that moved changes every project that follows it

        ring.listen(on_events)

        def on_call() -> None:
            if calls.value is not None:
                self._stale.set()

        def on_project() -> None:
            self.project.value  # noqa: B018 (reading it is the point: re-count and re-read the mode)
            self._stale.set()

        indah.effect(on_call)
        indah.effect(on_project)
        indah.effect(self._push_mode)

    def _push_mode(self) -> None:
        """The viewer flipped the switch: save it for this project. The service then announces
        it and every tab agrees already, so that does not loop."""
        want = "record" if self.recording.value else "replay"
        if want == self._applied:
            return
        self._applied = want
        project = self.project.peek()
        task = asyncio.get_running_loop().create_task(
            blocking(self.service.set_mode, want, project)
        )
        self.ctx.on_close(partial(cancel, task))

    async def _refresh(self) -> None:
        """Count again, at most once a second however many events arrived."""
        if not self._stale.is_set():
            return
        self._stale.clear()
        project = self.project.peek()
        mode = await blocking(self.service.mode_of, project)
        self._applied = mode
        self.recording.set(mode == "record")
        self.counts.set(await blocking(self.service.queue_counts, project))
        self.stats.set(await blocking(self.service.log_stats, project, "30m"))


def _project_in(address: str | None) -> str:
    return address if address and SLUG.fullmatch(address) else "default"


def nav(tab: Tab) -> indah.Component:
    def to(page: str) -> Any:
        return lambda: f"/projects/{tab.project.value}/{page}"

    return indah.Column(
        children=[
            indah.Select(_chosen(tab), options=tab.projects, label="Project"),
            indah.Link(tab.ctx, "Live log", to=to("log"), match="prefix"),
            indah.Link(tab.ctx, "Entries", to=to("entries"), match="prefix"),
            *tab.only(
                "edit",
                lambda: indah.Link(tab.ctx, "Playground", to=to("playground"), match="prefix"),
            ),
            indah.Row(
                children=[
                    indah.Link(tab.ctx, "Review", to=to("review"), match="prefix"),
                    indah.Badge(
                        indah.computed(lambda: str(tab.counts.value["review"])), tone="warn"
                    ),
                ],
                align="center",
            ),
            indah.Row(
                children=[
                    indah.Link(tab.ctx, "Pending", to=to("pending"), match="prefix"),
                    indah.Badge(
                        indah.computed(lambda: str(tab.counts.value["pending"])), tone="warn"
                    ),
                ],
                align="center",
            ),
            *tab.only("edit", lambda: indah.Switch(tab.recording, label="Record")),
            *workspace_links(tab),
        ]
    )


def workspace_links(tab: Tab) -> list[indah.Component]:
    """Admins only: the workspace's own screens, not tied to a project."""
    if not tab.administers:
        return []
    return [
        indah.Text("**Workspace**", markdown=True, size="body"),
        indah.Link(tab.ctx, "Members", to="/workspace/members", match="prefix"),
        indah.Link(tab.ctx, "Tokens", to="/workspace/tokens", match="prefix"),
        indah.Link(tab.ctx, "Audit", to="/workspace/audit", match="prefix"),
        indah.Link(tab.ctx, "Usage", to="/workspace/usage", match="prefix"),
    ]


def _chosen(tab: Tab) -> indah.Signal[str]:
    chosen: indah.Signal[str] = indah.Signal(tab.project.value)
    armed = False

    def sync_to_url() -> None:
        if tab.project.value in tab.projects and chosen.peek() != tab.project.value:
            chosen.set(tab.project.value)

    def sync_from_switcher() -> None:
        nonlocal armed
        name = chosen.value
        if armed and name != tab.project.peek():
            page = (tab.ctx.path.peek().split("/") + ["", "", "", "log"])[3] or "log"
            tab.ctx.navigate(f"/projects/{name}/{page}")
        armed = True

    indah.effect(sync_to_url)
    indah.effect(sync_from_switcher)
    return chosen


def build_session(
    ctx: indah.Context,
    dash: Dash,
    service: Service,
    who: indah.Principal | None = None,
    public_url: str = "",
) -> indah.Session:
    tab = Tab(ctx, dash, service, who, public_url)
    routes: dict[str, indah.Component] = {
        "/projects/{project}/log": log_page(tab),
        "/projects/{project}/pending": pending_page(tab),
        "/projects/{project}/review": review_page(tab),
        "/projects/{project}/entries": entries_page(tab),
        "/projects/{project}/entries/{entry}": entry_page(tab),
    }
    if tab.can("edit"):  # test_match is an editor's tool
        routes["/projects/{project}/playground"] = playground_page(tab)
    if tab.administers:  # an admin's, and only built for one
        routes["/workspace/members"] = members_page(tab)
        routes["/workspace/tokens"] = tokens_page(tab)
        routes["/workspace/audit"] = audit_page(tab)
        routes["/workspace/usage"] = usage_page(tab)
    pages = indah.Router(
        ctx,
        routes=routes,
        not_found=indah.Text("### There is nothing at this address.", markdown=True),
    )
    side = [account(tab, who), nav(tab)] if who is not None else [nav(tab)]
    return indah.Session(indah.Sidebar(children=[indah.Column(children=side), pages]))


def account(tab: Tab, who: indah.Principal) -> indah.Component:
    """Who is signed in, where, in which role, and the way out."""
    role = next(iter(who.roles), "")

    async def sign_out() -> None:
        # End this device's sign-in, then every open tab of it; the next request is not signed in.
        await blocking(tab.service.end_session_id, str(who.claims.get("session_id", "")))
        tab.ctx.sign_out()

    return indah.Column(
        children=[
            # plain text: a name is chosen at the sign-in provider, so it is never markdown (a link
            # in it would be a live link in this person's sidebar on every screen)
            indah.Text(who.name or who.email, size="body"),
            indah.Text(f"{role} in {who.workspace}", size="body"),
            *workspace_switcher(tab, who),
            indah.Button("Sign out", sign_out, variant="outlined"),
        ]
    )


def workspace_switcher(tab: Tab, who: indah.Principal) -> list[indah.Component]:
    """A person in several workspaces picks the one to work in. Their sign-in remembers it, but
    a tab is signed for one workspace, so this tab is ended and the page must be reloaded: a
    server cannot reload a tab (indah has no such message), so it says so."""
    mine = [w["workspace_id"] for w in tab.service.workspaces_of(who.id)]
    if len(mine) < 2:
        return []
    chosen: indah.Signal[str] = indah.Signal(str(who.workspace))
    note: indah.Signal[str] = indah.Signal("")

    async def switch() -> None:
        target = chosen.peek()
        if target == who.workspace:
            return
        done = await blocking(
            tab.service.switch_session, str(who.claims.get("session_id", "")), target
        )
        if not done:
            chosen.set(str(who.workspace))
            note.set(f"You do not belong to {target}.")
            return
        note.set(f"Now working in {target}. Reload this page to continue.")
        tab.ctx.sign_out()  # this tab (and the person's others) end; the reload starts afresh

    def on_pick() -> None:
        chosen.value  # noqa: B018 (reading it is the point: act when the person picks)
        tab.ctx.on_close(partial(cancel, asyncio.get_running_loop().create_task(switch())))

    indah.effect(on_pick)
    return [
        indah.Select(chosen, options=mine, label="Workspace"),
        indah.Text(lambda: note.value, size="body"),
    ]


def build_waiting_session(ctx: indah.Context, who: indah.Principal | None) -> indah.Session:
    """A signed-in person who belongs to no workspace yet can do nothing but wait for a link."""
    name = who.email if who is not None else ""
    return indah.Session(
        indah.Column(
            children=[
                indah.Text("# No workspace yet", markdown=True),
                indah.Text(
                    f"You are signed in as {name}, but you do not belong to a workspace. Ask an "
                    "admin to invite you, and open the link they send.",
                    size="body",
                ),
                indah.Text(
                    "Or, if you were sent an invite code, [enter it here](/auth/signup).",
                    markdown=True,
                    size="body",
                ),
            ]
        )
    )
