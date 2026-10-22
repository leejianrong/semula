"""Shape service data into dashboard rows. Pure functions: no indah, no database."""

from __future__ import annotations

import contextlib
import json
from datetime import UTC, datetime, tzinfo
from typing import Any

OUTCOMES = {
    "hit": "Hit",
    "miss": "Miss",
    "recorded": "Recorded",
    "record_timeout": "Timed out",
    "forced_write": "Forced edit",
    "forced_delete": "Forced delete",
}
OUTCOME_TONES = {
    "Hit": "ok",
    "Miss": "warn",
    "Recorded": "info",
    "Timed out": "err",
    "Forced edit": "warn",
    "Forced delete": "warn",
}
TIERS = {
    "exact": "Exact",
    "normalised": "Normalised",
    "alias": "Alias",
    "bm25": "Words",
    "embedding": "Meaning",
}
# The filter chips on the log: label -> the outcomes it keeps (None keeps all).
FILTERS: dict[str, tuple[str, ...] | None] = {
    "All": None,
    "Hits": ("hit",),
    "Misses": ("miss", "record_timeout"),
    "Recorded": ("recorded",),
}


PROMPT_COLUMN = 64  # characters of the prompt the table shows; the detail card has it all


def short(text: str, limit: int = PROMPT_COLUMN) -> str:
    """Cut ``text`` for a table cell. A plain cell cannot truncate by width (indah sizes it to
    its content), so a long prompt would push every other column off the page."""
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def parse_ts(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)


def clock(moment: datetime, tz: tzinfo | None = None) -> str:
    """``09:14:22.318``, in the machine's zone unless ``tz`` is given."""
    return f"{moment.astimezone(tz):%H:%M:%S}.{moment.microsecond // 1000:03d}"


def took(latency_ms: float | None) -> str:
    if latency_ms is None:
        return ""
    if latency_ms >= 60_000:
        return f"{latency_ms / 60_000:.1f} min"
    if latency_ms >= 1000:
        return f"{latency_ms / 1000:.1f} s"
    return f"{latency_ms:.0f} ms"


def match_text(tier: str | None, score: float | None) -> str:
    """``Exact``, or ``Words 0.98``: what answered, with its score unless it was exact."""
    if not tier:
        return ""
    name = TIERS.get(tier, tier)
    return (
        name
        if tier in ("exact", "normalised", "alias") or score is None
        else (f"{name} {score:.2f}")
    )


def call_row(call: dict[str, Any], tz: tzinfo | None = None) -> dict[str, Any]:
    """One log row as the table shows it, from a ``list_calls`` item or a live ``call`` event."""
    moment = parse_ts(call["ts"])
    return {
        "id": str(call["id"]),
        "at": int(moment.timestamp() * 1000),
        "when": clock(moment, tz),
        "project": call["project"],
        "outcome": call["outcome"],
        "result": OUTCOMES.get(call["outcome"], call["outcome"]),
        "tier": call.get("tier"),
        "score": call.get("score"),
        "match": match_text(call.get("tier"), call.get("score")),
        "took": took(call.get("latency_ms")),
        "prompt": short(call.get("preview") or ""),
        "preview": call.get("preview") or "",
        "entry_id": call.get("entry_id"),
        "pending_id": call.get("pending_id"),
    }


def keeps(row: dict[str, Any], project: str | None, chip: str) -> bool:
    """Does ``row`` belong on the log for this project and filter chip?"""
    if project and row["project"] != project:
        return False
    outcomes = FILTERS.get(chip)
    return outcomes is None or row["outcome"] in outcomes


RAIL = ("exact", "normalised", "alias", "words", "meaning")
RAIL_NAMES = {"exact": "Exact", "normalised": "Normalised", "alias": "Alias", "words": "Words",
              "meaning": "Meaning"}  # fmt: skip
_LOG_TIER_TO_RAIL = {"exact": "exact", "normalised": "normalised", "alias": "alias",
                     "bm25": "words", "embedding": "meaning"}  # fmt: skip


def rail_for(call: dict[str, Any], *, meaning_on: bool) -> list[dict[str, str]]:
    """The five tiers as one call went through them, from what the log kept: the tier that
    answered and its score. Tiers before it said no, tiers after were never asked. A call that
    nothing answered had every tier say no (the log does not keep the near misses)."""
    answered = _LOG_TIER_TO_RAIL.get(call.get("tier") or "")
    reached = RAIL.index(answered) if answered else len(RAIL)
    out = []
    for i, tier in enumerate(RAIL):
        if tier == "meaning" and not meaning_on and answered != "meaning":
            verdict, text = "off", "Off"
        elif i < reached:
            verdict, text = "no", "No"
        elif i == reached:
            score = call.get("score")
            verdict = "match"
            text = (
                "Match"
                if tier in ("exact", "normalised", "alias") or score is None
                else (f"Match, {score:.2f}")
            )
        else:
            verdict, text = "skipped", "Skipped"
        out.append({"id": tier, "name": RAIL_NAMES[tier], "verdict": verdict, "text": text,
                    "dot": DOT_STYLES[verdict]})  # fmt: skip
    return out


