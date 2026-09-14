"""Display boundary for a Runtime **input snapshot** in the Task Center.

AG-G2-AUTO-027.

The input snapshot is what the Task Center hands to the Runtime for an unattended
task: the task's own ``name`` / ``description`` / ``prompt`` / ``message`` /
``input`` / ``inputs``, restricted to non-identity fields (see
``task_runtime_context._INPUT_ALLOWED_KEYS``). It is execution input, and it is
not a display artefact:

- ``prompt`` and friends are the *whole* user-authored instruction, which may
  embed a file path, an internal endpoint or a pasted credential;
- a task row is client-writable, so the snapshot can carry whatever a caller put
  there — ``access_token``, a cookie header, a provider block, an attachment path;
- the stream and the existing ``execution_history`` list are read by every client
  that can see the task, so anything that reaches them is broadcast.

The rule this module implements is therefore the narrow one the domain already
uses elsewhere: project the **minimum** a user could already see, and drop the
rest — never truncate a secret, never partially mask it.

Reuse, not a parallel redactor
------------------------------
The leak shapes (paths, URLs, tracebacks, tokens, ``api_key``, ``password``,
``secret`` …) are already defined once, in ``task_runtime_failure``, and that
module's rule — "drop the whole value if it looks like a leak" — is exactly the
rule needed here. So a candidate value is put through the existing
:func:`sanitize_failure_message`, and if that refuses it, it is withheld. There is
no second pattern list to keep in sync, and a shape added there is honoured here
immediately.

What is projected when a value is fine
--------------------------------------
Only keys on :data:`DISPLAYABLE_INPUT_KEYS` — the fields the Task Center already
renders for the user. Everything else contributes at most a count
(``withheld_input_fields``), so a reader can tell "nothing else was sent" from
"something was withheld" without the withheld content, or even its key names,
being revealed. A key name alone would already disclose that a provider block or a
credential field exists.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from typing import Any

from app.gateway.task_runtime_failure import sanitize_failure_message

__all__ = [
    "DISPLAYABLE_INPUT_KEYS",
    "MAX_SUMMARY_TEXT",
    "NEVER_PROJECTED_INPUT_KEYS",
    "WITHHELD_COUNT_KEY",
    "input_snapshot_leaks",
    "is_sensitive_input_key",
    "sanitize_input_snapshot",
]

# Free text is collapsed to one line and truncated, so a summary can never carry
# a payload dump into the task history.
MAX_SUMMARY_TEXT = 2000

# The fields the Task Center already shows the user. A snapshot may only be
# summarised from these.
DISPLAYABLE_INPUT_KEYS = ("name", "description")

# Execution input. Never projected, not even truncated: a truncated prompt is
# still a prompt, and it is still the user's instruction rather than a display
# artefact.
NEVER_PROJECTED_INPUT_KEYS = ("prompt", "message", "input", "inputs")

# Reported as a count, never as names.
WITHHELD_COUNT_KEY = "withheld_input_fields"

# A key whose *name* says it holds a secret, a credential, a provider setting or
# a location. Matched on word parts, not substrings, so ``author`` is not mistaken
# for ``auth`` and ``ghost`` is not mistaken for ``host``.
_SENSITIVE_KEY_PARTS = frozenset(
    {
        "apikey",
        "asset",
        "assets",
        "attachment",
        "attachments",
        "auth",
        "authorization",
        "bucket",
        "config",
        "cookie",
        "cookies",
        "credential",
        "credentials",
        "dir",
        "directory",
        "dsn",
        "endpoint",
        "endpoints",
        "file",
        "files",
        "filename",
        "host",
        "hostname",
        "key",
        "keys",
        "path",
        "paths",
        "password",
        "passwd",
        "private",
        "provider",
        "secret",
        "secrets",
        "session",
        "signature",
        "token",
        "tokens",
        "uri",
        "url",
        "urls",
        "webhook",
    }
)

_KEY_SPLIT = re.compile(r"[^a-zA-Z0-9]+")


def _key_parts(key: Any) -> tuple[str, ...]:
    """A key name split into lowercase word parts, camelCase included."""
    name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", str(key or ""))
    return tuple(part.lower() for part in _KEY_SPLIT.split(name) if part)


def is_sensitive_input_key(key: Any) -> bool:
    """Whether a key's own name marks it as secret-bearing or internal.

    Public so the rule can be asserted directly, and so a future caller adding a
    key to the displayable set cannot quietly widen the boundary past it.
    """
    return any(part in _SENSITIVE_KEY_PARTS for part in _key_parts(key))


def _clean_displayable(value: Any) -> str | None:
    """A value safe to show under a displayable key, or ``None``.

    ``None`` covers a non-primitive, an empty value, and a value that looks like a
    leak — the caller does not need to tell them apart, because in all three cases
    the value must not be projected.
    """
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        return None
    text = " ".join(str(value).split())
    if not text:
        return None
    # The existing leak scanner, used as the predicate it already is: it refuses a
    # value carrying a path, a URL, a traceback, a token or a credential word.
    if sanitize_failure_message(value) is None:
        return None
    return text[:MAX_SUMMARY_TEXT]


def _is_displayable_key(key: str) -> bool:
    return key in DISPLAYABLE_INPUT_KEYS and not is_sensitive_input_key(key)


def sanitize_input_snapshot(payload: Any) -> dict[str, Any] | None:
    """The minimum, user-visible summary of a Runtime input snapshot.

    ``None`` means "nothing may be shown" — a normal outcome for a snapshot that
    is entirely execution input, not a failure. When anything *is* withheld, the
    summary says how many fields were, so a reader is never misled into thinking
    the snapshot was empty.
    """
    if not isinstance(payload, Mapping):
        return None

    summary: dict[str, Any] = {}
    withheld = 0
    for raw_key, value in payload.items():
        key = str(raw_key)
        if not _is_displayable_key(key):
            withheld += 1
            continue
        cleaned = _clean_displayable(value)
        if cleaned is None:
            withheld += 1
            continue
        summary[key] = cleaned

    if withheld:
        summary[WITHHELD_COUNT_KEY] = withheld
    return summary or None


def _iter_strings(value: Any, *, depth: int = 0) -> Iterator[Any]:
    """Every string reachable in a display value, at a bounded depth."""
    if depth > 6:
        return
    if isinstance(value, str):
        yield value
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield from _iter_strings(key, depth=depth + 1)
            yield from _iter_strings(item, depth=depth + 1)
        return
    if isinstance(value, list | tuple | set | frozenset):
        for item in value:
            yield from _iter_strings(item, depth=depth + 1)


def input_snapshot_leaks(payload: Any, display_value: Any) -> tuple[str, ...]:
    """Snapshot strings that must never appear in a display value.

    A cheap, explicit check rather than a proof: it reports the values the
    snapshot carries and the display value already contains verbatim. Used to
    assert the boundary in tests, and available to a debug assertion on a
    projection path that must not grow a snapshot field by accident.

    Only non-trivial strings are considered, so a snapshot field that happens to
    contain ``"running"`` does not produce a false positive.
    """
    if not isinstance(payload, Mapping):
        return ()

    leaked: list[str] = []
    for _, value in payload.items():
        texts = [value] if isinstance(value, str) else list(_iter_strings(value))
        for text in texts:
            probe = str(text).strip()
            if len(probe) < 8:
                continue
            for candidate in _iter_strings(display_value):
                if probe in candidate:
                    leaked.append(probe)
                    break
    return tuple(dict.fromkeys(leaked))
