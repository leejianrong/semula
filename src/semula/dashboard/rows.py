"""Shape service data into dashboard rows. Pure functions: no indah, no database."""

from __future__ import annotations

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
