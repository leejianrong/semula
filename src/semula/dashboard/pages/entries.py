"""The entries browser: what is recorded in a project, with search, filters and sort."""

from __future__ import annotations

import asyncio
from functools import partial
from typing import TYPE_CHECKING, Any

import indah

from ..rows import (
    ENTRY_FILTERS,
    ENTRY_PAGE,
    ENTRY_SORTS,
    entry_filters,
    entry_row,
    page_text,
)
from ..state import blocking, cancel

if TYPE_CHECKING:
    from ..shell import Tab

DEBOUNCE = 0.2  # seconds of quiet after a keystroke before searching


def entries_page(tab: Tab) -> indah.Component:
    ctx = tab.ctx
    shown: indah.Stream = indah.Stream(limit=ENTRY_PAGE)
    query: indah.Signal[str] = indah.Signal("")
    chip: indah.Signal[str] = indah.Signal("All")
    order: indah.Signal[str] = indah.Signal("Oldest first")
    offset: indah.Signal[int] = indah.Signal(0)
    picked: indah.Signal[Any] = indah.Signal(None)
    summary: indah.Signal[str] = indah.Signal("")
    total: indah.Signal[int] = indah.Signal(0)
    loads = 0  # the newest search wins; an older one that finishes late is dropped

    def spawn(coro: Any) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        ctx.on_close(partial(cancel, task))

    async def reload(project: str, text: str, which: str, sort: str, start: int) -> None:
        nonlocal loads
        loads += 1
        mine = loads
        await asyncio.sleep(DEBOUNCE)
        if mine != loads:
            return
        by, direction = ENTRY_SORTS[sort]
        found = await blocking(
            partial(
                tab.service.list_entries,
                project,
                text or None,
                ENTRY_PAGE,
                start,
                sort=by,
                order=direction,
                **entry_filters(which),
            )
        )
        if mine != loads or project != tab.project.peek():
            return
        rows = [entry_row(e) for e in found["entries"]]
        shown.set(rows)
        total.set(found["total"])
        summary.set(page_text(found["total"], start, len(rows)))

    def first_page() -> None:
        tab.project.value, query.value, chip.value, order.value  # noqa: B018
        offset.set(0)

    def on_change() -> None:
        tab.entries_moved.value  # noqa: B018 (reading it is the point: look again when it moves)
        spawn(reload(tab.project.value, query.value, chip.value, order.value, offset.value))

    indah.effect(first_page)
    indah.effect(on_change)

    def open_entry() -> None:
        key = picked.value
        if key is not None:
            ctx.navigate(f"/projects/{tab.project.peek()}/entries/{key}")
            picked.set(None)

    indah.effect(open_entry)

    def step(by: int) -> None:
        offset.set(max(0, offset.peek() + by))

    def previous() -> None:
        step(-ENTRY_PAGE)

    def following() -> None:
        if offset.peek() + ENTRY_PAGE < total.peek():
            step(ENTRY_PAGE)

    table = indah.Table(
        rows=shown,
        key="id",
        selected=picked,
        height="28rem",
        density="compact",
        label="Entries",
        columns=[
            indah.Col("prompt", title="Prompt", sortable=False),
            indah.Col("hits", title="Hits", kind="mono", width="6ch", sortable=False),
            indah.Col("answers", title="Answers", width="14ch", sortable=False),
            indah.Col("flags", title="Flags", width="24ch", sortable=False),
            indah.Col("version", title="Version", kind="mono", width="8ch", sortable=False),
            indah.Col("updated", title="Changed", kind="mono", width="18ch", sortable=False),
        ],
    )
    return indah.Column(
        children=[
            indah.Text("# Entries", markdown=True),
            indah.Row(
                children=[
                    indah.TextInput(query, label="Search", placeholder="Find a prompt"),
                    indah.Select(chip, options=list(ENTRY_FILTERS), label="Filter"),
                    indah.Select(order, options=list(ENTRY_SORTS), label="Sort"),
                ],
                align="end",
            ),
            table,
            indah.Row(
                children=[
                    indah.Button("Previous", previous, variant="outlined"),
                    indah.Text(lambda: summary.value, size="body"),
                    indah.Button("Next", following, variant="outlined"),
                ],
                align="center",
            ),
        ]
    )
