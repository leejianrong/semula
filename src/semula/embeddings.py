"""Optional semantic matching (SEM-E8, ADR-0003).

Everything that needs numpy or fastembed lives here and is imported lazily, so the default
install and image never load them. An ``Embedder`` turns text into unit-length vectors; the
``VectorStore`` keeps one vector per entry in the database (portable bytes, so SQLite and
Postgres both work) and answers "which entries look closest to this text" with a cosine search
over an in-memory matrix.

Embedding is slow next to everything else semula does (tens of milliseconds for a sentence on a
CPU), so it never sits on the hit path: vectors are made by a background worker after an entry
is written, and a query is embedded only when every cheaper tier has already missed.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, Protocol, cast

import sqlalchemy as sa

from .db import Database
from .schema import entry_vector as V

if TYPE_CHECKING:
    import numpy as np

log = logging.getLogger("semula.embeddings")

MODEL_ID = "BAAI/bge-small-en-v1.5"
MAX_TOKENS = 128  # the last message, truncated: a sentence or two is what paraphrases share
INSTALL_HINT = (
    "Install the extra: pip install 'semula[embeddings]' (or uv sync --extra embeddings)."
)


class EmbeddingsUnavailable(RuntimeError):
    """Embeddings were asked for but cannot run (extra missing, or model not downloaded)."""


class Embedder(Protocol):
    model_id: str

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        """Unit-length float32 vectors, one row per text."""
        ...


class Neighbour(NamedTuple):
    entry_id: str
    score: float  # cosine similarity, -1 to 1


class FastEmbedder:
    """fastembed with bge-small-en-v1.5 (Apache-2.0 / MIT, runs offline once downloaded)."""

    def __init__(self, cache_dir: Path, *, offline: bool = False, model_id: str = MODEL_ID) -> None:
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:
            raise EmbeddingsUnavailable(f"fastembed is not installed. {INSTALL_HINT}") from exc
        self.model_id = model_id
        cache_dir.mkdir(parents=True, exist_ok=True)
        try:  # the cache first, so a start with no network works once the model is downloaded
            with _quiet():  # a cache miss is normal on the first run, not an error to print
                self._model = TextEmbedding(
                    model_id, cache_dir=str(cache_dir), local_files_only=True
                )
        except Exception as cached:
            if offline:
                raise EmbeddingsUnavailable(
                    f"The model {model_id} is not in {cache_dir} and SEMULA_EMBEDDINGS_OFFLINE is "
                    "set. Run `semula embeddings fetch` once with network access."
                ) from cached
            log.info("Downloading %s to %s (once)...", model_id, cache_dir)
            try:
                self._model = TextEmbedding(model_id, cache_dir=str(cache_dir))
            except Exception as exc:
                raise EmbeddingsUnavailable(
                    f"Could not download {model_id}: {exc}. Check the network, or set "
                    "SEMULA_EMBEDDINGS=off."
                ) from exc
        cast(Any, self._model).model.tokenizer.enable_truncation(max_length=MAX_TOKENS)

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        import numpy as np

        vectors = np.asarray(list(self._model.embed(list(texts))), dtype=np.float32)
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        return np.asarray(vectors / np.where(norms == 0, 1, norms), dtype=np.float32)


@contextmanager
def _quiet() -> Iterator[None]:
    """fastembed logs a cache miss as an ERROR through loguru; mute it while we probe."""
    try:
        from loguru import logger
    except ImportError:
        yield
        return
    logger.disable("fastembed")
    try:
        yield
    finally:
        logger.enable("fastembed")


def load_embedder(model_dir: Path, *, offline: bool) -> Embedder:
    return FastEmbedder(model_dir, offline=offline)


def to_bytes(vector: np.ndarray) -> bytes:
    return bytes(vector.astype("float32").tobytes())


def from_bytes(blob: bytes) -> np.ndarray:
    import numpy as np

    return np.frombuffer(blob, dtype=np.float32)


class VectorStore:
    """Vectors in the database plus a per-project matrix cache for the cosine search."""

    def __init__(self, db: Database, embedder: Embedder) -> None:
        self.db = db
        self.embedder = embedder
        self._cache: dict[tuple[str, str], tuple[Any, list[str], Any]] = {}
        self._lock = threading.Lock()

    def put(self, entry_id: str, workspace_id: str, project_id: str, vector: np.ndarray) -> None:
        with self.db.begin() as conn:
            self.db.execute(
                sa.delete(V).where(V.c.workspace_id == workspace_id, V.c.entry_id == entry_id),
                conn,
            )
            self.db.execute(
                sa.insert(V).values(
                    entry_id=entry_id,
                    workspace_id=workspace_id,
                    project_id=project_id,
                    model=self.embedder.model_id,
                    vector=to_bytes(vector),
                ),
                conn,
            )

    def clear_project(self, workspace_id: str, project_id: str) -> None:
        self.db.execute(
            sa.delete(V).where(V.c.workspace_id == workspace_id, V.c.project_id == project_id)
        )

    def indexed(self, workspace_id: str) -> set[str]:
        rows = self.db.rows(
            sa.select(V.c.entry_id).where(
                V.c.workspace_id == workspace_id, V.c.model == self.embedder.model_id
            )
        )
        return {str(r["entry_id"]) for r in rows}

    def count(self, workspace_id: str) -> int:
        return len(self.indexed(workspace_id))

    def search(
        self, workspace_id: str, project_id: str, query: np.ndarray, limit: int
    ) -> list[Neighbour]:
        import numpy as np

        ids, matrix = self._matrix(workspace_id, project_id)
        if not ids:
            return []
        scores = matrix @ query
        top = np.argsort(-scores)[:limit]
        return [Neighbour(ids[i], float(scores[i])) for i in top]

    def _matrix(self, workspace_id: str, project_id: str) -> tuple[list[str], Any]:
        """The project's vectors as a matrix, rebuilt only when the stored set changed."""
        import numpy as np

        mine = (
            V.c.workspace_id == workspace_id,
            V.c.project_id == project_id,
            V.c.model == self.embedder.model_id,
        )
        stamp_row = self.db.one(
            sa.select(sa.func.count().label("n"), sa.func.max(V.c.created_at).label("latest"))
            .select_from(V)
            .where(*mine)
        )
        stamp = (stamp_row["n"], stamp_row["latest"]) if stamp_row else (0, None)
        key = (workspace_id, project_id)
        with self._lock:
            cached = self._cache.get(key)
            if cached and cached[0] == stamp:
                return cached[1], cached[2]
        rows = self.db.rows(sa.select(V.c.entry_id, V.c.vector).where(*mine).order_by(V.c.entry_id))
        ids = [str(r["entry_id"]) for r in rows]
        matrix = (
            np.vstack([from_bytes(r["vector"]) for r in rows])
            if rows
            else np.zeros((0, 0), dtype=np.float32)
        )
        with self._lock:
            self._cache[key] = (stamp, ids, matrix)
        return ids, matrix


