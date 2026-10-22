"""Runtime settings, read from the environment (ADR-0001)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Literal

from .oidc import Provider, providers_from_env

Mode = Literal["replay", "record"]


def _flag(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Quotas:
    """What one workspace may hold or do (ADR-0011 section 6). None means no limit, which is
    what a local server has; a hosted one starts from ``HOSTED_QUOTAS``."""

    projects: int | None = None
    entries: int | None = None
    bytes: int | None = None  # of stored requests and answers
    parked_per_project: int | None = None
    parked_per_workspace: int | None = None
    tokens: int | None = None
    members: int | None = None
    body_bytes: int | None = None  # one request body, at the edge
    record_timeout_max: float | None = None  # seconds a project may hold a call for an agent
    rate: float | None = None  # requests per second per token, steady
    burst: int | None = None  # and the most a token may spend at once


HOSTED_QUOTAS = Quotas(
    projects=20,
    entries=50_000,
    bytes=250_000_000,
    parked_per_project=50,
    parked_per_workspace=200,
    tokens=50,
    members=25,
    body_bytes=1_000_000,
    record_timeout_max=900.0,
    rate=20.0,
    burst=100,
)


def _quotas_from_env(hosted: bool) -> Quotas:
    """Hosted defaults, each changeable with ``SEMULA_QUOTA_<NAME>`` (``none`` removes a limit)."""
    base = HOSTED_QUOTAS if hosted else Quotas()
    changed: dict[str, int | float | None] = {}
    for item in fields(Quotas):
        raw = os.environ.get(f"SEMULA_QUOTA_{item.name.upper()}")
        if raw is None:
            continue
        changed[item.name] = (
            None if raw.strip().lower() == "none" else (float(raw) if "." in raw else int(raw))
        )
    return replace(base, **changed)  # type: ignore[arg-type]


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    mode: Mode = "replay"
    record_timeout: float = 300.0
    host: str = "127.0.0.1"
    port: int = 8080
    chunk_size: int = 24
    embeddings: bool = False  # SEMULA_EMBEDDINGS=on; needs the `embeddings` extra
    embeddings_offline: bool = False  # never download the model; fail if it is not cached
    model_dir: Path | None = None  # default: <data dir>/models, on the volume
    log_retention_days: int = 30
    log_max_rows: int = 50_000
    models: tuple[str, ...] = ("semula",)
    ui: bool = True  # SEMULA_UI=off hides the dashboard at /ui
    api_key: str | None = field(default=None, repr=False)
    # Hosted mode: every call needs an API token, which names the workspace (ADR-0010). Off
    # locally, where whoever can reach the port is trusted (or the shared key above).
    hosted: bool = False
    # Replicas on one Postgres: migrate once, before rollout, instead of on every start.
    migrate: bool = True
    # People signing in (hosted): where the server is reached from (cookies, redirect URIs), a
    # secret of at least 16 bytes for signing sign-in state, and the providers that are set up.
    public_url: str = "http://127.0.0.1:8080"
    secret: str = field(default="", repr=False)
    auth_providers: dict[str, Provider] = field(default_factory=dict)
    quotas: Quotas = field(default_factory=Quotas)
    database_url: str | None = field(default=None, repr=False)  # may hold a password

    @property
    def db_path(self) -> Path:
        return self.data_dir / "semula.db"

    @property
    def resolved_model_dir(self) -> Path:
        return self.model_dir or self.data_dir / "models"

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
            embeddings=_flag(os.environ.get("SEMULA_EMBEDDINGS", "off")),
            embeddings_offline=_flag(os.environ.get("SEMULA_EMBEDDINGS_OFFLINE", "off")),
            model_dir=Path(os.environ["SEMULA_MODEL_DIR"])
            if os.environ.get("SEMULA_MODEL_DIR")
            else None,
            log_retention_days=int(os.environ.get("SEMULA_LOG_RETENTION_DAYS", "30")),
            log_max_rows=int(os.environ.get("SEMULA_LOG_MAX_ROWS", "50000")),
            ui=_flag(os.environ.get("SEMULA_UI", "on")),
            api_key=os.environ.get("SEMULA_API_KEY") or None,
            hosted=_flag(os.environ.get("SEMULA_HOSTED", "off")),
            migrate=_flag(os.environ.get("SEMULA_MIGRATE", "on")),
            public_url=os.environ.get("SEMULA_PUBLIC_URL", "http://127.0.0.1:8080").rstrip("/"),
            secret=os.environ.get("SEMULA_SECRET", ""),
            auth_providers=providers_from_env(dict(os.environ)),
            quotas=_quotas_from_env(_flag(os.environ.get("SEMULA_HOSTED", "off"))),
            database_url=os.environ.get("SEMULA_DATABASE_URL") or None,
            models=tuple(
                m.strip() for m in os.environ.get("SEMULA_MODELS", "semula").split(",") if m.strip()
            ),
        )
