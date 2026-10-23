"""One entry: its request, its answers, and how it has changed. Reached by a deep link."""

from __future__ import annotations

import asyncio
from functools import partial
from typing import TYPE_CHECKING, Any

import indah

from ...errors import SemulaError
from ..rows import answers_from_editor, editor_text, revision_row
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


SINGLE_HINT = (
    "Plain text is the reply. Start with `{` for a tool call or a stream, "
    'such as `{"chunks": ["Hel", "lo"]}`.'
)


def entry_page(tab: Tab) -> indah.Component:
    ctx = tab.ctx
    entry: indah.Signal[dict[str, Any] | None] = indah.Signal(None)
    history: indah.Signal[list[dict[str, Any]]] = indah.Signal([])
    note: indah.Signal[str] = indah.Signal("")
    loads = 0
    # The editor. ``clean`` is the text last taken from the entry; a draft that differs from it
    # is the viewer's own and is never overwritten by a reload. ``edit_version`` is the version
    # that draft started from, which is what a save sends, so a stale draft meets a conflict.
    draft: indah.Signal[str] = indah.Signal("")
    clean: indah.Signal[str] = indah.Signal("")
    edit_version: indah.Signal[int] = indah.Signal(0)
    selection: indah.Signal[str] = indah.Signal("first")
    extra: indah.Signal[str] = indah.Signal("")
    problem: indah.Signal[str] = indah.Signal("")
    saved: indah.Signal[str] = indah.Signal("")

    def adopt(found: dict[str, Any]) -> None:
        """Take the entry's answers into the editor, unless the viewer has been typing."""
        text = editor_text(found["responses"])
        if draft.peek() == clean.peek():
            draft.set(text)
            edit_version.set(found["version"])
            selection.set(found["selection"])
        clean.set(text)

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
        adopt(found)
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

    def explain(exc: SemulaError) -> str:
        if exc.code == "version_conflict":
            current = (exc.extra.get("current") or {}).get("version")
            return (
                f"Someone changed this entry; it is now at version {current}. Your text is kept "
                "above. Press Reload to see their version (that discards your text)."
            )
        where = f" (at {exc.param})" if exc.param else ""
        return f"{exc.code}: {exc.message}{where}"

    async def save() -> None:
        found = entry.peek()
        if not found:
            return
        try:
            answers = answers_from_editor(draft.peek(), multiple=len(found["responses"]) > 1)
        except ValueError as exc:
            problem.set(str(exc))
            return
        changes: dict[str, Any] = {"selection": selection.peek()}
        changes["responses" if len(found["responses"]) > 1 else "response"] = (
            answers if len(found["responses"]) > 1 else answers[0]
        )
        await write(found, changes, "Saved.")

    async def write(
        found: dict[str, Any], changes: dict[str, Any], done: str, *, keep_draft: bool = False
    ) -> None:
        """Send one edit as a person. ``keep_draft`` is for edits that are not about the typed
        text (a pin, another answer): the draft stays, and later saves build on this version."""
        try:
            updated = await blocking(
                partial(
                    tab.service.update_entry,
                    found["id"],
                    edit_version.peek(),
                    changes,
                    actor="human",
                )
            )
        except SemulaError as exc:
            problem.set(explain(exc))
            saved.set("")
            return
        problem.set("")
        saved.set(done)
        entry.set(updated)
        text = editor_text(updated["responses"])
        if keep_draft and draft.peek() != clean.peek():
            clean.set(clean.peek())  # the draft is still the viewer's own
        else:
            clean.set(text)
            draft.set(text)
        edit_version.set(updated["version"])
        selection.set(updated["selection"])

    async def reload_current() -> None:
        found = entry.peek()
        if not found:
            return
        current = await blocking(tab.service.get_entry, found["id"])
        text = editor_text(current["responses"])
        entry.set(current)
        clean.set(text)
        draft.set(text)
        edit_version.set(current["version"])
        selection.set(current["selection"])
        problem.set("")
        saved.set("")

    async def toggle_pin() -> None:
        found = entry.peek()
        if found:
            await write(found, {"pinned": not found["pinned"]}, "Pin changed.", keep_draft=True)

    async def add_answer() -> None:
        found = entry.peek()
        if not found:
            return
        try:
            (answer,) = answers_from_editor(extra.peek(), multiple=False)
        except ValueError as exc:
            problem.set(str(exc))
            return
        if selection.peek() == "first":  # a second answer that is never picked is pointless
            selection.set("sequence")
        await write(
            found,
            {"add_response": answer, "selection": selection.peek()},
            "Answer added.",
            keep_draft=True,
        )
        if not problem.peek():
            extra.set("")

    async def delete() -> None:
        found = entry.peek()
        if not found:
            return
        try:
            await blocking(
                partial(
                    tab.service.delete_entry,
                    found["id"],
                    found["version"],
                    actor="human",
                )
            )
        except SemulaError as exc:
            problem.set(explain(exc))
            return
        ctx.navigate(f"/projects/{tab.project.peek()}/entries")

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
            *tab.only(
                "edit",
                lambda: indah.Column(
                    children=[
                        indah.Card(
                            title="Edit the answer",
                            children=[
                                indah.CodeEditor(
                                    draft,
                                    language="text",
                                    lint=False,
                                    height="12rem",
                                    label="Edit answer",
                                ),
                                indah.Text(
                                    lambda: (
                                        "This entry has several answers: edit them as a JSON list."
                                        if entry.value and len(entry.value["responses"]) > 1
                                        else SINGLE_HINT
                                    ),
                                    markdown=True,
                                    size="body",
                                ),
                                indah.Select(
                                    selection, options=["first", "sequence", "cycle"], label="Pick"
                                ),
                                indah.Text(
                                    lambda: problem.value, size="body"
                                ),  # plain: codes have underscores
                                indah.Text(lambda: saved.value, markdown=True, size="body"),
                                indah.Row(
                                    children=[
                                        indah.Button("Save answer", save),
                                        indah.Button("Reload", reload_current, variant="outlined"),
                                        indah.Button(
                                            lambda: (
                                                "Unpin"
                                                if entry.value and entry.value["pinned"]
                                                else "Pin"
                                            ),
                                            toggle_pin,
                                            variant="outlined",
                                        ),
                                        indah.Button(
                                            "Delete entry",
                                            delete,
                                            variant="outlined",
                                            confirm="Delete this entry? Its history goes with it.",
                                        ),
                                    ],
                                    align="center",
                                ),
                            ],
                        ),
                        indah.Card(
                            title="Add another answer",
                            children=[
                                indah.CodeEditor(
                                    extra,
                                    language="text",
                                    lint=False,
                                    height="6rem",
                                    label="Another answer",
                                ),
                                indah.Button("Add answer", add_answer, variant="outlined"),
                            ],
                        ),
                    ]
                ),
            ),
            indah.Card(title="History", children=[revisions]),
        ]
    )
