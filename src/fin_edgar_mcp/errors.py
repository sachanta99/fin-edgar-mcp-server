"""Typed errors. Every failure a tool can return carries one of these codes."""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    NOT_FOUND = "not_found"
    INVALID_INPUT = "invalid_input"
    UPSTREAM_ERROR = "upstream_error"
    RATE_LIMITED = "rate_limited"
    SQL_REJECTED = "sql_rejected"
    CONFIG_MISSING = "config_missing"
    INTERNAL_ERROR = "internal_error"


class FinMCPError(Exception):
    """Base error. ``str(err)`` is a message the calling model can act on."""

    code: ErrorCode = ErrorCode.INTERNAL_ERROR

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_text(self) -> str:
        text = f"[{self.code.value}] {self.message}"
        if self.details:
            extras = "; ".join(f"{k}={v}" for k, v in self.details.items())
            text += f" ({extras})"
        return text


class NotFoundError(FinMCPError):
    code = ErrorCode.NOT_FOUND


class InvalidInputError(FinMCPError):
    code = ErrorCode.INVALID_INPUT


class UpstreamError(FinMCPError):
    code = ErrorCode.UPSTREAM_ERROR


class RateLimitedError(FinMCPError):
    code = ErrorCode.RATE_LIMITED


class SQLRejectedError(FinMCPError):
    code = ErrorCode.SQL_REJECTED


class ConfigMissingError(FinMCPError):
    code = ErrorCode.CONFIG_MISSING
