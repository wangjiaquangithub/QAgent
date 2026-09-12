"""Tests for the shared gateway/embedded-client upload storage helpers."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from evoflow.config.paths import reset_paths_cache
from evoflow.uploads.manager import (
    PathTraversalError,
    claim_unique_filename,
    delete_file_safe,
    enrich_file_listing,
    ensure_uploads_dir,
    get_uploads_dir,
    list_files_in_dir,
    normalize_filename,
)


@pytest.fixture
def uploads_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("EVOFLOW_HOME", str(tmp_path / "evoflow-home"))
    reset_paths_cache()
    try:
        yield ensure_uploads_dir("thread-1")
    finally:
        reset_paths_cache()


def test_normalize_filename_accepts_flat_unicode_names() -> None:
    assert normalize_filename("报告 2026.txt") == "报告 2026.txt"


@pytest.mark.parametrize("filename", ["", " ", ".", "..", "../file", "a/b", r"a\b", "/tmp/file", "C:report.txt", "a\x00b"])
def test_normalize_filename_rejects_unsafe_paths(filename: str) -> None:
    with pytest.raises(ValueError):
        normalize_filename(filename)


def test_thread_upload_path_is_isolated_and_validated(uploads_dir: Path) -> None:
    assert get_uploads_dir("thread-1") == uploads_dir
    assert uploads_dir.is_dir()
    with pytest.raises(ValueError):
        get_uploads_dir("../other")


def test_listing_enrichment_and_symlink_exclusion(uploads_dir: Path) -> None:
    (uploads_dir / "z.txt").write_bytes(b"abc")
    (uploads_dir / "a b.txt").write_bytes(b"x")
    (uploads_dir / "nested").mkdir()
    os.symlink(uploads_dir / "z.txt", uploads_dir / "linked.txt")

    listing = enrich_file_listing(list_files_in_dir(uploads_dir), "thread-1")

    assert listing == {
        "files": [
            {
                "filename": "a b.txt",
                "size": "1",
                "virtual_path": "/mnt/user-data/uploads/a b.txt",
                "artifact_url": "/api/threads/thread-1/artifacts/mnt/user-data/uploads/a%20b.txt",
            },
            {
                "filename": "z.txt",
                "size": "3",
                "virtual_path": "/mnt/user-data/uploads/z.txt",
                "artifact_url": "/api/threads/thread-1/artifacts/mnt/user-data/uploads/z.txt",
            },
        ],
        "count": 2,
    }


def test_delete_removes_convertible_file_and_generated_markdown(uploads_dir: Path) -> None:
    (uploads_dir / "document.pdf").write_bytes(b"pdf")
    (uploads_dir / "document.md").write_text("converted", encoding="utf-8")

    assert delete_file_safe(uploads_dir, "document.pdf", convertible_extensions={".pdf"}) == {
        "success": True,
        "message": "Successfully deleted document.pdf",
    }
    assert not (uploads_dir / "document.pdf").exists()
    assert not (uploads_dir / "document.md").exists()


@pytest.mark.parametrize("filename", ["../secret", "nested/file", r"nested\file"])
def test_delete_rejects_unsafe_filename(uploads_dir: Path, filename: str) -> None:
    with pytest.raises(PathTraversalError):
        delete_file_safe(uploads_dir, filename)


def test_delete_refuses_symbolic_link(uploads_dir: Path) -> None:
    target = uploads_dir / "target.txt"
    target.write_text("keep", encoding="utf-8")
    os.symlink(target, uploads_dir / "link.txt")

    with pytest.raises(PathTraversalError):
        delete_file_safe(uploads_dir, "link.txt")
    assert target.exists()


def test_claim_unique_filename_preserves_suffix() -> None:
    names: set[str] = set()
    assert claim_unique_filename("report.pdf", names) == "report.pdf"
    assert claim_unique_filename("report.pdf", names) == "report (1).pdf"
    assert claim_unique_filename("report.pdf", names) == "report (2).pdf"
