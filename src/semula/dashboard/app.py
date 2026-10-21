"""The indah application: sessions, theme and the live call topic."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import indah

from ..service import Service
from .shell import build_session
from .state import Dash
from .theme import build_theme

LOGO = Path(__file__).with_name("semula-mark.svg")


def build_dashboard(service: Service) -> Any:
    dash = Dash(service)

    def build(ctx: indah.Context) -> indah.Session:
        return build_session(ctx, dash)

    app = indah.create_app(
        session_factory=build,
        title="semula",
        theme=build_theme(),
        spa=True,
        proxy_flush=False,  # a local container has no buffering proxy in front
        density="compact",
        logo=LOGO,
    )
    app.state.dash = dash
    app.state.publish = dash.publish
    return app