def rail_from_tests(tiers: list[dict[str, Any]]) -> list[dict[str, str]]:
    """The same rows from a ``test_match`` answer (the playground and the review screen)."""
    out = []
    for t in tiers:
        verdict, score, reason = t["verdict"], t.get("score"), t.get("reason", "")
        text = {"no": "No", "match": "Match", "skipped": "Skipped", "off": "Off",
                "candidate": "In review"}[verdict]  # fmt: skip
        if score is not None and verdict in ("match", "candidate"):
            text = f"{score:.2f}, in review" if verdict == "candidate" else f"Match, {score:.2f}"
        elif reason and verdict == "no":
            text = f"No, {reason}"
        out.append({"id": t["tier"], "name": RAIL_NAMES[t["tier"]], "verdict": verdict,
                    "text": text, "dot": DOT_STYLES[verdict]})  # fmt: skip
    return out


_DOT = (
    "width:.7rem;height:.7rem;border-radius:50%;box-sizing:border-box;flex:none;"
    "display:inline-block;"
)
DOT_STYLES = {
    "match": _DOT + "background:var(--primary);border:2px solid var(--primary)",
    "candidate": _DOT + "background:var(--warn);border:2px solid var(--warn)",
    "no": _DOT + "border:2px solid var(--muted)",
    "skipped": _DOT + "border:2px dashed var(--outline-soft)",
    "off": _DOT + "border:2px dotted var(--outline-soft)",
}


def pending_row(item: dict[str, Any], tz: tzinfo | None = None) -> dict[str, Any]:
    """One parked call as the Pending table shows it, from a ``list_pending`` item."""
    moment = parse_ts(item["created_at"])
    prompt = (item.get("last_message") or "").replace("\n", " ")
    return {
        "id": item["id"],
        "project": item["project"],
        "since": clock(moment, tz),
        "prompt": short(prompt),
    }


def answer_from_text(text: str) -> Any:
    """What the viewer typed, as the answer to send. Text that starts like JSON (``{`` or ``[``)
    must be valid JSON and goes as given (a tool call, a chunk list); anything else is a plain
    reply. Raises ``ValueError`` with a sentence to show beside the field."""
    body = text.strip()
    if not body:
        raise ValueError("Write an answer first.")
    if body[0] in "{[":
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"That looks like JSON but is not valid: {exc.msg} (line {exc.lineno})."
            ) from exc
    return {"content": text}


# The Entries screen: the filter menu (label -> list_entries arguments) and the sort menu.
ENTRY_FILTERS: dict[str, dict[str, bool]] = {
    "All": {},
    "Pinned": {"pinned": True},
    "Never hit": {"never_hit": True},
    "Has tool calls": {"has_tools": True},
    "Has aliases": {"has_alias": True},
}
ENTRY_SORTS: dict[str, tuple[str | None, str | None]] = {
    "Oldest first": (None, None),
    "Most hits": ("hits", "desc"),
    "Changed lately": ("updated", "desc"),
    "Newest first": ("created", "desc"),
}
ENTRY_PAGE = 50


def entry_filters(label: str) -> dict[str, bool]:
    """The ``list_entries`` arguments a filter-menu label stands for."""
    found = dict(ENTRY_FILTERS.get(label, {}))
    if "has_tools" in found:  # the service calls it tool_calls
        found["tool_calls"] = found.pop("has_tools")
    return found


def entry_flags(entry: dict[str, Any]) -> str:
    flags = []
    if entry.get("pinned"):
        flags.append("Pinned")
    if entry.get("has_tools"):
        flags.append("Tools")
    aliases = entry.get("alias_count") or 0
    if aliases:
        flags.append(f"{aliases} alias" if aliases == 1 else f"{aliases} aliases")
    return " · ".join(flags)


def entry_row(entry: dict[str, Any]) -> dict[str, Any]:
    """One entry as the Entries table shows it, from a ``list_entries`` item."""
    answers = entry.get("responses")
    answers = answers if isinstance(answers, int) else len(answers or [])
    selection = entry.get("selection") or "first"
    return {
        "id": entry["id"],
        "prompt": short((entry.get("last_message") or "").replace("\n", " ")),
        "hits": entry.get("hit_count", 0),
        "answers": str(answers) if answers <= 1 else f"{answers}, {selection}",
        "flags": entry_flags(entry),
        "version": f"v{entry['version']}",
        "updated": entry["updated_at"][:16].replace("T", " "),
    }


def page_text(total: int, offset: int, shown: int) -> str:
    """``1 to 50 of 123``, or ``No entries``."""
    if not total:
        return "No entries"
    return f"{offset + 1} to {offset + shown} of {total}"


