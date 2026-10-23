"""The admin screens of a hosted workspace: members, tokens, the audit log, usage and deletion.

Only an admin is given these (the routes and the nav links are not built for anyone else), and
the service refuses a non-admin whatever the browser sends. A secret (an invitation link, a new
token) goes to the open tab through ``SecretReveal`` and is never held anywhere else."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from functools import partial
from typing import TYPE_CHECKING, Any

import indah

from ...errors import SemulaError
from ..rows import (
    ALL_ACTIONS,
    ALL_PROJECTS,
    AUDIT_ACTIONS,
    AUDIT_PAGE,
    ROLES,
    TOKEN_SCOPES,
    audit_row,
    invitation_row,
    invite_link,
    member_row,
    stamp,
    token_row,
    usage_lines,
)
from ..state import blocking, cancel

if TYPE_CHECKING:
    from ..shell import Tab


def describe(exc: SemulaError) -> str:
    """An error as plain text beside the control (a plain ``Text``: codes have underscores,
    which markdown would turn into emphasis)."""
    where = f" (at {exc.param})" if exc.param else ""
    return f"{exc.code}: {exc.message}{where}"


class Screen:
    """What every admin screen needs: a task helper, an error line, and a reload that runs
    whenever the person opens the screen, so it is never older than the visit."""

    def __init__(self, tab: Tab, prefix: str) -> None:
        self.tab = tab
        self.prefix = prefix
        self.error: indah.Signal[str] = indah.Signal("")
        self.note: indah.Signal[str] = indah.Signal("")

    def spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self.tab.ctx.on_close(partial(cancel, task))

    def on_open(self, reload: Callable[[], Coroutine[Any, Any, None]]) -> None:
        def look() -> None:
            if self.tab.ctx.path.value.startswith(self.prefix):
                self.spawn(reload())

        indah.effect(look)

    async def attempt(self, work: Callable[..., Any], *args: Any) -> Any:
        """Run a service call off the loop; on a refusal show it and return ``None`` (the
        calls here return something, so ``None`` means it failed)."""
        try:
            result = await blocking(work, *args)
        except SemulaError as exc:
            self.error.set(describe(exc))
            self.note.set("")
            return None
        self.error.set("")
        return result if result is not None else True


def _lines(screen: Screen) -> list[indah.Component]:
    return [
        indah.Text(lambda: screen.note.value, size="body"),
        indah.Text(lambda: screen.error.value, size="body"),
    ]


def members_page(tab: Tab) -> indah.Component:
    screen = Screen(tab, "/workspace/members")
    members: indah.Stream = indah.Stream(limit=500)
    invitations: indah.Stream = indah.Stream(limit=500)
    email: indah.Signal[str] = indah.Signal("")
    role: indah.Signal[str] = indah.Signal("viewer")
    link: indah.Signal[str | None] = indah.Signal(None)

    async def reload() -> None:
        try:
            found = await blocking(tab.service.list_members)
            waiting = await blocking(tab.service.list_invitations)
        except SemulaError as exc:
            screen.error.set(describe(exc))
            return
        members.set([member_row(m) for m in found])
        invitations.set([invitation_row(i) for i in waiting if i["status"] == "open"])

    screen.on_open(reload)

    async def set_role(person: str, new: str) -> None:
        done = await screen.attempt(tab.service.set_member_role, person, new)
        if done:
            screen.note.set(f"Changed the role to {new}.")
        await reload()

    async def remove(person: str) -> None:
        if await screen.attempt(tab.service.remove_member, person):
            screen.note.set("Removed. Their sign-ins here have ended.")
        await reload()

    async def revoke(invitation: str) -> None:
        if await screen.attempt(tab.service.revoke_invitation, invitation):
            screen.note.set("Revoked the invitation.")
        await reload()

    async def invite() -> None:
        made = await screen.attempt(tab.service.create_invitation, email.peek(), role.peek())
        if made and made is not True:
            link.set(invite_link(tab.public_url, made["token"]))
            screen.note.set(
                f"Invited {made['email']} as {made['role']}. Send them the link below: it works "
                "once, for seven days, for someone signed in with that address."
            )
            email.set("")
        await reload()

    def action(label: str, new: str) -> indah.RowAction:
        return indah.RowAction(label, partial(set_role_task, new), variant="ghost")

    def set_role_task(new: str, person: str) -> Any:
        return set_role(person, new)

    members_table = indah.Table(
        rows=members,
        key="id",
        label="Members",
        density="compact",
        columns=[
            indah.Col("who", title="Person", sortable=False),
            indah.Col("email", title="Email", sortable=False),
            indah.Col("role", title="Role", sortable=False),
            indah.Col("since", title="Since", kind="mono", sortable=False),
        ],
        actions=[
            *(action(f"Make {r}", r) for r in ROLES),
            indah.RowAction(
                "Remove",
                remove,
                confirm="Remove this person from the workspace? Their sign-ins here end now.",
            ),
        ],
    )
    invitations_table = indah.Table(
        rows=invitations,
        key="id",
        label="Open invitations",
        density="compact",
        columns=[
            indah.Col("email", title="Email", sortable=False),
            indah.Col("role", title="Role", sortable=False),
            indah.Col("expires", title="Expires", kind="mono", sortable=False),
        ],
        actions=[indah.RowAction("Revoke", revoke, confirm="Revoke this invitation?")],
    )
    return indah.Column(
        children=[
            indah.Text("# Members", markdown=True),
            *_lines(screen),
            members_table,
            indah.Text("## Invite someone", markdown=True),
            indah.Row(
                children=[
                    indah.TextInput(email, label="Email", placeholder="name@example.com"),
                    indah.Select(role, options=list(ROLES), label="Role"),
                    indah.Button("Send invitation", on_click=invite),
                ],
                align="end",
            ),
            indah.SecretReveal(link, label="Invitation link"),
            indah.Text("## Open invitations", markdown=True),
            invitations_table,
        ]
    )


def tokens_page(tab: Tab) -> indah.Component:
    screen = Screen(tab, "/workspace/tokens")
    tokens: indah.Stream = indah.Stream(limit=500)
    name: indah.Signal[str] = indah.Signal("")
    scope: indah.Signal[str] = indah.Signal("record")
    project: indah.Signal[str] = indah.Signal(ALL_PROJECTS)
    secret: indah.Signal[str | None] = indah.Signal(None)

    async def reload() -> None:
        try:
            found = await blocking(tab.service.list_tokens)
        except SemulaError as exc:
            screen.error.set(describe(exc))
            return
        tokens.set([token_row(t) for t in found])

    screen.on_open(reload)

    async def create() -> None:
        bound = None if project.peek() == ALL_PROJECTS else project.peek()
        made = await screen.attempt(
            partial(
                tab.service.create_token,
                name.peek(),
                scope.peek(),
                project=bound,
                created_by=tab.who.id if tab.who else None,
            )
        )
        if made and made is not True:
            secret.set(made["token"])
            screen.note.set(f"Created {made['name']}. Copy the secret now: it is shown once.")
            name.set("")
        await reload()

    async def rotate(token: str) -> None:
        made = await screen.attempt(tab.service.rotate_token, token)
        if made and made is not True:
            secret.set(made["token"])
            screen.note.set(
                f"Rotated {made['name']}. The old token keeps working for 24 hours so you can "
                "swap it; copy the new secret now."
            )
        await reload()

    async def revoke(token: str) -> None:
        if await screen.attempt(tab.service.revoke_token, token):
            screen.note.set("Revoked. It stops working at once.")
        await reload()

    table = indah.Table(
        rows=tokens,
        key="id",
        label="Tokens",
        density="compact",
        columns=[
            indah.Col("name", title="Name", sortable=False),
            indah.Col("scope", title="Scope", sortable=False),
            indah.Col("project", title="Project", sortable=False),
            indah.Col("prefix", title="Starts with", kind="mono", sortable=False),
            indah.Col("used", title="Last used", kind="mono", sortable=False),
            indah.Col("state", title="State", sortable=False),
        ],
        actions=[
            indah.RowAction(
                "Rotate",
                rotate,
                confirm="Make a new token like this one? The old one stops in 24 h.",
            ),
            indah.RowAction("Revoke", revoke, confirm="Revoke this token? It stops working now."),
        ],
    )
    return indah.Column(
        children=[
            indah.Text("# Tokens", markdown=True),
            indah.Text(
                "What an app, a script or a coding agent uses to reach this workspace. A replay "
                "token only reads recordings, a record token can also answer calls, and an admin "
                "token can change settings. Bind one to a project to keep it out of the rest.",
                size="body",
            ),
            *_lines(screen),
            indah.SecretReveal(secret, label="New token"),
            table,
            indah.Text("## New token", markdown=True),
            indah.Row(
                children=[
                    indah.TextInput(name, label="Name", placeholder="ci runner"),
                    indah.Select(scope, options=list(TOKEN_SCOPES), label="Scope"),
                    indah.Select(
                        project, options=[ALL_PROJECTS, *tab.dash.project_ids()], label="Project"
                    ),
                    indah.Button("Create token", on_click=create),
                ],
                align="end",
            ),
        ]
    )


def audit_page(tab: Tab) -> indah.Component:
    screen = Screen(tab, "/workspace/audit")
    rows: indah.Stream = indah.Stream(limit=AUDIT_PAGE)
    action: indah.Signal[str] = indah.Signal(ALL_ACTIONS)
    trail: indah.Signal[list[int | None]] = indah.Signal([None])  # the before_id of each page seen
    more: indah.Signal[bool] = indah.Signal(False)
    page: indah.Signal[str] = indah.Signal("")
    oldest_shown = 0  # the id of the last (oldest) row on screen: the next page starts below it

    async def reload() -> None:
        nonlocal oldest_shown
        before = trail.peek()[-1]
        which = None if action.peek() == ALL_ACTIONS else action.peek()
        try:
            found = await blocking(
                partial(tab.service.list_audit, AUDIT_PAGE + 1, before_id=before, action=which)
            )
        except SemulaError as exc:
            screen.error.set(describe(exc))
            return
        screen.error.set("")
        shown = found[:AUDIT_PAGE]
        more.set(len(found) > AUDIT_PAGE)
        oldest_shown = int(shown[-1]["id"]) if shown else 0
        rows.set([audit_row(e) for e in shown])
        page.set(
            f"Page {len(trail.peek())}"
            + (f", from {stamp(shown[0]['ts'])} back to {stamp(shown[-1]['ts'])}" if shown else "")
            if shown or len(trail.peek()) > 1
            else "Nothing recorded yet."
        )

    screen.on_open(reload)

    def filter_changed() -> None:
        action.value  # noqa: B018 (reading it is the point: start again from the newest)
        trail.set([None])
        screen.spawn(reload())

    indah.effect(filter_changed)

    async def older() -> None:
        if not more.peek():
            return
        trail.set([*trail.peek(), oldest_shown])
        await reload()

    async def newer() -> None:
        if len(trail.peek()) > 1:
            trail.set(trail.peek()[:-1])
            await reload()

    table = indah.Table(
        rows=rows,
        key="id",
        label="Audit log",
        density="compact",
        columns=[
            indah.Col("when", title="When", kind="mono", sortable=False, width="17ch"),
            indah.Col("who", title="Who", sortable=False),
            indah.Col("action", title="Action", sortable=False),
            indah.Col("target", title="Target", kind="mono", sortable=False),
            indah.Col("detail", title="Detail", kind="mono", sortable=False),
        ],
    )
    return indah.Column(
        children=[
            indah.Text("# Audit", markdown=True),
            indah.Text(
                "Who changed what here. It keeps ids and counts, never prompts or answers, and "
                "nobody can edit it.",
                size="body",
            ),
            indah.Text(lambda: screen.error.value, size="body"),
            indah.Select(action, options=[ALL_ACTIONS, *AUDIT_ACTIONS], label="Action"),
            table,
            indah.Row(
                children=[
                    indah.Button("Newer", on_click=newer, variant="ghost"),
                    indah.Text(lambda: page.value, size="body"),
                    indah.Button("Older", on_click=older, variant="ghost"),
                ],
                align="center",
            ),
        ]
    )


def usage_page(tab: Tab) -> indah.Component:
    screen = Screen(tab, "/workspace/usage")
    lines: indah.Stream = indah.Stream(limit=20)
    status: indah.Signal[str] = indah.Signal("")
    deleted: indah.Signal[bool] = indah.Signal(False)
    confirm: indah.Signal[str] = indah.Signal("")

    async def reload() -> None:
        try:
            usage = await blocking(tab.service.usage)
            state = await blocking(tab.service.workspace_status)
        except SemulaError as exc:
            screen.error.set(describe(exc))
            return
        lines.set(usage_lines(usage))
        deleted.set(bool(state["deleted_at"]))
        status.set(
            f"Deleted {stamp(state['deleted_at'])}. Restorable until "
            f"{stamp(state['purge_after'])}, then purged for good."
            if state["deleted_at"]
            else ""
        )

    screen.on_open(reload)

    async def delete() -> None:
        state = await screen.attempt(tab.service.delete_workspace, confirm.peek())
        if state:
            confirm.set("")
            screen.note.set("Workspace deleted. Its tokens have stopped.")
        await reload()

    async def restore() -> None:
        if await screen.attempt(tab.service.restore_workspace):
            screen.note.set("Workspace restored.")
        await reload()

    table = indah.Table(
        rows=lines,
        key="id",
        label="Usage",
        density="compact",
        columns=[
            indah.Col("label", title="What", sortable=False),
            indah.Col("value", title="Used", kind="mono", sortable=False),
            indah.Col("limit", title="Limit", kind="mono", sortable=False),
        ],
    )
    workspace = tab.who.workspace if tab.who else ""
    return indah.Column(
        children=[
            indah.Text("# Usage", markdown=True),
            *_lines(screen),
            table,
            indah.Text("## Delete this workspace", markdown=True),
            indah.Text(
                "Its tokens stop working at once and its data is purged after 30 days. Until "
                "then it can be exported or restored. Type the workspace id to confirm.",
                size="body",
            ),
            indah.Text(lambda: status.value, size="body"),
            indah.Row(
                children=[
                    indah.TextInput(confirm, label="Workspace id", placeholder=workspace or ""),
                    indah.Button(
                        "Delete workspace", on_click=delete, confirm="Delete this workspace?"
                    ),
                    indah.Button("Restore workspace", on_click=restore, variant="ghost"),
                ],
                align="end",
            ),
        ]
    )
