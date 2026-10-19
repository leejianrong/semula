"""One error type that renders as an OpenAI-shaped error body."""

from __future__ import annotations

from typing import Any


class SemulaError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        param: str | None = None,
        err_type: str = "invalid_request_error",
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.param = param
        self.err_type = err_type

    def body(self) -> dict[str, Any]:
        return {
            "error": {
                "message": self.message,
                "type": self.err_type,
                "param": self.param,
                "code": self.code,
            }
        }
