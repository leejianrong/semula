"""The live log: every call as it happens, and what became of the selected one."""

from __future__ import annotations

import json
from functools import partial
from typing import TYPE_CHECKING, Any

import indah

from ...errors import SemulaError
from ..rows import FILTERS, OUTCOME_TONES, keeps, rail_for
from ..state import blocking, cancel

if TYPE_CHECKING:
    from ..shell import Tab

RAIL_ROW: dict[str, Any] = {
    "tag": "div",
    "attrs": {"style": "display:flex;align-items:center;gap:.6rem;padding:.12rem 0"},
    "children": [
        {"tag": "span", "bind": {"style": "dot"}},
        {"tag": "span", "attrs": {"style": "min-width:6rem;font-weight:500"}, "text": "name"},
        {"tag": "span", "attrs": {"style": "color:var(--muted)"}, "text": "text"},
    ],
}
MESSAGE: dict[str, Any] = {
    "tag": "div",
    "attrs": {"style": "margin:.45rem 0"},
    "children": [
        {"tag": "small", "attrs": {"style": "color:var(--muted)"}, "text": "role"},
        {
            "tag": "pre",
            "attrs": {
                "style": "white-space:pre-wrap;word-break:break-word;margin:.15rem 0 0;"
                "padding:.5rem .7rem;border-radius:8px;background:var(--surface-2);"
                "font:12px/16px var(--font-mono)"
            },
            "text": "content",
        },
    ],
}


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return json.dumps(content, indent=2, ensure_ascii=False) if content is not None else ""


def _answer(entry: dict[str, Any]) -> str:
    first = (entry.get("responses") or [{}])[0].get("message", {})
    return _text(first.get("content") or first.get("tool_calls"))


async def _load(tab: Tab, row: dict[str, Any]) -> dict[str, Any]:
    """What the detail pane shows for one call: its request and answer where the service
    still has them (the log itself keeps only a preview)."""
    service = tab.service
    detail: dict[str, Any] = {"row": row, "messages": [], "answer": "", "note": ""}
    try:
        if row["entry_id"]:
            entry = await blocking(service.get_entry, row["entry_id"])
            detail["messages"] = entry["request"]["messages"]
            detail["answer"] = _answer(entry)
        elif row["pending_id"]:
            pending = await blocking(service.get_pending, row["pending_id"])
            detail["messages"] = pending["request"]["messages"]
            if pending["entry_id"]:
                detail["answer"] = _answer(await blocking(service.get_entry, pending["entry_id"]))
    except SemulaError:
        detail["note"] = "Its entry was deleted."
    if not detail["messages"] and row["preview"]:
        detail["messages"] = [{"role": "user", "content": row["preview"]}]
        detail["note"] = detail["note"] or "Only the preview was kept."
    return detail


def log_page(tab: Tab) -> indah.Component:
    ctx, dash = tab.ctx, tab.dash
    ring = ctx.tail(dash.calls, limit=1000)
    shown: indah.Stream = indah.Stream(limit=1000)
    chip: indah.Signal[str] = indah.Signal("All")
    live: indah.Signal[bool] = indah.Signal(True)
    picked: indah.Signal[Any] = indah.Signal(None)
    detail: indah.Signal[dict[str, Any] | None] = indah.Signal(None)

    def refilter() -> None:
        project, which = tab.project.value, chip.value
        if live.value:
            shown.set([r for r in ring.peek() if keeps(r, project, which)])

    def on_ring(kind: str, rows: list[Any]) -> None:
        if kind == "replace":
            refilter()
        elif live.peek():
            shown.append(*[r for r in rows if keeps(r, tab.project.peek(), chip.peek())])

    ring.listen(on_ring)
    indah.effect(refilter)

    def find(key: Any) -> dict[str, Any] | None:
        return next((r for r in ring.peek() if r["id"] == key), None)

    async def open_call(key: Any) -> None:
        row = find(key)
        if row is None:
            return
        loaded = await _load(tab, row)
        if picked.peek() == key:  # the viewer may have moved on while this loaded
            detail.set(loaded)

    def on_pick() -> None:
        key = picked.value
        if key is None:
            detail.set(None)
        else:
            _spawn(open_call(key))

    def _spawn(coro: Any) -> None:
        import asyncio

        task = asyncio.get_running_loop().create_task(coro)
        ctx.on_close(partial(cancel, task))

    indah.effect(on_pick)

    def heading() -> str:
        d = detail.value
        if not d:
            return ""
        row = d["row"]
        return f"**{row['result']}**  {row['when']}  ·  {row['project']}"

    rail = indah.computed(
        lambda: rail_for(detail.value["row"], meaning_on=dash.meaning_on) if detail.value else []
    )

    def shown_messages() -> list[dict[str, str]]:
        if not detail.value:
            return []
        out = [
            {"id": f"m{i}", "role": m.get("role", ""), "content": _text(m.get("content"))}
            for i, m in enumerate(detail.value["messages"])
        ]
        if detail.value["answer"]:
            out.append({"id": "answer", "role": "answer", "content": detail.value["answer"]})
        return out

    messages = indah.computed(shown_messages)
    note = indah.computed(lambda: detail.value["note"] if detail.value else "")

    stat_rate = indah.computed(
        lambda: (
            f"{tab.stats.value['hit_rate'] * 100:.0f}%"
            if tab.stats.value["hit_rate"] is not None
            else "-"
        )
    )
    stat_median = indah.computed(
        lambda: (
            f"{tab.stats.value['median_latency_ms']:.0f} ms"
            if tab.stats.value["median_latency_ms"] is not None
            else "-"
        )
    )
    stat_parked = indah.computed(lambda: str(tab.stats.value["parked"]))

    table = indah.Table(
        rows=shown,
        key="id",
        selected=picked,
        height="26rem",
        label="Calls",
        follow=True,
        density="compact",
        columns=[
            indah.Col("when", title="When", kind="mono", width="15ch", sortable=False),
            indah.Col("prompt", title="Prompt", sortable=False),
            indah.Col("result", title="Result", kind="badge", tones=OUTCOME_TONES, sortable=False),
            indah.Col("match", title="Match", sortable=False),
            indah.Col("took", title="Took", kind="mono", sortable=False),
        ],
    )
    pane = indah.Column(
        children=[
            indah.Text(heading, markdown=True),
            indah.List(rail, key="id", item=RAIL_ROW),
            indah.Text(lambda: note.value, markdown=True),
            indah.List(messages, key="id", item=MESSAGE, empty=""),
        ]
    )
    return indah.Column(
        children=[
            indah.Row(
                children=[
                    indah.Text("# Live log", markdown=True),
                    indah.Switch(live, label="Live"),
                    indah.Select(chip, options=list(FILTERS), label="Show"),
                ],
                align="center",
            ),
            indah.Row(
                children=[
                    indah.Stat(stat_rate, label="Hit rate"),
                    indah.Stat(stat_median, label="Median"),
                    indah.Stat(stat_parked, label="Parked"),
                ]
            ),
            table,
            indah.Card(children=[pane]),
        ]
    )
