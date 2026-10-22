"""The frame around every screen: navigation, project, mode, and the routes."""

from __future__ import annotations

import asyncio
from typing import Any

import indah

from .pages.log import log_page
from .pages.pending import pending_page
from .state import Dash, blocking, every

EMPTY_STATS: dict[str, Any] = {"hit_rate": None, "median_latency_ms": None, "parked": 0}


class Tab:
    """Everything one open tab knows: the project in the URL, the queue counts, the mode."""

    def __init__(self, ctx: indah.Context, dash: Dash) -> None:
        self.ctx = ctx
        self.dash = dash
        self.service = dash.service
        self.project = indah.computed(lambda: ctx.params.value.get("project") or "default")
        self.projects: list[str] = dash.project_ids()
        self.counts: indah.Signal[dict[str, int]] = indah.Signal({"pending": 0, "review": 0})
        # Bumped when this project's pending or review queue moves, for screens that list it.
        self.queues_moved: indah.Signal[int] = indah.Signal(0)
        self.stats: indah.Signal[dict[str, Any]] = indah.Signal(EMPTY_STATS)
        # Whether *this project* records. ``_applied`` is what the service last said, so the
        # switch handler can tell a viewer's click from an update that came from the service.
        self._applied = self.service.default_mode
        self.recording: indah.Signal[bool] = indah.Signal(self._applied == "record")
        self._stale = asyncio.Event()
        self._stale.set()  # the first refresh is due now
        self._follow_events(ctx)
        every(ctx, 1.0, self._refresh)

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
        self.ctx.on_close(task.cancel)

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


def nav(tab: Tab) -> indah.Component:
    def to(page: str) -> Any:
        return lambda: f"/projects/{tab.project.value}/{page}"

    return indah.Column(
        children=[
            indah.Select(_chosen(tab), options=tab.projects, label="Project"),
            indah.Link(tab.ctx, "Live log", to=to("log"), match="prefix"),
            indah.Row(
                children=[
                    indah.Link(tab.ctx, "Pending", to=to("pending"), match="prefix"),
                    indah.Badge(
                        indah.computed(lambda: str(tab.counts.value["pending"])), tone="warn"
                    ),
                ],
                align="center",
            ),
            indah.Switch(tab.recording, label="Record"),
        ]
    )


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


def build_session(ctx: indah.Context, dash: Dash) -> indah.Session:
    tab = Tab(ctx, dash)
    pages = indah.Router(
        ctx,
        routes={
            "/projects/{project}/log": log_page(tab),
            "/projects/{project}/pending": pending_page(tab),
        },
        not_found=indah.Text("### There is nothing at this address.", markdown=True),
    )
    return indah.Session(indah.Sidebar(children=[nav(tab), pages]))
