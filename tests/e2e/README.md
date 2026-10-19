# e2e

Tests marked `e2e` run the real app over loopback and drive it with the OpenAI SDK.
They are kept out of the fast gate and run as their own CI job (`uv run pytest -m e2e`).

`test_record_replay.py` is the walking-skeleton acceptance demo: record through the
CLI, restart in replay mode, replay with every non-loopback connection refused.
Container-level e2e (Docker, LangChain) arrives with the conformance suite (SEM-E3).
