"""A deterministic stand-in for the embedding model, so the fast layer needs no download.

Words are hashed into a small vector, and a synonym table maps different words to the same
(heavier) slot, so "the screen is cracked" and "the display is shattered" come out close even
though they share almost no words. Unit length, like the real model's output.
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
from collections.abc import Sequence

import numpy as np

SYNONYMS = {
    "cracked": "broken", "shattered": "broken", "smashed": "broken",
    "screen": "display", "monitor": "display",
    "refund": "money", "reimburse": "money", "repay": "money",
    "arrived": "came", "delivered": "came",
}  # fmt: skip
DIM = 64


class FakeEmbedder:
    model_id = "fake-synonyms"

    def __init__(
        self, *, delay: float = 0.0, fail: bool = False, gate: threading.Event | None = None
    ) -> None:
        self.delay = delay
        self.fail = fail
        self.gate = gate  # when given, embedding waits here until the test sets it
        self.seen: list[str] = []  # every text it was asked to embed

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        if self.fail:
            raise RuntimeError("the model fell over")
        if self.gate is not None:
            self.gate.wait(30)
        time.sleep(self.delay)
        self.seen.extend(texts)
        out = np.zeros((len(texts), DIM), dtype=np.float32)
        for row, text in enumerate(texts):
            for word in re.findall(r"\w+", text.lower()):
                weight = 3.0 if word in SYNONYMS else 1.0  # the meaning-bearing words dominate
                word = SYNONYMS.get(word, word)
                slot = int(hashlib.md5(word.encode()).hexdigest(), 16) % DIM  # noqa: S324
                out[row, slot] += weight
        norms = np.linalg.norm(out, axis=1, keepdims=True)
        return np.asarray(out / np.where(norms == 0, 1, norms), dtype=np.float32)
