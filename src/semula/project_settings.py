"""Per-project matching and recording settings (ADR-0007).

Stored as JSON on the project row. Every field is optional; the defaults are what
applies when it is unset. ``extra="forbid"`` so a typo is an error, not a silent no-op.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .canonical import FingerprintConfig

Param = Literal["model", "temperature", "top_p", "max_tokens", "stream", "user", "seed"]

# Settings that change how requests are fingerprinted; changing one re-keys the project.
FINGERPRINT_FIELDS = ("tail_n", "scrub_patterns", "include_params")


class ProjectSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    models: list[str] | None = Field(
        default=None, description="Model ids advertised on /v1/models (default: server-wide)."
    )
    tail_n: int | None = Field(
        default=None, ge=1, description="Match only the last N messages (default: all)."
    )
    scrub_patterns: list[str] = Field(
        default_factory=list, description="Extra regexes whose matches are ignored when matching."
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

    @field_validator("scrub_patterns")
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
        )
