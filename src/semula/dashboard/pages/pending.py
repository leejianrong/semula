"""The pending queue: calls parked in record mode, and a form to answer the selected one."""

from __future__ import annotations

import json
from functools import partial
from typing import TYPE_CHECKING, Any

import indah

from ...errors import SemulaError
from ..rows import answer_from_text, pending_row
from ..state import blocking
from ..state import spawn as spawn_task
from .log import MESSAGE

if TYPE_CHECKING:
    from ..shell import Tab

HINT = (
    "Plain text is sent as the reply. To send a tool call or a stream, write JSON that starts "
    'with `{`, for example `{"chunks": ["Hel", "lo"]}`. '
    "With the keyboard: Ctrl-S sends, and Ctrl-M then Tab leaves the box (Tab indents in it)."
)


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return json.dumps(content, indent=2, ensure_ascii=False) if content is not None else ""


def pending_page(tab: Tab) -> indah.Component:
    ctx = tab.ctx
    shown: indah.Stream = indah.Stream(limit=500)
    picked: indah.Signal[Any] = indah.Signal(None)
    detail: indah.Signal[dict[str, Any] | None] = indah.Signal(None)
    answer: indah.Signal[str] = indah.Signal("")
    error: indah.Signal[str] = indah.Signal("")
    done: indah.Signal[str] = indah.Signal("")
    loads = 0  # the newest reload wins; an older one that finishes late is dropped

    spawn = partial(spawn_task, ctx)

    async def reload(project: str) -> None:
        nonlocal loads
        loads += 1
        mine = loads
        items = await blocking(tab.service.list_pending, "pending", project)
        if mine != loads or project != tab.project.peek():
            return
        rows = [pending_row(i) for i in items]
        shown.set(rows)
        if picked.peek() is not None and all(r["id"] != picked.peek() for r in rows):
            picked.set(None)  # answered (here or elsewhere) while it was open

    def on_change() -> None:
        tab.queues_moved.value  # noqa: B018 (reading it is the point: look again when it moves)
        spawn(reload(tab.project.value))

    indah.effect(on_change)

    async def open_one(key: str) -> None:
        try:
            loaded = await blocking(tab.service.get_pending, key)
        except SemulaError:
            loaded = None
        if picked.peek() == key:
            detail.set(loaded)

    def on_pick() -> None:
        key = picked.value
        error.set("")
        answer.set("")
        if key is None:
            detail.set(None)
        else:
            done.set("")
            spawn(open_one(key))

    indah.effect(on_pick)

    async def send() -> None:
        key = picked.peek()
        if key is None:
            return
        try:
            payload = answer_from_text(answer.peek())
        except ValueError as exc:
            error.set(str(exc))
            return
        try:
            await blocking(_answer, tab, key, payload)
        except SemulaError as exc:
            where = f" (at {exc.param})" if exc.param else ""
            error.set(f"{exc.code}: {exc.message}{where}")
            return
        error.set("")
        done.set(f"Answered {key}. The held call is released.")
        picked.set(None)
        await reload(tab.project.peek())

    def messages() -> list[dict[str, str]]:
        d = detail.value
        if not d:
            return []
        out = [
            {"id": f"m{i}", "role": m.get("role", ""), "content": _text(m.get("content"))}
            for i, m in enumerate(d["request"]["messages"])
        ]
        for i, t in enumerate(d["request"].get("tools") or []):
            fn = t.get("function", t)
            out.append({"id": f"t{i}", "role": "tool you may call", "content": _text(fn)})
        added = (d.get("conversation") or {}).get("added") or []
        if added:
            out.append(
                {
                    "id": "added",
                    "role": "added since the previous call",
                    "content": "\n\n".join(
                        f"{m.get('role', '')}: {_text(m.get('content'))}" for m in added
                    ),
                }
            )
        shape = d.get("shape") or {}
        for i, t in enumerate(shape.get("tools") or []):
            out.append(
                {
                    "id": f"s{i}",
                    "role": f"answer skeleton ({t['name']})",
                    "content": _text(t["skeleton"]),
                }
            )
        if shape.get("max_tokens"):
            out.append(
                {
                    "id": "limit",
                    "role": "free-text limit",
                    "content": f"max_tokens {shape['max_tokens']}",
                }
            )
        return out

    heading = indah.computed(
        lambda: (
            f"**{detail.value['project']}**  ·  {detail.value['id']}"
            if detail.value
            else "Select a parked call to read it."
        )
    )

    table = indah.Table(
        rows=shown,
        key="id",
        selected=picked,
        keyboard=True,  # arrows move, Enter selects: a row is not a link, so Tab skips it
        height="16rem",
        density="compact",
        label="Parked calls",
        columns=[
            indah.Col("since", title="Parked at", kind="mono", width="15ch", sortable=False),
            indah.Col("thread", title="Conversation", kind="mono", width="16ch", sortable=False),
            indah.Col("prompt", title="Prompt", sortable=False),
        ],
    )
    pane = indah.Column(
        children=[
            indah.Text(heading, markdown=True),
            indah.List(indah.computed(messages), key="id", item=MESSAGE, empty=""),
            *tab.only(
                "edit",
                lambda: indah.Column(
                    children=[
                        indah.CodeEditor(
                            answer,
                            language="text",
                            lint=False,
                            height="10rem",
                            label="Answer",
                            placeholder="What the real model would say",
                            on_save=send,
                        ),
                        indah.Text(
                            lambda: error.value, size="body"
                        ),  # plain: codes have underscores
                        indah.Text(HINT, markdown=True),
                        indah.Button("Send answer", on_click=send),
                    ]
                ),
            ),
        ]
    )
    return indah.Column(
        children=[
            indah.Text("# Pending", markdown=True),
            indah.Text(
                "Calls held in record mode. Select one to read it and answer it.", markdown=True
            ),
            indah.Text(lambda: done.value, markdown=True),
            table,
            indah.Card(children=[pane]),
        ]
    )


def _answer(tab: Tab, key: str, payload: Any) -> str:
    return tab.service.answer_pending(key, payload, actor="human")
