"""Redaction of Runtime failure detail before it reaches the Task Center.

AG-G2-AUTO-014.

A Runtime failure is described by a public error code plus a human message. Both
travel into the existing ``execution_history`` and from there into the existing
task stream, so both are untrusted display text: the message may have been built
from a provider exception and can therefore carry a traceback, an internal
endpoint, a filesystem path, a credential, or the original prompt.

The rule here is deliberately blunt:

- the **code** is reduced to the characters a Runtime error code can contain, so
  nothing structured or secret can ride along in it;
- the **message** is only kept when it looks like a plain sentence. If it matches
  a known leak shape the whole message is dropped rather than truncated or
  partially masked — a partially redacted message is still a leak, and a
  half-masked secret is harder to notice than a missing one;
- a dropped message is reported, never silently swallowed, so a reader can tell
  "no detail was given" from "the detail was withheld".

Nothing here invents a new status or a new vocabulary: a failure stays a Runtime
failure, it simply arrives redacted.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = [
    "MAX_FAILURE_MESSAGE",
    "sanitize_error_code",
    "sanitize_failure_detail",
    "sanitize_failure_message",
]

# Kept short on purpose: a real error message is a sentence, not a report.
MAX_FAILURE_MESSAGE = 240

# What a Runtime error code may contain. Anything else is not part of a code.
_ERROR_CODE_ALLOWED = re.compile(r"[^A-Za-z0-9._-]")
_ERROR_CODE_SHAPE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
MAX_ERROR_CODE = 64

# Leak shapes. Each is specific enough to avoid dropping ordinary sentences.
_UNSAFE_PATTERNS = (
    re.compile(r"traceback"),
    re.compile(r"file \""),
    re.compile(r"line \d+"),
    re.compile(r"://"),
    re.compile(r"www\."),
    re.compile(r"(?:^|[\s(\[])/[a-z0-9._-]*/"),
    re.compile(r"/(?:var|etc|home|usr|users|tmp|opt|root|private|applications)/"),
    re.compile(r"[a-z]:\\"),
    re.compile(r"github_pat_|ghp_|sk-|sk_live_|xox[baprs]-"),
    re.compile(r"api[_-]?key"),
    re.compile(r"access[_-]?token|refresh[_-]?token|bearer\s|authorization:"),
    re.compile(r"password|passwd|secret"),
)


def sanitize_error_code(value: Any) -> str | None:
    """A Runtime error code, reduced to a code-shaped string, or ``None``.

    Only a string is accepted: rendering some other object would turn a stray
    structure into a plausible-looking code.
    """
    if not isinstance(value, str):
        return None
    code = _ERROR_CODE_ALLOWED.sub("", value.strip())
    if not _ERROR_CODE_SHAPE.fullmatch(code):
        return None
    return code[:MAX_ERROR_CODE]


def _looks_like_a_leak(text: str) -> bool:
    lowered = text.lower()
    return any(pattern.search(lowered) for pattern in _UNSAFE_PATTERNS)


def sanitize_failure_message(value: Any) -> str | None:
    """A failure message that is safe to show, or ``None`` if it is not.

    ``None`` means "nothing was shown", which covers both an absent message and a
    withheld one; use :func:`sanitize_failure_detail` when the difference matters.
    """
    return sanitize_failure_detail(value)[0]


def sanitize_failure_detail(value: Any) -> tuple[str | None, bool]:
    """Return ``(safe message or None, was an unsafe detail withheld)``."""
    if value is None:
        return None, False
    text = " ".join(str(value).split())
    if not text:
        return None, False
    if _looks_like_a_leak(text):
        return None, True
    return text[:MAX_FAILURE_MESSAGE], False