class Indexer:
    """Makes vectors in the background so writing an entry never waits on the model.

    ``schedule`` is cheap and safe from any thread. ``flush`` blocks until the queue is empty
    (tests and the CLI use it). An entry not yet visible (its transaction is still open) is
    retried a few times before being left for the startup sync to find.
    """

    BATCH = 32
    SETTLE = 0.2  # seconds to let the writing transaction commit before reading the entry

    def __init__(
        self,
        store: VectorStore,
        load: Callable[[list[str]], list[tuple[str, str, str, str]]],
        workspace_id: str,
    ) -> None:
        self.store = store
        self._load = load  # ids -> [(entry_id, workspace_id, project_id, text)]
        self._queue: queue.Queue[tuple[str, int]] = queue.Queue()
        self._busy = threading.Event()
        self._workspace = workspace_id
        threading.Thread(target=self._run, name="semula-embed", daemon=True).start()

    def schedule(self, entry_id: str, attempt: int = 0) -> None:
        self._queue.put((entry_id, attempt))

    def flush(self, timeout: float = 60.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._queue.empty() and not self._busy.is_set():
                return True
            time.sleep(0.01)
        return False

    @property
    def backlog(self) -> int:
        return self._queue.qsize()

    def _run(self) -> None:
        while True:
            first = self._queue.get()
            self._busy.set()
            try:
                batch = [first]
                while len(batch) < self.BATCH:
                    try:
                        batch.append(self._queue.get_nowait())
                    except queue.Empty:
                        break
                time.sleep(self.SETTLE)
                self._process(batch)
            except Exception:
                log.exception("Embedding a batch failed; it will be retried at the next start.")
            finally:
                self._busy.clear()

    def _process(self, batch: list[tuple[str, int]]) -> None:
        attempts = dict(batch)
        found = self._load(list(attempts))
        seen = {row[0] for row in found}
        for entry_id, attempt in attempts.items():
            if entry_id not in seen and attempt < 3:
                self.schedule(entry_id, attempt + 1)
        if not found:
            return
        vectors = self.store.embedder.embed([row[3] for row in found])
        for (entry_id, workspace_id, project_id, _), vector in zip(found, vectors, strict=True):
            self.store.put(entry_id, workspace_id, project_id, vector)
