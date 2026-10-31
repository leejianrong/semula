"""The review queue: near misses that wait for a person to say they mean the same thing."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, Any

import indah

from ...errors import SemulaError
from ..rows import candidate_row, request_text
from ..state import blocking
from ..state import spawn as spawn_task
from .entry import _answer_text

if TYPE_CHECKING:
    from ..shell import Tab

CHECK = (
    "Approving serves this prompt from the answer below, including any edit made to it later. "
    "Check that it fits."
)


def review_page(tab: Tab) -> indah.Component:
    ctx = tab.ctx
    shown: indah.Stream = indah.Stream(limit=500)
    picked: indah.Signal[Any] = indah.Signal(None)
    detail: indah.Signal[dict[str, Any] | None] = indah.Signal(None)
    incoming: indah.Signal[str] = indah.Signal("")
    recorded: indah.Signal[str] = indah.Signal("")
    answer: indah.Signal[str] = indah.Signal("")
    done: indah.Signal[str] = indah.Signal("")
    problem: indah.Signal[str] = indah.Signal("")
    loads = 0

    spawn = partial(spawn_task, ctx)

    async def reload(project: str) -> None:
        nonlocal loads
        loads += 1
        mine = loads
        items = await blocking(tab.service.list_candidates, project, "open")
        if mine != loads or project != tab.project.peek():
            return
        rows = [candidate_row(c) for c in items]
        shown.set(rows)
        if picked.peek() is not None and all(r["id"] != picked.peek() for r in rows):
            picked.set(None)  # decided here or elsewhere while it was open

    def on_change() -> None:
        tab.queues_moved.value  # noqa: B018 (reading it is the point: look again when it moves)
        spawn(reload(tab.project.value))

    indah.effect(on_change)

    async def open_one(key: str) -> None:
        try:
            found = await blocking(tab.service.get_candidate, key)
        except SemulaError:
            found = None
        if picked.peek() != key:
            return
        detail.set(found)
        if found:
            incoming.set(request_text(found["request"]))
            recorded.set(request_text(found["entry"]["request"]))
            answer.set("\n\n".join(_answer_text(r) for r in found["entry"]["responses"]))

    def on_pick() -> None:
        key = picked.value
        problem.set("")
        if key is None:
            detail.set(None)
            incoming.set("")
            recorded.set("")
            answer.set("")
        else:
            done.set("")
            spawn(open_one(key))

    indah.effect(on_pick)

    async def decide(approve: bool) -> None:
        found = detail.peek()
        if not found:
            return
        act = tab.service.approve_candidate if approve else tab.service.reject_candidate
        try:
            await blocking(act, found["id"])
        except SemulaError as exc:
            problem.set(f"{exc.code}: {exc.message}")
            return
        problem.set("")
        done.set(
            "Approved: that prompt is now served from the entry."
            if approve
            else "Rejected: that pairing will not come back."
        )
        picked.set(None)
        await reload(tab.project.peek())

    async def approve() -> None:
        await decide(True)

    async def reject() -> None:
        await decide(False)

    heading = indah.computed(
        lambda: (
            f"**{detail.value['project']}**  ·  score {detail.value['score']:.2f}  ·  "
            f"found by the {detail.value['tier']} tier"
            if detail.value
            else "Select a near miss to compare it."
        )
    )
    table = indah.Table(
        rows=shown,
        key="id",
        selected=picked,
        height="14rem",
        density="compact",
        label="Review queue",
        columns=[
            indah.Col("score", title="Score", kind="mono", width="7ch", sortable=False),
            indah.Col("tier", title="Found by", width="12ch", sortable=False),
            indah.Col("prompt", title="New prompt", sortable=False),
            indah.Col("entry", title="Matches the entry", sortable=False),
        ],
    )
    return indah.Column(
        children=[
            indah.Text("# Review", markdown=True),
            indah.Text(
                "Prompts that nearly matched a recording but not closely enough to serve. "
                "Say whether they mean the same thing.",
                markdown=True,
                size="body",
            ),
            indah.Text(lambda: done.value, size="body"),
            table,
            indah.Card(
                children=[
                    indah.Text(heading, markdown=True, size="body"),
                    indah.Diff(
                        incoming, recorded, normalise="text", labels=("new prompt", "recorded")
                    ),
                    indah.Text(CHECK, size="body"),
                    indah.Card(
                        title="The recorded answer",
                        children=[indah.Text(lambda: answer.value, size="body")],
                    ),
                    indah.Text(lambda: problem.value, size="body"),
                    *tab.only(
                        "edit",
                        lambda: indah.Row(
                            children=[
                                indah.Button("Approve", approve),
                                indah.Button("Reject", reject, variant="outlined"),
                            ],
                            align="center",
                        ),
                    ),
                ]
            ),
        ]
    )
