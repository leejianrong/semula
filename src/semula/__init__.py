"""semula: a mock LLM server powered by your AI subscription."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("semula")  # one source of truth: ``version`` in pyproject.toml
except PackageNotFoundError:  # run from a bare source tree, not installed
    __version__ = "0.0.0+unknown"
