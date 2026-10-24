"""Record the local dashboard demo video: a call is held, answered once, then replayed free.

    make demo-video            writes docs/assets/semula-dashboard-demo.mp4

Starts a throwaway server (fixture data only, nothing real), drives the real dashboard in
Chromium with Playwright, logs when each beat happens, then mixes music and sound effects with
the video-demo skill's ``mux_audio.py`` (set VIDEO_DEMO_MUX to its path if it lives elsewhere).
Playwright is not a project dependency: ``make demo-video`` brings it with ``uv run --with``.
The shot list is in ``docs/assets/README.md``.
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from semula.config import Settings  # noqa: E402
from semula.db import Database  # noqa: E402
from semula.service import Service  # noqa: E402
from tests.support.db import fresh_database_url  # noqa: E402

FINAL = ROOT / "docs/assets/semula-dashboard-demo.mp4"
MUX = Path(
    os.environ.get("VIDEO_DEMO_MUX", Path.home() / ".claude/skills/video-demo/scripts/mux_audio.py")
)
MUSIC = "minimal-clean"
LEAD = 0.5  # seconds the recording starts before the script's clock (the page opens a moment later)
SIZE = {"width": 1280, "height": 800}
PROMPT = "Classify this support ticket: 'I was charged twice this month.'"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def start_server(work: Path, port: int) -> subprocess.Popen[bytes]:
    """A record-mode server with one older entry, so the Entries screen is not bare."""
    data = work / "data"
    data.mkdir()
    db_url = fresh_database_url(data)
    Service(Database(db_url), Settings(data_dir=data)).upsert_entry(
        "default",
        {"messages": [{"role": "user", "content": "Summarise this changelog in one line"}]},
        {"content": "Adds hosted mode, sign-in and an admin dashboard."},
    )
    env = {
        **os.environ,
        "SEMULA_DATA_DIR": str(data),
        "SEMULA_DATABASE_URL": db_url,
        "SEMULA_HOST": "127.0.0.1",
        "SEMULA_PORT": str(port),
        "SEMULA_MODE": "record",
    }
    server = subprocess.Popen(  # noqa: S603
        [sys.executable, "-m", "semula", "serve"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(100):
        try:
            httpx.get(f"http://127.0.0.1:{port}/health", timeout=0.3)
            return server
        except httpx.HTTPError:
            time.sleep(0.1)
    server.terminate()
    raise RuntimeError("the demo server did not start")


def app_call(url: str) -> httpx.Response:
    """What the demo's 'app' sends: one chat request, which a record-mode server holds."""
    return httpx.post(
        url + "/v1/chat/completions",
        timeout=60,
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": PROMPT}]},
    )


def capture(url: str, work: Path) -> tuple[Path, list[dict[str, object]], list[dict[str, object]]]:
    """Drive the dashboard and return the silent video, the sound cues and the captions."""
    cues: list[dict[str, object]] = []
    caps: list[dict[str, object]] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(
            viewport=SIZE, record_video_dir=str(work / "raw"), record_video_size=SIZE
        )
        page = ctx.new_page()
        t0 = time.time()

        def now() -> float:
            return round(time.time() - t0, 2)

        def cue(name: str) -> None:
            cues.append({"sfx": name, "t": now()})

        def caption(text: str, seconds: float) -> None:
            caps.append({"text": text, "start": now(), "end": round(now() + seconds, 2)})

        page.goto(url + "/ui/projects/default/log")
        page.get_by_text("live", exact=True).wait_for(timeout=15000)
        page.wait_for_timeout(1200)

        caption("Your app calls an LLM...", 2.2)  # 1. an empty log, Record on
        page.wait_for_timeout(2200)

        held = threading.Thread(target=app_call, args=(url,))  # 2. the call arrives and is held
        held.start()
        page.locator("text=PARKED").locator("xpath=..").get_by_text("1", exact=True).wait_for(
            timeout=10000
        )
        cue("pop")
        caption("semula holds it for you to answer", 2.8)
        page.wait_for_timeout(1800)
        page.get_by_role("link", name="Pending").click()
        cue("click")
        page.wait_for_timeout(900)

        page.get_by_text("Classify this support ticket").first.click()  # 3. answer it
        page.wait_for_timeout(900)
        page.locator(".cm-content").first.click()
        page.keyboard.type("billing / duplicate-charge", delay=45)
        page.wait_for_timeout(600)
        page.get_by_role("button", name="Send answer").click()
        cue("success-chime")
        held.join(timeout=10)
        page.wait_for_timeout(1500)

        page.get_by_role("link", name="Entries").click()  # 4. it is now recorded
        cue("whoosh")
        caption("Recorded once", 2.2)
        page.get_by_text("Classify this support ticket").first.wait_for(timeout=8000)
        page.wait_for_timeout(2200)

        page.get_by_role("link", name="Live log").click()  # 5. the same prompt again is a hit
        cue("click")
        page.wait_for_timeout(1600)
        app_call(url)
        page.get_by_text("Hit", exact=True).first.wait_for(timeout=10000)
        cue("pop")
        caption("...replayed for free, every time", 3.4)
        page.wait_for_timeout(3600)

        video = Path(page.video.path()) if page.video else None
        ctx.close()
        browser.close()
    if video is None:
        raise RuntimeError("no video was recorded")
    silent = work / "demo.webm"
    video.rename(silent)
    return silent, cues, caps


def main() -> None:
    work = Path(tempfile.mkdtemp(prefix="semula-demo-"))
    port = free_port()
    server = start_server(work, port)
    try:
        silent, cues, caps = capture(f"http://127.0.0.1:{port}", work)
    finally:
        server.terminate()
    for c in cues:
        c["t"] = round(float(str(c["t"])) + LEAD, 2)
    for c in caps:
        c["start"] = round(float(str(c["start"])) + LEAD, 2)
        c["end"] = round(float(str(c["end"])) + LEAD, 2)
    (work / "cues.json").write_text(json.dumps(cues, indent=1))
    (work / "captions.json").write_text(json.dumps(caps, indent=1))
    FINAL.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(MUX),
            *("--video", str(silent), "--music", MUSIC),
            *("--cues", str(work / "cues.json"), "--captions", str(work / "captions.json")),
            *("--out", str(FINAL)),
        ],
        check=True,
    )


if __name__ == "__main__":
    main()
