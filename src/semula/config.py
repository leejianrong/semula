"""Runtime settings, read from the environment (ADR-0001)."""

from __future__ import annotations

import ipaddress
import json
import os
import re
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


def _answerer_from_env() -> dict[str, object]:
    """The answerer hook's settings. A command is a JSON array of strings (an argv, never a shell
    line); a URL is http(s); not both. Names to pass through may not be ours."""
    raw = os.environ.get("SEMULA_ANSWERER_COMMAND", "").strip()
    url = os.environ.get("SEMULA_ANSWERER_URL", "").strip() or None
    command: tuple[str, ...] = ()
    if raw:
        try:
            parsed = json.loads(raw)
        except ValueError as exc:
            raise ValueError(
                'SEMULA_ANSWERER_COMMAND must be a JSON array of strings, like ["claude", "-p"]'
            ) from exc
        if not (
            isinstance(parsed, list) and parsed and all(isinstance(a, str) and a for a in parsed)
        ):
            raise ValueError("SEMULA_ANSWERER_COMMAND must be a non-empty JSON array of strings")
        command = tuple(parsed)
    if command and url:
        raise ValueError("Set SEMULA_ANSWERER_COMMAND or SEMULA_ANSWERER_URL, not both")
    if url and not url.startswith(("http://", "https://")):
        raise ValueError("SEMULA_ANSWERER_URL must start with http:// or https://")
    names = tuple(
        n.strip() for n in os.environ.get("SEMULA_ANSWERER_ENV", "").split(",") if n.strip()
    )
    if any(n.upper().startswith("SEMULA_") for n in names):
        raise ValueError("SEMULA_ANSWERER_ENV cannot pass on a SEMULA_ variable")
    return {
        "answerer_command": command,
        "answerer_url": url,
        "answerer_bearer": os.environ.get("SEMULA_ANSWERER_BEARER") or None,
        "answerer_timeout": float(os.environ.get("SEMULA_ANSWERER_TIMEOUT", "120")),
        "answerer_concurrency": max(1, int(os.environ.get("SEMULA_ANSWERER_CONCURRENCY", "2"))),
        "answerer_env": names,
    }


def _networks_from_env(raw: str) -> tuple[str, ...]:
    """``SEMULA_TRUSTED_PROXIES``: comma-separated addresses or CIDR ranges. A wildcard is
    refused, because trusting every peer lets anyone choose the address the limits see."""
    out: list[str] = []
    for item in (part.strip() for part in raw.split(",")):
        if not item:
            continue
        try:
            ipaddress.ip_network(item, strict=False)
        except ValueError as exc:
            raise ValueError(
                f"SEMULA_TRUSTED_PROXIES: {item!r} is not an address or a CIDR range"
            ) from exc
        out.append(item)
    return tuple(out)


def _signup_from_env() -> Literal["code"]:
    raw = os.environ.get("SEMULA_SIGNUP", "code").strip().lower()
    if raw != "code":
        raise ValueError(
            f"SEMULA_SIGNUP: only 'code' is available (a person needs an invite code), got {raw!r}"
        )
    return "code"


def _abuse_contact_from_env() -> str:
    """An email address for reports of abuse, shown on the sign-in page and the terms. Checked
    because it is written into a page."""
    raw = os.environ.get("SEMULA_ABUSE_CONTACT", "").strip()
    if raw and not re.fullmatch(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+", raw):
        raise ValueError(f"SEMULA_ABUSE_CONTACT: {raw!r} is not an email address")
    return raw


def _limit_from_env(name: str, default: int) -> int | None:
    """A whole number of at least 1, or ``none`` to remove the limit."""
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw == "none":
        return None
    if not raw.isdigit() or int(raw) < 1:
        raise ValueError(f"{name} must be a whole number of at least 1, or 'none', got {raw!r}")
    return int(raw)


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
    # Reverse proxies (addresses or CIDR ranges) whose X-Forwarded-For is believed. Empty: the
    # header is ignored and the socket peer is the client.
    trusted_proxies: tuple[str, ...] = ()
    # How a person gets a workspace of their own: with an invite code the operator made.
    signup: Literal["code"] = "code"
    abuse_contact: str = ""  # where to report abuse (an email address); shown when hosted
    # Hosted only (a local server has no one to hold back). ``None`` removes a limit.
    auth_rate: int | None = 30  # /auth/* requests a minute for one address
    ui_streams_per_address: int | None = 40  # open dashboard streams from one address
    ui_sessions: int | None = 2000  # open dashboard tabs on this process, in all
    ui_tabs_per_person: int | None = 10  # open dashboard tabs of one signed-in person
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
    # Record-through (ADR-0013): the real provider a project may forward a miss to. The key lives
    # here and nowhere else; it is never stored, logged, exported or echoed in an error.
    upstream_url: str = "https://api.openai.com/v1"
    upstream_key: str | None = field(default=None, repr=False)
    upstream_timeout: float = 120.0
    upstream_tenants: bool = False  # hosted: may a workspace admin turn forwarding on?
    # The answerer hook (ADR-0014): the operator's command (an argv list) or webhook that answers
    # parked calls. Server configuration only, never a project setting, never shown anywhere.
    answerer_command: tuple[str, ...] = field(default=(), repr=False)
    answerer_url: str | None = field(default=None, repr=False)
    answerer_bearer: str | None = field(default=None, repr=False)
    answerer_timeout: float = 120.0
    answerer_concurrency: int = 2
    answerer_env: tuple[
        str, ...
    ] = ()  # names of variables the child may inherit, besides the basics

    @property
    def answerer_configured(self) -> bool:
        return bool(self.answerer_command or self.answerer_url)

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
            trusted_proxies=_networks_from_env(os.environ.get("SEMULA_TRUSTED_PROXIES", "")),
            signup=_signup_from_env(),
            abuse_contact=_abuse_contact_from_env(),
            auth_rate=_limit_from_env("SEMULA_AUTH_RATE", 30),
            ui_streams_per_address=_limit_from_env("SEMULA_UI_STREAMS_PER_ADDRESS", 40),
            ui_sessions=_limit_from_env("SEMULA_UI_SESSIONS", 2000),
            ui_tabs_per_person=_limit_from_env("SEMULA_UI_TABS_PER_PERSON", 10),
            api_key=os.environ.get("SEMULA_API_KEY") or None,
            hosted=_flag(os.environ.get("SEMULA_HOSTED", "off")),
            migrate=_flag(os.environ.get("SEMULA_MIGRATE", "on")),
            public_url=os.environ.get("SEMULA_PUBLIC_URL", "http://127.0.0.1:8080").rstrip("/"),
            secret=os.environ.get("SEMULA_SECRET", ""),
            auth_providers=providers_from_env(dict(os.environ)),
            quotas=_quotas_from_env(_flag(os.environ.get("SEMULA_HOSTED", "off"))),
            database_url=os.environ.get("SEMULA_DATABASE_URL") or None,
            upstream_url=os.environ.get("SEMULA_UPSTREAM_URL", "https://api.openai.com/v1").rstrip(
                "/"
            ),
            upstream_key=os.environ.get("SEMULA_UPSTREAM_KEY") or None,
            upstream_timeout=float(os.environ.get("SEMULA_UPSTREAM_TIMEOUT", "120")),
            upstream_tenants=_flag(os.environ.get("SEMULA_UPSTREAM_TENANTS", "off")),
            **_answerer_from_env(),  # type: ignore[arg-type]
            models=tuple(
                m.strip() for m in os.environ.get("SEMULA_MODELS", "semula").split(",") if m.strip()
            ),
        )
