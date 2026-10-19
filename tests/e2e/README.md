# e2e

Tests marked `e2e` run the real app over loopback and drive it with real client
libraries (OpenAI SDK, LangChain). They are kept out of the fast gate (they need the
`e2e` extra) and run as their own CI job: `make test-e2e`.

- `test_record_replay.py`: the walking-skeleton demo. Record through the CLI, restart in
  replay mode, replay with every non-loopback connection refused.
- `test_conformance.py`: each client records a call (a stand-in agent answers it) and
  replays it. Covers text, streaming with a usage chunk, tool calls, models, errors and
  the async client. A failure message names the client field that did not conform.
