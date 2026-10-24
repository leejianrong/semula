"""Per-project matching and recording settings (ADR-0007).

Stored as JSON on the project row. Every field is optional; the defaults are what
applies when it is unset. ``extra="forbid"`` so a typo is an error, not a silent no-op.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .canonical import BUILTIN_REDACTIONS, FingerprintConfig

Param = Literal["model", "temperature", "top_p", "max_tokens", "stream", "user", "seed"]

# Settings that change how requests are fingerprinted; changing one re-keys the project.
FINGERPRINT_FIELDS = (
    "tail_n",
    "scrub_patterns",
    "include_params",
    "redact_patterns",
    "redact_defaults",
)

# Decisions about this server (and its money), not about the prompts: a bundle never carries them
# and importing one never changes them.
OPERATOR_FIELDS = (
    "mode",
    "record_through",
    "upstream_model",
    "upstream_max_calls",
    "upstream_max_tokens",
    "upstream_dry_run",
)


class ProjectSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["replay", "record"] | None = Field(
        default=None,
        description=(
            "'record' parks a call that has no recorded answer for an agent to answer; "
            "'replay' fails it fast. Unset, the project follows the server's default "
            "(SEMULA_MODE). Kept across restarts; never exported."
        ),
    )
    models: list[str] | None = Field(
        default=None, description="Model ids advertised on /v1/models (default: server-wide)."
    )
    tail_n: int | None = Field(
        default=None, ge=1, description="Match only the last N messages (default: all)."
    )
    scrub_patterns: list[str] = Field(
        default_factory=list, description="Extra regexes whose matches are ignored when matching."
    )
    redact_patterns: list[str] = Field(
        default_factory=list,
        description=(
            "Regexes for secrets. Matches become [REDACTED] in everything stored (existing "
            "recordings too) and are ignored when matching, so a changing secret does not miss."
        ),
    )
    redact_defaults: bool = Field(
        default=True,
        description="Also redact common credentials: API keys, GitHub and Slack tokens, bearer "
        "tokens, private keys.",
    )
    include_params: list[Param] = Field(
        default_factory=list,
        description=(
            "Request parameters that count when matching. Ignored by default: model, "
            "temperature, top_p, max_tokens, stream, user, seed."
        ),
    )
    record_timeout: float | None = Field(
        default=None,
        gt=0,
        description="Seconds a call is held in record mode (default: server-wide).",
    )
    record_through: bool = Field(
        default=False,
        description=(
            "Opt in to forwarding a miss to the server's upstream provider (SEMULA_UPSTREAM_*) "
            "and saving its answer. Needs the project in record mode; replay never forwards. "
            "Spends the operator's credits, so it is capped by upstream_max_calls. Never exported."
        ),
    )
    upstream_model: str | None = Field(
        default=None,
        description="Model id sent upstream instead of the one the request names. Never exported.",
    )
    upstream_max_calls: int = Field(
        default=100,
        ge=0,
        description=(
            "Most calls this project may forward upstream; after that a miss fails with "
            "semula_upstream_budget until the count is reset. Never exported."
        ),
    )
    upstream_max_tokens: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Most prompt plus completion tokens the project may spend upstream. Tokens are only "
            "known after a call, so one call may overshoot; the next miss then fails with "
            "semula_upstream_budget. Unset means calls are the only cap. Never exported."
        ),
    )
    upstream_dry_run: bool = Field(
        default=False,
        description=(
            "With record_through on, a miss is not forwarded: it fails with "
            "semula_upstream_dry_run and is listed as what would have been called "
            "(semula project upstream). Never exported."
        ),
    )

    bm25_serve_threshold: float = Field(
        default=1.0,
        gt=0,
        le=1,
        description=(
            "Word-overlap score (0-1) at or above which a close lexical match is served "
            "automatically. The default, 1, serves only prompts with the same words, so "
            "near-misses go to review until you lower it."
        ),
    )
    bm25_review_threshold: float = Field(
        default=0.6,
        gt=0,
        le=1,
        description=(
            "Score below which a lexical match is ignored. Between this and the serve "
            "threshold it goes to the review queue and the call is still a miss."
        ),
    )

    embedding_review_threshold: float = Field(
        default=0.75,
        gt=0,
        le=1,
        description=(
            "Cosine similarity (0-1) from which a semantic match joins the review queue. Only "
            "used when the server runs with SEMULA_EMBEDDINGS=on."
        ),
    )
    embedding_serve_threshold: float | None = Field(
        default=None,
        gt=0,
        le=1,
        description=(
            "Opt in to serving a semantic match without review at or above this cosine "
            "similarity. Off by default: a paraphrase is not the same question. Use is logged "
            "(tier 'embedding')."
        ),
    )

    @model_validator(mode="after")
    def _embedding_serve_at_least_review(self) -> ProjectSettings:
        serve = self.embedding_serve_threshold
        if serve is not None and serve < self.embedding_review_threshold:
            raise ValueError(
                f"embedding_serve_threshold ({serve}) cannot be below "
                f"embedding_review_threshold ({self.embedding_review_threshold})."
            )
        return self

    @model_validator(mode="after")
    def _review_at_most_serve(self) -> ProjectSettings:
        if self.bm25_review_threshold > self.bm25_serve_threshold:
            raise ValueError(
                f"bm25_review_threshold ({self.bm25_review_threshold}) cannot exceed "
                f"bm25_serve_threshold ({self.bm25_serve_threshold})."
            )
        return self

    @field_validator("scrub_patterns", "redact_patterns")
    @classmethod
    def _compile(cls, patterns: list[str]) -> list[str]:
        for pattern in patterns:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"{pattern!r} is not a valid regular expression: {exc}") from exc
        return patterns

    def fingerprint_config(self) -> FingerprintConfig:
        return FingerprintConfig(
            tail_n=self.tail_n,
            extra_scrubs=tuple(self.scrub_patterns),
            include_params=tuple(self.include_params),
            redactions=(
                *(BUILTIN_REDACTIONS if self.redact_defaults else ()),
                *self.redact_patterns,
            ),
        )
