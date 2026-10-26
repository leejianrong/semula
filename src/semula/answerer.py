"""The answerer hook: the operator's command (or webhook) answers parked calls (ADR-0014).

This runs a program on the operator's behalf with text an app wrote, so it trusts the operator's
configuration and nothing else. The command is an argv list from the server's environment, never
a shell line and never built from a request; the request goes to it on stdin as JSON; each call
gets a fresh process in a fresh empty directory with a stripped environment, a timeout, an output
cap and a concurrency cap. Whatever it returns is only an answer: it goes through
``answer_pending`` like an agent's, so nothing it says skips validation. A failure leaves the call
parked.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import signal
import tempfile
from pathlib import Path
from typing import Any

import httpx

from .config import Settings
from .errors import SemulaError
from .service import Service

log = logging.getLogger("semula.answerer")

WORKER = "answerer"
MAX_OUTPUT = 1_048_576  # bytes read from the child or the webhook; more is a failure
LEASE_MARGIN = 30.0  # seconds a claim outlasts the timeout, so a slow finish is still covered
LOOK_AGAIN = 2.0  # seconds between looks at the queue when no event says it moved
BASE_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "USER")
_FENCE = re.compile(r"^```[a-zA-Z]*\n(.*)\n```$", re.DOTALL)


class Failed(Exception):
    """The command or hook gave no usable output. The message is safe to log: it names a reason
    and an exit status, never the prompt or anything the child said."""


def parse_output(raw: bytes) -> Any:
    """What the child printed, as an answer payload for ``answer_pending``: JSON if it is a JSON
    object or array (a reply wrapped in one code fence is unwrapped first), else the text."""
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        raise Failed("no output")
    fenced = _FENCE.match(text)
    body = fenced.group(1).strip() if fenced else text
    if body[:1] in ("{", "["):
        try:
            return json.loads(body)
        except ValueError as exc:
            raise Rejected(
                {"code": "invalid_json", "message": f"The output is not valid JSON: {exc}"}
            ) from exc
    return {"content": text}


class Rejected(Exception):
    """The output cannot be used as an answer; the reason goes back to the answerer once."""

    def __init__(self, reason: dict[str, Any]) -> None:
        super().__init__(reason["message"])
        self.reason = reason


class Answerer:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self._command = settings.answerer_command
        self._url = settings.answerer_url
        self._bearer = settings.answerer_bearer
        self._timeout = settings.answerer_timeout
        self._passthrough = settings.answerer_env
        self._limit = asyncio.Semaphore(settings.answerer_concurrency)
        self._transport = transport
        self._tried: set[str] = set()  # one try per call per process lifetime: no retry loops
        self._tasks: set[asyncio.Task[None]] = set()

    # --- the loop ---------------------------------------------------------------------------
    async def run(self, service: Service, stop: asyncio.Event) -> None:
        """Look at the queue whenever it moves (and every few seconds), start one task for each
        call not yet tried. Returns when ``stop`` is set, after killing what is still running."""
        with service.events.subscribe() as sub:
            try:
                while not stop.is_set():
                    try:
                        await self._pass(service)
                    except Exception:
                        log.exception("The answerer could not read the queue.")
                    await self._rest(sub, stop)
            finally:
                for task in list(self._tasks):
                    task.cancel()
                await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _rest(self, sub: Any, stop: asyncio.Event) -> None:
        waiting = {
            asyncio.ensure_future(sub.get()),
            asyncio.ensure_future(stop.wait()),
        }
        _, rest = await asyncio.wait(
            waiting, timeout=LOOK_AGAIN, return_when=asyncio.FIRST_COMPLETED
        )
        for future in rest:
            future.cancel()

    async def _pass(self, service: Service) -> None:
        projects = await asyncio.to_thread(self._enabled, service)
        for project in projects:
            for item in await asyncio.to_thread(service.list_pending, "pending", project):
                if item["id"] in self._tried or item["claimed_until"]:
                    continue
                self._tried.add(item["id"])
                task = asyncio.create_task(self._handle(service, item["id"]))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)

    @staticmethod
    def _enabled(service: Service) -> list[str]:
        return [
            p["id"] for p in service.list_projects() if service.project_settings(p["id"]).answerer
        ]

    # --- one call ---------------------------------------------------------------------------
    async def _handle(self, service: Service, pending_id: str) -> None:
        async with self._limit:
            lease = self._timeout * 2 + LEASE_MARGIN  # a second try is allowed
            if not await asyncio.to_thread(service.claim_pending, pending_id, lease, WORKER):
                return  # an agent or a person has it, or it is gone
            try:
                await self._answer(service, pending_id)
            except asyncio.CancelledError:
                raise
            except Failed as why:
                log.warning("answerer left %s parked: %s", pending_id, why)
            except Exception:
                log.exception("answerer failed on %s", pending_id)
            finally:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(service.release_claim, pending_id, WORKER)

    async def _answer(self, service: Service, pending_id: str) -> None:
        view = await asyncio.to_thread(service.get_pending, pending_id)
        payload: dict[str, Any] = {
            "request": view["request"],
            "shape": view["shape"],
            "project": view["project"],
            "pending_id": pending_id,
            "attempt": 1,
        }
        rejection: dict[str, Any] | None = None
        previous: bytes | None = None
        for attempt in (1, 2):
            payload["attempt"] = attempt
            if rejection is not None:
                payload["rejection"] = rejection
                payload["previous_output"] = (previous or b"").decode("utf-8", "replace")[:4000]
            output = await self.invoke(payload)
            previous = output
            try:
                answer = parse_output(output)
                await asyncio.to_thread(service.answer_pending, pending_id, answer)
                return
            except Rejected as why:
                rejection = why.reason
            except SemulaError as why:
                if why.status != 422:
                    raise Failed(f"the answer was refused ({why.code})") from why
                rejection = {"code": why.code, "message": why.message, "param": why.param}
        raise Failed(f"the answer was rejected twice ({(rejection or {}).get('code')})")

    # --- running it -------------------------------------------------------------------------
    async def invoke(self, payload: dict[str, Any]) -> bytes:
        """One run: a fresh process (or one POST) for ``payload``. Returns what it printed."""
        data = json.dumps(payload, ensure_ascii=False).encode()
        if self._url:
            return await self._post(self._url, data)
        return await self._spawn(data)

    def _environment(self) -> dict[str, str]:
        names = (*BASE_ENV, *self._passthrough)
        return {k: os.environ[k] for k in names if k in os.environ and not k.startswith("SEMULA_")}

    async def _spawn(self, data: bytes) -> bytes:
        workdir = tempfile.mkdtemp(prefix="semula-answerer-")
        proc: asyncio.subprocess.Process | None = None
        try:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *self._command,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,  # a child may echo the prompt: drop it
                    cwd=workdir,
                    env=self._environment(),
                    start_new_session=True,  # its own group, so a timeout kills the whole tree
                )
            except (FileNotFoundError, PermissionError, OSError) as exc:
                raise Failed(
                    f"could not start {Path(self._command[0]).name}: {type(exc).__name__}"
                ) from exc
            try:
                out = await asyncio.wait_for(self._talk(proc, data), self._timeout)
            except TimeoutError as exc:
                raise Failed(f"timed out after {self._timeout:g}s") from exc
            code = await proc.wait()
            if code != 0:
                raise Failed(f"exited with status {code}")
            return out
        finally:
            if proc is not None and proc.returncode is None:
                self._kill(proc)
                with contextlib.suppress(Exception):
                    await proc.wait()
            shutil.rmtree(workdir, ignore_errors=True)

    @staticmethod
    def _kill(proc: asyncio.subprocess.Process) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)

    async def _talk(self, proc: asyncio.subprocess.Process, data: bytes) -> bytes:
        stdin, stdout = proc.stdin, proc.stdout
        if stdin is None or stdout is None:
            raise Failed("no pipes to the process")

        async def feed() -> None:
            try:
                stdin.write(data)
                await stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass  # it stopped reading; its exit status says what happened
            finally:
                with contextlib.suppress(Exception):
                    stdin.close()

        async def read() -> bytes:
            chunks: list[bytes] = []
            size = 0
            while chunk := await stdout.read(65536):
                size += len(chunk)
                if size > MAX_OUTPUT:
                    self._kill(proc)
                    raise Failed(f"more than {MAX_OUTPUT} bytes of output")
                chunks.append(chunk)
            return b"".join(chunks)

        _, out = await asyncio.gather(feed(), read())
        return out

    async def _post(self, url: str, data: bytes) -> bytes:
        headers = {"Content-Type": "application/json"}
        if self._bearer:
            headers["Authorization"] = f"Bearer {self._bearer}"
        try:
            async with (
                httpx.AsyncClient(
                    timeout=self._timeout, follow_redirects=False, transport=self._transport
                ) as client,
                client.stream("POST", url, content=data, headers=headers) as resp,
            ):
                if resp.status_code != 200:
                    raise Failed(f"the hook answered status {resp.status_code}")
                chunks: list[bytes] = []
                size = 0
                async for chunk in resp.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_OUTPUT:
                        raise Failed(f"more than {MAX_OUTPUT} bytes of output")
                    chunks.append(chunk)
                return b"".join(chunks)
        except httpx.HTTPError as exc:  # by type only: the URL may carry a token
            raise Failed(f"the hook was unreachable: {type(exc).__name__}") from exc
