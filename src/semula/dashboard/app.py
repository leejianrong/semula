"""The indah application: sessions, theme and the live call topic."""

from __future__ import annotations

from typing import Any

import indah

from ..service import Service

TOPIC_REPLAY = 1000  # calls a late tab can see at once, before it asks for more


def build_dashboard(service: Service) -> Any:
    topic: indah.Topic[dict[str, Any]] = indah.Topic("calls", replay=TOPIC_REPLAY)

    def build(ctx: indah.Context) -> indah.Session:
        projects = service.list_projects()
        return indah.Session(
            indah.Column(
                children=[
                    indah.Text("# semula", markdown=True),
                    indah.Text(
                        f"Mode **{service.mode}**. {len(projects)} project(s).", markdown=True
                    ),
                ]
            )
        )

    app = indah.create_app(
        session_factory=build,
        title="semula",
        spa=True,
        proxy_flush=False,  # a local container has no buffering proxy in front
    )
    app.state.topic = topic
    app.state.publish = topic.publish
    return app
