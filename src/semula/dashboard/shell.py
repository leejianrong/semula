"""The frame around every screen: navigation, project, mode, and the routes."""

from __future__ import annotations

import asyncio
from typing import Any

import indah

from .pages.log import log_page
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
        self.stats: indah.Signal[dict[str, Any]] = indah.Signal(EMPTY_STATS)
        self.recording: indah.Signal[bool] = indah.Signal(self.service.mode == "record")
        self._stale = asyncio.Event()
        self._stale.set()  # the first refresh is due now
        self._follow_events(ctx)
        every(ctx, 1.0, self._refresh)

    def _follow_events(self, ctx: indah.Context) -> None:
        latest = ctx.latest(self.dash.events, initial=None)
        calls = ctx.latest(self.dash.calls, initial=None)

        def on_event() -> None:
            event = latest.value
            if event and event.get("type") == "mode":
                self.recording.set(event["mode"] == "record")
            if event:
                self._stale.set()

        def on_call() -> None:
            if calls.value is not None:
                self._stale.set()

        indah.effect(on_event)
        indah.effect(on_call)
        indah.effect(self._push_mode)

    def _push_mode(self) -> None:
        """The viewer chose a mode: tell the service. The service then announces it, and every
        tab (this one included) already agrees, so that does not loop."""
        chosen = "record" if self.recording.value else "replay"
        if chosen != self.service.mode:
            self.service.set_mode(chosen)

    async def _refresh(self) -> None:
        """Count again, at most once a second however many events arrived."""
        if not self._stale.is_set():
            return
        self._stale.clear()
        project = self.project.peek()
        self.counts.set(await blocking(self.service.queue_counts, project))
        self.stats.set(await blocking(self.service.log_stats, project, "30m"))


def nav(tab: Tab) -> indah.Component:
    def to(page: str) -> Any:
        return lambda: f"/projects/{tab.project.value}/{page}"

    return indah.Column(
        children=[
            indah.Select(_chosen(tab), options=tab.projects, label="Project"),
            indah.Link(tab.ctx, "Live log", to=to("log"), match="prefix"),
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
        routes={"/projects/{project}/log": log_page(tab)},
        not_found=indah.Text("### There is nothing at this address.", markdown=True),
    )
    return indah.Session(indah.Sidebar(children=[nav(tab), pages]))
