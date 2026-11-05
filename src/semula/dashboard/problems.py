"""How a refusal reaches the screen (SEM-149): its own sentence, never its code.

The code is for logs and scripts and stays in the API's error body. On screen a refusal is the
service's ``message`` (what went wrong and how to fix it), drawn under the field it is about
through indah's ``error=`` when the field has one, and as an error-toned line otherwise."""

from __future__ import annotations

from collections.abc import Callable

import indah

from ..errors import SemulaError


def sentence(exc: SemulaError) -> str:
    return exc.message


def line(source: Callable[[], str]) -> indah.Component:
    """A refusal that belongs to no field, in the error tone (indah's ``Text`` has none yet)."""
    return indah.Badge(source, tone="err")


class Problem:
    """The one refusal a screen shows: its sentence and the field it is about, if any."""

    def __init__(self) -> None:
        self._now: indah.Signal[tuple[str, str]] = indah.Signal(("", ""))

    def clear(self) -> None:
        self._now.set(("", ""))

    def say(self, message: str, field: str = "") -> None:
        self._now.set((field, message))

    def refused(self, exc: SemulaError) -> None:
        self.say(sentence(exc), exc.param or "")

    def at(self, field: str) -> Callable[[], str]:
        """For a field's ``error=``: the sentence while it is about this field."""
        return lambda: self._now.value[1] if self._now.value[0] == field else ""

    def elsewhere(self, *fields: str) -> Callable[[], str]:
        """For a line below the form: the sentence when no field on screen shows it."""
        return lambda: self._now.value[1] if self._now.value[0] not in fields else ""
