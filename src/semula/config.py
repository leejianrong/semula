"""Runtime settings, read from the environment (ADR-0001)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
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
    chunk_size: int = 24
    log_retention_days: int = 30
    log_max_rows: int = 50_000
    models: tuple[str, ...] = ("semula",)
    api_key: str | None = field(default=None, repr=False)
    database_url: str | None = field(default=None, repr=False)  # may hold a password

    @property
    def db_path(self) -> Path:
        return self.data_dir / "semula.db"

    @property
    def resolved_database_url(self) -> str:
        """``SEMULA_DATABASE_URL`` if set, else a SQLite file in the data dir."""
        return self.database_url or f"sqlite:///{self.db_path}"

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
            chunk_size=int(os.environ.get("SEMULA_CHUNK_SIZE", "24")),
            log_retention_days=int(os.environ.get("SEMULA_LOG_RETENTION_DAYS", "30")),
            log_max_rows=int(os.environ.get("SEMULA_LOG_MAX_ROWS", "50000")),
            api_key=os.environ.get("SEMULA_API_KEY") or None,
            database_url=os.environ.get("SEMULA_DATABASE_URL") or None,
            models=tuple(
                m.strip() for m in os.environ.get("SEMULA_MODELS", "semula").split(",") if m.strip()
            ),
        )
