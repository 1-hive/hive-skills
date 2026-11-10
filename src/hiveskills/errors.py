# SPDX-License-Identifier: GPL-3.0-or-later
"""Refusal codes (SPEC.md §6) and exit codes."""

from __future__ import annotations

EXIT_OK = 0
EXIT_USAGE = 2      # bad invocation, or an invalid registry, entry or skill
EXIT_REFUSED = 3    # a refusal (duplicate, conflict, expired, unknown, R0 verification) or drift found
EXIT_INTERNAL = 5

_USAGE = {"INVALID_REGISTRY", "INVALID_ENTRY", "INVALID_SKILL", "EXPIRY_INVALID", "INVALID_PIN", "INVALID_PATH",
          "UNSUPPORTED_VERSION", "DESTINATION_EXISTS"}
CODES = frozenset({"INVALID_REGISTRY", "INVALID_ENTRY", "INVALID_SKILL", "DUPLICATE_SKILL", "UNKNOWN_SKILL",
                   "SKILL_EXPIRED", "SKILL_CONFLICT", "EXPIRY_INVALID"})


class SkillError(Exception):
    """A refusal with a stable code. R0's PinError codes pass through unchanged."""

    def __init__(self, code: str, message: str, **details: object) -> None:
        super().__init__(message)
        self.code, self.message, self.details = code, message, details

    @property
    def exit_code(self) -> int:
        if self.code == "INTERNAL_ERROR":
            return EXIT_INTERNAL
        return EXIT_USAGE if self.code in _USAGE else EXIT_REFUSED

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, **({"details": self.details} if self.details else {})}


def from_pin_error(e: Exception) -> SkillError:
    return SkillError(getattr(e, "code", "INTERNAL_ERROR"), str(getattr(e, "message", None) or e))