def revision_row(revision: dict[str, Any], tz: tzinfo | None = None) -> dict[str, Any]:
    """One line of an entry's history, from an ``entry_history`` revision."""
    forced = " (forced past a pin)" if revision.get("forced") else ""
    return {
        "id": f"h{revision['version']}",
        "version": f"v{revision['version']}",
        "when": clock(parse_ts(revision["ts"]), tz) if revision.get("ts") else "",
        "actor": revision["actor"],
        "summary": revision["summary"] + forced,
    }


def answer_form(response: dict[str, Any]) -> dict[str, Any]:
    """A stored response as the answer a person would type: ``{"content": ...}``, a tool call
    with its arguments as an object, or ``{"chunks": [...]}``. ``parse_answer`` takes it back."""
    message = response.get("message") or {}
    if response.get("chunks"):
        return {"chunks": [c.get("content", "") for c in response["chunks"]]}
    if message.get("tool_calls"):
        calls = []
        for call in message["tool_calls"]:
            fn = dict(call.get("function") or {})
            with contextlib.suppress(TypeError, ValueError):  # not JSON: leave the text as it is
                fn["arguments"] = json.loads(fn.get("arguments") or "{}")
            calls.append({"function": fn})
        return {"message": {"content": message.get("content"), "tool_calls": calls}}
    return {"content": message.get("content") or ""}


def editor_text(responses: list[dict[str, Any]]) -> str:
    """What the entry editor opens with: the text itself for one plain answer, otherwise JSON."""
    forms = [answer_form(r) for r in responses]
    if len(forms) == 1:
        only = forms[0]
        if list(only) == ["content"]:
            return str(only["content"])
        return json.dumps(only, indent=2, ensure_ascii=False)
    return json.dumps(forms, indent=2, ensure_ascii=False)


def answers_from_editor(text: str, *, multiple: bool) -> list[Any]:
    """The answers an edit sends, from the editor's text. Raises ``ValueError`` with a sentence
    to show beside the field."""
    if not multiple:
        return [answer_from_text(text)]
    try:
        body = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"That is not valid JSON: {exc.msg} (line {exc.lineno}).") from exc
    if not isinstance(body, list) or not body:
        raise ValueError("This entry has several answers: write a JSON list with one per answer.")
    return body


def new_entry_request(prompt: str, system: str, tools_text: str) -> dict[str, Any]:
    """The chat request the New entry form describes: an optional system message, the prompt
    as one user message, and optional tools given as a JSON list. Raises ``ValueError`` with a
    sentence to show beside the field."""
    if not prompt.strip():
        raise ValueError("Write the prompt the app will send.")
    messages = []
    if system.strip():
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    request: dict[str, Any] = {"messages": messages}
    if tools_text.strip():
        try:
            tools = json.loads(tools_text)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"The tools are not valid JSON: {exc.msg} (line {exc.lineno})."
            ) from exc
        if not isinstance(tools, list) or not all(isinstance(t, dict) for t in tools):
            raise ValueError("The tools must be a JSON list of tool objects.")
        request["tools"] = tools
    return request


def request_text(request: dict[str, Any]) -> str:
    """A request as plain text for a side-by-side comparison: one block per message, then the
    names of the tools it offers."""
    blocks = []
    for message in request.get("messages", []):
        content = message.get("content")
        text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
        blocks.append(f"{message.get('role', '')}:\n{text}")
    names = [(t.get("function") or t).get("name", "?") for t in request.get("tools") or []]
    if names:
        blocks.append("tools: " + ", ".join(names))
    return "\n\n".join(blocks)


def candidate_row(candidate: dict[str, Any]) -> dict[str, Any]:
    """One near miss as the review table shows it, from a ``list_candidates`` item."""
    score = candidate.get("score")
    return {
        "id": candidate["id"],
        "score": f"{score:.2f}" if score is not None else "",
        "tier": TIERS.get(candidate.get("tier") or "", candidate.get("tier") or ""),
        "prompt": short((candidate.get("last_message") or "").replace("\n", " "), 48),
        "entry": short((candidate.get("entry_last_message") or "").replace("\n", " "), 48),
    }


def verdict_text(result: dict[str, Any]) -> str:
    """The playground's one-line answer, from a ``test_match`` result."""
    if result["matched"]:
        score = result.get("score")
        tier = TIERS.get(result["tier"], result["tier"])
        shown = (
            f", score {score:.2f}"
            if score is not None and result["tier"] in ("bm25", "embedding")
            else ""
        )
        return f"Would serve the recorded answer ({tier}{shown})."
    if result["would"] == "park":
        return "Would park this call for an agent to answer (the project is recording)."
    return "Would fail with 424: nothing recorded matches (the project is replaying)."


def near_text(result: dict[str, Any]) -> str:
    """What came close on a miss: the nearest entry and any weak candidates for review."""
    parts = []
    nearest = result.get("nearest")
    if nearest:
        parts.append(
            f'Nearest recorded prompt: "{nearest["last_message"]}" (score {nearest["score"]:.2f}).'
        )
    waiting = result.get("candidates") or []
    if waiting and not result["matched"]:
        parts.append(f"{len(waiting)} weak candidate(s) would go to the review queue.")
    return " ".join(parts)
