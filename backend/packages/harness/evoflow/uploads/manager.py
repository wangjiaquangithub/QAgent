"""Safe storage helpers for files uploaded to a conversation thread.

The gateway and embedded client share this module so uploads have one path
layout, response shape, and path-traversal policy.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from urllib.parse import quote

from evoflow.config.paths import VIRTUAL_PATH_PREFIX, get_paths


class PathTraversalError(ValueError):
    """Raised when an upload operation would access an unsafe path."""


def normalize_filename(filename: str) -> str:
    """Validate and return a single upload filename.

    Upload filenames are deliberately flat.  Rejecting rather than stripping
    directory components prevents a caller from silently writing a different
    file than the one it requested.
    """
    if not isinstance(filename, str):
        raise ValueError("Filename must be a string")
    if not filename or not filename.strip():
        raise ValueError("Filename must not be empty")
    if filename in {".", ".."}:
        raise ValueError("Filename must not be '.' or '..'")
    if "\x00" in filename:
        raise ValueError("Filename must not contain NUL bytes")
    if "/" in filename or "\\" in filename:
        raise ValueError("Filename must not contain path separators")
    # This protects platforms where a drive-qualified path does not contain a
    # slash (for example ``C:report.txt`` on Windows).
    if Path(filename).is_absolute() or (len(filename) >= 2 and filename[0].isalpha() and filename[1] == ":"):
        raise ValueError("Filename must not be an absolute path")
    return filename


def claim_unique_filename(filename: str, seen_names: set[str]) -> str:
    """Return a non-conflicting filename, adding `` (N)`` before the suffix."""
    filename = normalize_filename(filename)
    if filename not in seen_names:
        seen_names.add(filename)
        return filename

    path = Path(filename)
    index = 1
    while True:
        candidate = f"{path.stem} ({index}){path.suffix}"
        if candidate not in seen_names:
            seen_names.add(candidate)
            return candidate
        index += 1


def get_uploads_dir(thread_id: str) -> Path:
    """Return the host path for a thread's upload directory without creating it."""
    return get_paths().sandbox_uploads_dir(thread_id)


def ensure_uploads_dir(thread_id: str) -> Path:
    """Create and return a thread's upload directory."""
    uploads_dir = get_uploads_dir(thread_id)
    uploads_dir.mkdir(parents=True, exist_ok=True)
    # Sandbox providers can run under a different UID from the gateway.
    uploads_dir.chmod(0o777)
    return uploads_dir


def _is_regular_file(path: Path) -> bool:
    """Return whether *path* is a regular file without following symlinks."""
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except FileNotFoundError:
        return False


def list_files_in_dir(directory: Path) -> dict[str, Any]:
    """List regular, non-symlinked files in *directory* in stable order."""
    files: list[dict[str, str]] = []
    try:
        entries = directory.iterdir()
        for path in entries:
            if not _is_regular_file(path):
                continue
            try:
                size = path.stat(follow_symlinks=False).st_size
            except FileNotFoundError:
                # A concurrent delete is harmless; omit the vanished entry.
                continue
            files.append({"filename": path.name, "size": str(size)})
    except FileNotFoundError:
        pass

    files.sort(key=lambda item: item["filename"].casefold())
    return {"files": files, "count": len(files)}


def upload_virtual_path(filename: str) -> str:
    """Return the sandbox-visible virtual path for an uploaded file."""
    return f"{VIRTUAL_PATH_PREFIX}/uploads/{normalize_filename(filename)}"


def upload_artifact_url(thread_id: str, filename: str) -> str:
    """Return the gateway artifact URL for a thread upload."""
    # Reuse Paths' thread-id validation so callers get the same policy as all
    # other per-thread storage APIs.
    get_paths().thread_dir(thread_id)
    return (
        f"/api/threads/{thread_id}/artifacts/"
        f"mnt/user-data/uploads/{quote(normalize_filename(filename), safe='')}"
    )


def enrich_file_listing(result: dict[str, Any], thread_id: str) -> dict[str, Any]:
    """Add virtual and artifact URLs to a ``list_files_in_dir`` result in place."""
    for file_info in result.get("files", []):
        filename = normalize_filename(str(file_info["filename"]))
        file_info["virtual_path"] = upload_virtual_path(filename)
        file_info["artifact_url"] = upload_artifact_url(thread_id, filename)
    return result


def _safe_file_for_deletion(directory: Path, filename: str) -> Path:
    """Validate an upload filename and return its direct child path."""
    try:
        safe_filename = normalize_filename(filename)
    except ValueError as exc:
        raise PathTraversalError("Invalid upload filename") from exc

    file_path = directory / safe_filename
    # The normalized filename contains no separators, but retain a defense in
    # depth check for callers that supply an unusual Path implementation.
    try:
        file_path.relative_to(directory)
    except ValueError as exc:
        raise PathTraversalError("Invalid upload path") from exc
    return file_path


def delete_file_safe(
    directory: Path,
    filename: str,
    *,
    convertible_extensions: Iterable[str] = (),
) -> dict[str, str | bool]:
    """Delete one regular upload and its generated Markdown companion.

    The function never follows symbolic links.  Unsafe filenames and symlink
    targets raise :class:`PathTraversalError`; a missing file raises
    :class:`FileNotFoundError` for the API layer to map to ``404``.
    """
    file_path = _safe_file_for_deletion(directory, filename)
    try:
        file_mode = file_path.lstat().st_mode
    except FileNotFoundError:
        raise FileNotFoundError(filename) from None

    if stat.S_ISLNK(file_mode):
        raise PathTraversalError("Refusing to delete a symbolic link")
    if not stat.S_ISREG(file_mode):
        raise FileNotFoundError(filename)

    file_path.unlink()

    extensions = {str(extension).lower() for extension in convertible_extensions}
    if file_path.suffix.lower() in extensions:
        markdown_path = file_path.with_suffix(".md")
        try:
            markdown_mode = markdown_path.lstat().st_mode
        except FileNotFoundError:
            markdown_mode = None
        if markdown_mode is not None and stat.S_ISREG(markdown_mode):
            markdown_path.unlink()

    return {"success": True, "message": f"Successfully deleted {file_path.name}"}
