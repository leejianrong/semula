"""Runtime settings, read from the environment (ADR-0001)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Mode = Literal["replay", "record"]


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    mode: Mode = "replay"
    record_timeout: float = 300.0
    host: str = "127.0.0.1"
    port: int = 8080

    @property
    def db_path(self) -> Path:
        return self.data_dir / "semula.db"

    @classmethod
    def from_env(cls) -> Settings:
        mode = os.environ.get("SEMULA_MODE", "replay")
        if mode not in ("replay", "record"):
            raise ValueError(f"SEMULA_MODE must be 'replay' or 'record', got {mode!r}")
        return cls(
            data_dir=Path(os.environ.get("SEMULA_DATA_DIR", "./data")),
            mode=mode,  # type: ignore[arg-type]
            record_timeout=float(os.environ.get("SEMULA_RECORD_TIMEOUT", "300")),
            host=os.environ.get("SEMULA_HOST", "127.0.0.1"),
            port=int(os.environ.get("SEMULA_PORT", "8080")),
        )
