"""Safe, displayable Runtime result summaries for Task Center projection.

AG-G2-AUTO-013.

The Runtime owns the real result. This module only derives the *minimum* summary a
user is allowed to see, because the raw Runtime result is not safe to hand to the
Task Center:

- it may embed provider objects that are not JSON, and copying them would either
  break the existing ``execution_history`` list or smuggle internals into it;
- it may carry credentials, endpoints, filesystem paths or the original prompt;
- it may carry a traceback that reveals internals.

So nothing is copied through. ``sanitize_result_summary`` builds a **new** dict
from a fixed allowlist of presentation-only keys, keeps only primitives, collapses
and truncates free text, and drops everything else. A key that is not on the list
never reaches the caller, so a new field appearing in a Runtime result cannot
leak by default.

The projection decides *when* a summary is attached and always records whether a
result was available, so a completed run with no result still has a stable,
explicit representation rather than an ambiguous absence.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = [
    "MAX_SUMMARY_ITEMS",
    "MAX_SUMMARY_TEXT",
    "RESULT_SUMMARY_KEYS",
    "sanitize_result_summary",
]

# Free text is collapsed to one line and truncated, so a summary can never carry
# a payload dump into the task history.
MAX_SUMMARY_TEXT = 2000
MAX_SUMMARY_ITEMS = 20

# Presentation-only keys. Anything absent from this mapping is dropped.
_TEXT_KEYS = {
    "summary": MAX_SUMMARY_TEXT,
    "text": MAX_SUMMARY_TEXT,
    "message": MAX_SUMMARY_TEXT,
    "title": 200,
    "output_text": MAX_SUMMARY_TEXT,
}
_SCALAR_KEYS = ("count", "total", "duration_ms", "status")
_LIST_KEYS = ("items", "highlights", "bullet_points")

RESULT_SUMMARY_KEYS = tuple(
    sorted({*_TEXT_KEYS, *_SCALAR_KEYS, *_LIST_KEYS})
)

_PRIMITIVES = (bool, int, float, str)


def _clean_text(value: Any, limit: int) -> str | None:
    text = " ".join(str(value).split())
    return text[:limit] or None


def _clean_scalar(value: Any) -> Any:
    if isinstance(value, bool) or isinstance(value, int | float | str):
        if isinstance(value, str):
            return _clean_text(value, 200)
        return value
    return None


def _clean_list(value: Any) -> list[Any] | None:
    if not isinstance(value, list | tuple):
        return None
    out: list[Any] = []
    for item in value:
        if not isinstance(item, _PRIMITIVES):
            continue
        cleaned = _clean_text(item, 400) if isinstance(item, str) else item
        if cleaned is None:
            continue
        out.append(cleaned)
        if len(out) >= MAX_SUMMARY_ITEMS:
            break
    return out or None


def sanitize_result_summary(result: Any) -> dict[str, Any] | None:
    """Derive the displayable summary of a Runtime result, or ``None``.

    ``None`` means "nothing was available that a user may see" — it is a normal,
    expected outcome, not a failure.
    """
    if not isinstance(result, Mapping):
        return None

    summary: dict[str, Any] = {}
    for key, limit in _TEXT_KEYS.items():
        if key not in result:
            continue
        raw = result.get(key)
        if not isinstance(raw, str):
            continue
        cleaned = _clean_text(raw, limit)
        if cleaned is not None:
            summary[key] = cleaned

    for key in _SCALAR_KEYS:
        if key in result:
            cleaned = _clean_scalar(result.get(key))
            if cleaned is not None:
                summary[key] = cleaned

    for key in _LIST_KEYS:
        if key in result:
            cleaned_list = _clean_list(result.get(key))
            if cleaned_list is not None:
                summary[key] = cleaned_list

    return summary or None
