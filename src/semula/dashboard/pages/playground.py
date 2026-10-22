"""The playground: ask what would happen to a prompt, tier by tier, without sending it."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, Any

import indah

from ... import canonical
from ...errors import SemulaError
from ..rows import near_text, new_entry_request, rail_from_tests, verdict_text
from ..state import blocking
from .log import RAIL_ROW

if TYPE_CHECKING:
    from ..shell import Tab


def playground_page(tab: Tab) -> indah.Component:
    ctx = tab.ctx
    prompt: indah.Signal[str] = indah.Signal("")
    system: indah.Signal[str] = indah.Signal("")
    tools: indah.Signal[str] = indah.Signal("")
    result: indah.Signal[dict[str, Any] | None] = indah.Signal(None)
    problem: indah.Signal[str] = indah.Signal("")

    async def ask() -> None:
        try:
            request = new_entry_request(prompt.peek(), system.peek(), tools.peek())
        except ValueError as exc:
            problem.set(str(exc))
            return
        try:
            found = await blocking(
                partial(
                    tab.service.test_match,
                    canonical.from_openai(request),
                    tab.project.peek(),
                )
            )
        except SemulaError as exc:
            problem.set(f"{exc.code}: {exc.message}")
            return
        problem.set("")
        result.set(found)

    def open_entry() -> None:
        found = result.peek()
        if found and found.get("entry_id"):
            ctx.navigate(f"/projects/{tab.project.peek()}/entries/{found['entry_id']}")

    headline = indah.computed(
        lambda: verdict_text(result.value) if result.value else "Nothing asked yet."
    )
    rail = indah.computed(lambda: rail_from_tests(result.value["tiers"]) if result.value else [])
    near = indah.computed(lambda: near_text(result.value) if result.value else "")
    preview = indah.computed(
        lambda: (result.value.get("response_preview") or "") if result.value else ""
    )
    return indah.Column(
        children=[
            indah.Text("# Playground", markdown=True),
            indah.Text(
                "Ask what semula would do with a prompt in this project. Nothing is served, "
                "logged or counted.",
                size="body",
            ),
            indah.CodeEditor(
                prompt, language="text", lint=False, height="6rem", label="Test prompt",
                placeholder="The user message your app would send",
            ),
            indah.TextInput(system, label="Test system prompt (optional)"),
            indah.CodeEditor(
                tools, language="json", lint=False, height="5rem", label="Test tools (optional)"
            ),
            indah.Text(lambda: problem.value, size="body"),
            indah.Button("Test it", ask),
            indah.Card(
                children=[
                    indah.Text(headline, size="body"),
                    indah.List(rail, key="id", item=RAIL_ROW),
                    indah.Text(near, size="body"),
                    indah.Text(preview, size="body"),
                    indah.Button("Open the entry", open_entry, variant="outlined"),
                ]
            ),
        ]
    )  # fmt: skip
