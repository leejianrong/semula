"""One entry: its request, its answers, and how it has changed. Reached by a deep link."""

from __future__ import annotations

import asyncio
from functools import partial
from typing import TYPE_CHECKING, Any

import indah

from ...errors import SemulaError
from ..rows import revision_row
from ..state import blocking, cancel
from .log import MESSAGE
from .pending import _text

if TYPE_CHECKING:
    from ..shell import Tab


def _answer_text(response: dict[str, Any]) -> str:
    if response.get("chunks"):
        return "".join(response["chunks"])
    message = response.get("message") or {}
    return _text(message.get("content") or message.get("tool_calls"))


def entry_page(tab: Tab) -> indah.Component:
    ctx = tab.ctx
    entry: indah.Signal[dict[str, Any] | None] = indah.Signal(None)
    history: indah.Signal[list[dict[str, Any]]] = indah.Signal([])
    note: indah.Signal[str] = indah.Signal("")
    loads = 0

    def spawn(coro: Any) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        ctx.on_close(partial(cancel, task))

    async def load(entry_id: str) -> None:
        nonlocal loads
        loads += 1
        mine = loads
        try:
            found = await blocking(tab.service.get_entry, entry_id)
            past = await blocking(tab.service.entry_history, entry_id, None)
        except SemulaError:
            if mine == loads:
                entry.set(None)
                note.set("There is no such entry. It may have been deleted.")
            return
        if mine != loads:
            return
        note.set("")
        entry.set(found)
        history.set([revision_row(r) for r in past["revisions"]])

    def on_change() -> None:
        tab.entries_moved.value  # noqa: B018
        entry_id = ctx.params.value.get("entry")
        if entry_id:
            spawn(load(entry_id))

    indah.effect(on_change)

    def heading() -> str:
        e = entry.value
        if not e:
            return note.value or "Loading the entry."
        pin = "  ·  **pinned**" if e["pinned"] else ""
        return f"`{e['id']}`  ·  version {e['version']}  ·  {e['hit_count']} hits{pin}"

    def request_rows() -> list[dict[str, str]]:
        e = entry.value
        if not e:
            return []
        out = [
            {"id": f"m{i}", "role": m.get("role", ""), "content": _text(m.get("content"))}
            for i, m in enumerate(e["request"]["messages"])
        ]
        for i, t in enumerate(e["request"].get("tools") or []):
            out.append(
                {"id": f"t{i}", "role": "tool offered", "content": _text(t.get("function", t))}
            )
        return out

    def answer_rows() -> list[dict[str, str]]:
        e = entry.value
        if not e:
            return []
        label = f"answer, {e['selection']}" if len(e["responses"]) > 1 else "answer"
        return [
            {"id": f"a{i}", "role": f"{label} {i + 1}" if len(e["responses"]) > 1 else label,
             "content": _answer_text(r)}
            for i, r in enumerate(e["responses"])
        ]  # fmt: skip

    def back() -> None:
        ctx.navigate(f"/projects/{tab.project.peek()}/entries")

    revisions = indah.Table(
        rows=history,
        key="id",
        height="12rem",
        density="compact",
        label="History",
        columns=[
            indah.Col("version", title="Version", kind="mono", width="8ch", sortable=False),
            indah.Col("when", title="When", kind="mono", width="15ch", sortable=False),
            indah.Col("actor", title="By", width="8ch", sortable=False),
            indah.Col("summary", title="What changed", sortable=False),
        ],
    )
    return indah.Column(
        children=[
            indah.Row(
                children=[
                    indah.Button("Back to entries", back, variant="outlined"),
                    indah.Text(heading, markdown=True),
                ],
                align="center",
            ),
            indah.Card(
                title="Request",
                children=[
                    indah.List(indah.computed(request_rows), key="id", item=MESSAGE, empty="")
                ],
            ),
            indah.Card(
                title="Answers",
                children=[
                    indah.List(indah.computed(answer_rows), key="id", item=MESSAGE, empty="")
                ],
            ),
            indah.Card(title="History", children=[revisions]),
        ]
    )
