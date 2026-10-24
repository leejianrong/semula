# Demo assets

- `semula-dashboard-demo.mp4` (not in git: it is built by `make demo-video` and ignored): a 21 s walkthrough of the local dashboard (1280x800, about 2 MB). Embed it by dragging the file into a GitHub README or issue editor, which gives a `githubusercontent.com` URL.
- `dashboard-log.png`, `dashboard-pending.png`: screenshots used by the docs.

## The video

A call is held, answered once, then replayed for free:

| Time | Beat | Sound |
|---|---|---|
| 0-4 s | Empty live log, Record on. "Your app calls an LLM..." | |
| 4-8 s | The call arrives and is held: Parked and the Pending badge go to 1. "semula holds it for you to answer" | pop, click |
| 8-12 s | Pending: select the call, type the answer, Send answer | success chime |
| 12-15 s | Entries shows it recorded. "Recorded once" | whoosh |
| 15-21 s | Live log: the same prompt is a Hit (Exact, 2 ms). "...replayed for free, every time" | click, pop |

Everything on screen is invented fixture data. Re-record after a dashboard change with:

```bash
make demo-video
```

It runs `scripts/video/record_dashboard_demo.py`: a throwaway server, the real dashboard driven by Playwright in Chromium, then music and sound effects mixed in by the `video-demo` skill's `mux_audio.py` (set `VIDEO_DEMO_MUX` if the skill is somewhere else). It needs ffmpeg and a Chromium that Playwright can find (`npx playwright install chromium`). Playwright is brought in by `uv run --with`, not added to the project.
