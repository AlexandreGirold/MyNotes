"""Tests for src/mynotes/backup.py"""

from __future__ import annotations

import json
import os
import pathlib
import sqlite3
import tempfile
import zipfile
from typing import Any
from unittest import mock

import pytest

from mynotes.backup import (
    _DB_NAME,
    _META_NAME,
    MAX_ARCHIVE_SIZE,
    MAX_ENTRY_COUNT,
    MAX_ENTRY_SIZE,
    _is_excluded,
    _reject_path,
    _validate_archive,
    backup,
    restore,
    safe_extract,
)

# =============================================================================
# Helpers
# =============================================================================


def _make_data_dir(tmp_path: pathlib.Path, extra_files: dict[str, str] | None = None) -> pathlib.Path:
    """Create a data_dir with a library.sqlite3 containing one course."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    # Create a minimal but valid SQLite DB
    db_path = data_dir / _DB_NAME
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE TABLE courses (id TEXT PRIMARY KEY, name TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO courses VALUES ('abc', 'Mathematics')"
    )
    conn.commit()
    conn.close()

    if extra_files:
        for rel, content in extra_files.items():
            fpath = data_dir / rel
            fpath.parent.mkdir(parents=True, exist_ok=True)
            fpath.write_text(content, encoding="utf-8")

    return data_dir


def _create_malicious_zip(
    tmp_path: pathlib.Path,
    *,
    name: str = "evil.zip",
    entries: dict[str, bytes] | None = None,
    include_meta: bool = False,
    meta_override: dict | None = None,
) -> pathlib.Path:
    """Create a malicious zip archive for testing validation."""
    archive = tmp_path / name
    with zipfile.ZipFile(archive, "w") as zf:
        if entries:
            for fname, content in entries.items():
                zf.writestr(fname, content)
        if include_meta:
            meta = meta_override or {
                "version": 1,
                "timestamp": 0,
                "data_dir": str(tmp_path / "data"),
                "files": [
                    {"archive_path": "test.txt", "size": len(entries.get("test.txt", b""))}
                ],
            }
            zf.writestr(_META_NAME, json.dumps(meta))
    return archive


# =============================================================================
# _is_excluded
# =============================================================================


class Test_is_excluded:
    @pytest.mark.parametrize("name", [
        "credentials.enc", "keys", "key", ".env",
    ])
    def test_excludes_credentials(self, name: str) -> None:
        assert _is_excluded(name) is True

    @pytest.mark.parametrize("name", [
        "library.sqlite3", "lecture.pdf", "notes.txt", "data.csv",
    ])
    def test_includes_regular_files(self, name: str) -> None:
        assert _is_excluded(name) is False

    def test_excludes_backup_files(self) -> None:
        assert _is_excluded("library.sqlite3.bak") is True
        assert _is_excluded("data.backup") is True

    def test_excludes_zip_files(self) -> None:
        assert _is_excluded("existing_backup.zip") is True

    def test_includes_non_backup_zip_like(self) -> None:
        # A file that doesn't end with excluded suffixes
        assert _is_excluded("data.json") is False


# =============================================================================
# _reject_path
# =============================================================================


class Test_reject_path:
    def test_accepts_simple_file(self, tmp_path: pathlib.Path) -> None:
        _reject_path("file.txt", tmp_path)  # no raise

    def test_accepts_subdirectory(self, tmp_path: pathlib.Path) -> None:
        _reject_path("sub/file.txt", tmp_path)  # no raise

    def test_rejects_absolute_path(self, tmp_path: pathlib.Path) -> None:
        with pytest.raises(ValueError, match="absolute"):
            _reject_path("/etc/passwd", tmp_path)

    def test_rejects_dotdot(self, tmp_path: pathlib.Path) -> None:
        with pytest.raises(ValueError, match="traversal|escapes"):
            _reject_path("../etc/passwd", tmp_path)

    def test_rejects_dotdot_nested(self, tmp_path: pathlib.Path) -> None:
        with pytest.raises(ValueError, match="traversal|escapes"):
            _reject_path("a/../../etc/passwd", tmp_path)

    def test_rejects_windows_drive(self, tmp_path: pathlib.Path) -> None:
        with pytest.raises(ValueError, match="Windows"):
            _reject_path("C:/Windows/system32", tmp_path)


# =============================================================================
# backup
# =============================================================================


class Test_backup:
    def test_creates_archive(self, tmp_path: pathlib.Path) -> None:
        data_dir = _make_data_dir(tmp_path)
        dest = tmp_path / "backups"
        result = backup(data_dir, dest)
        assert result.exists()
        assert result.name.endswith(".zip")

    def test_archive_contains_db(self, tmp_path: pathlib.Path) -> None:
        data_dir = _make_data_dir(tmp_path)
        dest = tmp_path / "backups"
        result = backup(data_dir, dest)

        with zipfile.ZipFile(result, "r") as zf:
            names = zf.namelist()
            assert _DB_NAME in names
            assert _META_NAME in names

    def test_archive_contains_referenced_files(self, tmp_path: pathlib.Path) -> None:
        data_dir = _make_data_dir(tmp_path, {
            "originals/lecture1.pdf": "pdf content",
            "derived/slides.html": "<html></html>",
        })
        dest = tmp_path / "backups"
        result = backup(data_dir, dest)

        with zipfile.ZipFile(result, "r") as zf:
            names = zf.namelist()
            assert "originals/lecture1.pdf" in names
            assert "derived/slides.html" in names

    def test_archive_contains_metadata(self, tmp_path: pathlib.Path) -> None:
        data_dir = _make_data_dir(tmp_path, {"notes.txt": "hello"})
        dest = tmp_path / "backups"
        result = backup(data_dir, dest)

        with zipfile.ZipFile(result, "r") as zf:
            meta = json.loads(zf.read(_META_NAME))
            assert meta["version"] == 1
            assert meta["data_dir"] == str(data_dir)
            assert len(meta["files"]) >= 1
            paths = [f["archive_path"] for f in meta["files"]]
            assert _DB_NAME in paths
            assert "notes.txt" in paths

    def test_excludes_credentials_enc(self, tmp_path: pathlib.Path) -> None:
        data_dir = _make_data_dir(tmp_path, {
            "credentials.enc": "secret data",
        })
        dest = tmp_path / "backups"
        result = backup(data_dir, dest)

        with zipfile.ZipFile(result, "r") as zf:
            names = zf.namelist()
            assert "credentials.enc" not in names

    def test_excludes_keys(self, tmp_path: pathlib.Path) -> None:
        data_dir = _make_data_dir(tmp_path, {
            "keys": "my keys",
        })
        dest = tmp_path / "backups"
        result = backup(data_dir, dest)

        with zipfile.ZipFile(result, "r") as zf:
            names = zf.namelist()
            assert "keys" not in names

    def test_excludes_env(self, tmp_path: pathlib.Path) -> None:
        data_dir = _make_data_dir(tmp_path, {
            ".env": "API_KEY=secret",
        })
        dest = tmp_path / "backups"
        result = backup(data_dir, dest)

        with zipfile.ZipFile(result, "r") as zf:
            names = zf.namelist()
            assert ".env" not in names

    def test_excludes_bak_files(self, tmp_path: pathlib.Path) -> None:
        data_dir = _make_data_dir(tmp_path, {
            "data.bak": "old data",
        })
        dest = tmp_path / "backups"
        result = backup(data_dir, dest)

        with zipfile.ZipFile(result, "r") as zf:
            names = zf.namelist()
            assert "data.bak" not in names

    def test_excludes_zip_backups(self, tmp_path: pathlib.Path) -> None:
        data_dir = _make_data_dir(tmp_path, {
            "old_backup.zip": "old zip",
        })
        dest = tmp_path / "backups"
        result = backup(data_dir, dest)

        with zipfile.ZipFile(result, "r") as zf:
            names = zf.namelist()
            assert "old_backup.zip" not in names

    def test_excludes_symlinks(self, tmp_path: pathlib.Path) -> None:
        data_dir = _make_data_dir(tmp_path, {"target.txt": "real file"})
        link = data_dir / "link.txt"
        link.symlink_to(data_dir / "target.txt")
        dest = tmp_path / "backups"
        result = backup(data_dir, dest)

        with zipfile.ZipFile(result, "r") as zf:
            names = zf.namelist()
            assert "link.txt" not in names
            assert "target.txt" in names

    def test_db_snapshot_is_consistent(self, tmp_path: pathlib.Path) -> None:
        """The SQLite snapshot should be queryable and contain expected data."""
        data_dir = _make_data_dir(tmp_path)
        dest = tmp_path / "backups"
        result = backup(data_dir, dest)

        # Extract the DB snapshot from the archive
        with zipfile.ZipFile(result, "r") as zf:
            db_content = zf.read(_DB_NAME)

        # Write to a temp file and query it
        with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as f:
            f.write(db_content)
            tmp_db = f.name

        try:
            conn = sqlite3.connect(tmp_db)
            rows = conn.execute("SELECT name FROM courses").fetchall()
            conn.close()
            assert len(rows) == 1
            assert rows[0][0] == "Mathematics"
        finally:
            os.unlink(tmp_db)

    def test_missing_database_raises(self, tmp_path: pathlib.Path) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        dest = tmp_path / "backups"
        with pytest.raises(FileNotFoundError, match="Database not found"):
            backup(data_dir, dest)


# =============================================================================
# _validate_archive
# =============================================================================


class Test_validate_archive:
    def test_valid_archive_passes(self, tmp_path: pathlib.Path) -> None:
        data_dir = _make_data_dir(tmp_path)
        dest = tmp_path / "backups"
        archive = backup(data_dir, dest)
        meta = _validate_archive(archive)
        assert meta["version"] == 1
        assert "data_dir" in meta

    def test_missing_archive_raises(self, tmp_path: pathlib.Path) -> None:
        with pytest.raises(FileNotFoundError):
            _validate_archive(tmp_path / "nonexistent.zip")

    def test_missing_metadata_raises(self, tmp_path: pathlib.Path) -> None:
        archive = _create_malicious_zip(tmp_path)
        with pytest.raises(ValueError, match="missing metadata"):
            _validate_archive(archive)

    def test_wrong_version_raises(self, tmp_path: pathlib.Path) -> None:
        archive = _create_malicious_zip(
            tmp_path,
            include_meta=True,
            meta_override={"version": 2, "timestamp": 0, "data_dir": "/", "files": []},
        )
        with pytest.raises(ValueError, match="Unsupported.*version"):
            _validate_archive(archive)

    def test_empty_files_raises(self, tmp_path: pathlib.Path) -> None:
        archive = _create_malicious_zip(
            tmp_path,
            include_meta=True,
            meta_override={"version": 1, "timestamp": 0, "data_dir": "/", "files": []},
        )
        with pytest.raises(ValueError, match="No files"):
            _validate_archive(archive)

    def test_missing_data_dir_raises(self, tmp_path: pathlib.Path) -> None:
        archive = _create_malicious_zip(
            tmp_path,
            include_meta=True,
            meta_override={"version": 1, "timestamp": 0, "files": [{"archive_path": "x", "size": 0}]},
        )
        with pytest.raises(ValueError, match="Missing data_dir"):
            _validate_archive(archive)

    def test_overlarge_entry_raises(self, tmp_path: pathlib.Path) -> None:
        archive = _create_malicious_zip(
            tmp_path,
            include_meta=True,
            meta_override={
                "version": 1, "timestamp": 0, "data_dir": "/",
                "files": [{"archive_path": "big.bin", "size": MAX_ENTRY_SIZE + 1}],
            },
        )
        with pytest.raises(ValueError, match="exceeds max size"):
            _validate_archive(archive)

    def test_too_many_entries_raises(self, tmp_path: pathlib.Path) -> None:
        # Create actual entries in the zip so we hit the count check
        files_meta = [
            {"archive_path": f"f{i}", "size": 1} for i in range(MAX_ENTRY_COUNT + 1)
        ]
        # Also write them to the zip
        archive = tmp_path / "too_many.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            for fname in files_meta:
                zf.writestr(fname["archive_path"], b"x")
            meta = {
                "version": 1, "timestamp": 0, "data_dir": "/",
                "files": files_meta,
            }
            zf.writestr(_META_NAME, json.dumps(meta))
        with pytest.raises(ValueError, match="Too many entries"):
            _validate_archive(archive)

    def test_overall_archive_too_large(self, tmp_path: pathlib.Path) -> None:
        archive = tmp_path / "big.zip"
        archive.write_bytes(b"x" * 100)

        class FakeStat:
            st_size = MAX_ARCHIVE_SIZE + 1
            st_mode = 0o100644

        with mock.patch("mynotes.backup.pathlib.Path.stat", return_value=FakeStat()), \
             pytest.raises(ValueError, match="exceeds maximum size"):
            _validate_archive(archive)


# =============================================================================
# restore
# =============================================================================


class Test_restore:
    def _full_cycle(
        self, tmp_path: pathlib.Path, extra_files: dict[str, str] | None = None,
    ) -> tuple[pathlib.Path, pathlib.Path, dict[str, Any]]:
        """Run backup then restore, return (data_dir, archive)."""
        data_dir = _make_data_dir(tmp_path, extra_files)
        dest = tmp_path / "backups"
        archive = backup(data_dir, dest)
        # Restore to a NEW directory to test that restore works
        restore_dir = tmp_path / "restored"
        result = restore(restore_dir, archive)
        return restore_dir, archive, result

    def test_restore_succeeds(self, tmp_path: pathlib.Path) -> None:
        data_dir = _make_data_dir(tmp_path)
        dest = tmp_path / "backups"
        archive = backup(data_dir, dest)
        restore_dir = tmp_path / "restored"
        result = restore(restore_dir, archive)
        assert result["status"] == "ok"
        assert result["database_restored"] is True

    def test_restore_creates_db(self, tmp_path: pathlib.Path) -> None:
        data_dir, *_ = self._full_cycle(tmp_path)
        db_path = data_dir / _DB_NAME
        assert db_path.exists()

    def test_restore_preserves_db_data(self, tmp_path: pathlib.Path) -> None:
        data_dir, _, _ = self._full_cycle(tmp_path)
        db_path = data_dir / _DB_NAME
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute("SELECT name FROM courses").fetchall()
        conn.close()
        assert len(rows) == 1
        assert rows[0][0] == "Mathematics"

    def test_restore_preserves_files(self, tmp_path: pathlib.Path) -> None:
        data_dir, _, _ = self._full_cycle(tmp_path, {
            "originals/lecture1.pdf": "pdf content here",
            "notes.txt": "study notes",
        })
        assert (data_dir / "originals/lecture1.pdf").read_text(encoding="utf-8") == "pdf content here"
        assert (data_dir / "notes.txt").read_text(encoding="utf-8") == "study notes"

    def test_restore_reports_files_restored(self, tmp_path: pathlib.Path) -> None:
        data_dir = _make_data_dir(tmp_path, {"notes.txt": "hello"})
        dest = tmp_path / "backups"
        archive = backup(data_dir, dest)
        restore_dir = tmp_path / "restored"
        result = restore(restore_dir, archive)
        assert "notes.txt" in result["files_restored"]

    def test_restore_no_data_loss(self, tmp_path: pathlib.Path) -> None:
        """After backup+restore, no data should be lost."""
        extra = {
            "originals/lecture1.pdf": "content A",
            "originals/lecture2.pdf": "content B",
            "derived/slide.html": "<html><body>Slide</body></html>",
            "README.md": "# My Notes",
        }
        data_dir = _make_data_dir(tmp_path, extra)
        dest = tmp_path / "backups"
        archive = backup(data_dir, dest)

        # Verify original data
        for rel, content in extra.items():
            assert (data_dir / rel).read_text(encoding="utf-8") == content

        # Restore to a new directory
        restore_dir = tmp_path / "restored"
        result = restore(restore_dir, archive)
        assert result["status"] == "ok"

        # Verify all files are present with correct content
        for rel, content in extra.items():
            restored = (restore_dir / rel).read_text(encoding="utf-8")
            assert restored == content, f"Data loss in {rel}: expected {content!r}, got {restored!r}"

        # Verify DB data
        db_path = restore_dir / _DB_NAME
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute("SELECT name FROM courses").fetchall()
        conn.close()
        assert len(rows) == 1
        assert rows[0][0] == "Mathematics"

    def test_restore_fails_on_missing_archive(self, tmp_path: pathlib.Path) -> None:
        restore_dir = tmp_path / "restored"
        with pytest.raises(FileNotFoundError):
            restore(restore_dir, tmp_path / "nonexistent.zip")

    def test_restore_does_not_restore_credentials(self, tmp_path: pathlib.Path) -> None:
        """Even if credentials.enc was in the original data (shouldn't be),
        backup doesn't include it, so restore can't restore it."""
        data_dir = _make_data_dir(tmp_path, {
            "credentials.enc": "encrypted secrets",
        })
        dest = tmp_path / "backups"
        archive = backup(data_dir, dest)

        # Verify credentials.enc is NOT in the archive
        with zipfile.ZipFile(archive, "r") as zf:
            assert "credentials.enc" not in zf.namelist()

        restore_dir = tmp_path / "restored"
        result = restore(restore_dir, archive)
        assert result["status"] == "ok"
        # credentials.enc should NOT be restored
        assert not (restore_dir / "credentials.enc").exists()

    def test_restore_rolls_back_on_invalid_db(self, tmp_path: pathlib.Path) -> None:
        """If the staged DB fails integrity check, the original should be intact."""
        data_dir = _make_data_dir(tmp_path)
        dest = tmp_path / "backups"
        archive = backup(data_dir, dest)

        # Modify the staged DB by corrupting it: replace the zip entry
        archive_path = archive
        corrupted = tmp_path / "corrupted.zip"
        with zipfile.ZipFile(archive_path, "r") as zf:
            meta_raw = zf.read(_META_NAME)

        with zipfile.ZipFile(corrupted, "w") as zf:
            zf.writestr(_META_NAME, meta_raw)
            # Write a corrupted DB instead of the real one
            zf.writestr(_DB_NAME, b"not a sqlite database at all")
            # Update metadata to reflect the corruption (still valid meta)

        restore_dir = tmp_path / "restored"
        restore_dir.mkdir()
        # Write a valid DB first so rollback has something to restore
        orig_db = restore_dir / _DB_NAME
        conn = sqlite3.connect(str(orig_db))
        conn.execute("CREATE TABLE courses (id TEXT PRIMARY KEY, name TEXT NOT NULL)")
        conn.execute("INSERT INTO courses VALUES ('xyz', 'Original Course')")
        conn.commit()
        conn.close()

        with pytest.raises(ValueError, match="integrity check failed"):
            restore(restore_dir, corrupted)

        # Original DB should be restored
        conn = sqlite3.connect(str(orig_db))
        rows = conn.execute("SELECT name FROM courses").fetchall()
        conn.close()
        assert len(rows) == 1
        assert rows[0][0] == "Original Course"

    def test_restore_rejects_symlink_in_archive(self, tmp_path: pathlib.Path) -> None:
        """A malicious archive with a symlink entry should be rejected."""
        data_dir = _make_data_dir(tmp_path)
        dest = tmp_path / "backups"
        archive = backup(data_dir, dest)

        # Tamper with the archive to add a symlink entry
        corrupted = tmp_path / "symlink_archive.zip"
        with zipfile.ZipFile(archive, "r") as zf:
            meta_raw = zf.read(_META_NAME)
            file_data = {}
            for name in zf.namelist():
                if name != _META_NAME:
                    file_data[name] = zf.read(name)

        with zipfile.ZipFile(corrupted, "w") as zf:
            zf.writestr(_META_NAME, meta_raw)
            for name, data in file_data.items():
                zf.writestr(name, data)
            # Add a symlink entry
            info = zipfile.ZipInfo("malicious_link.txt")
            # Set Unix symlink attribute: 0o120000 << 16
            info.external_attr = (0o120000 << 16) | 0o644
            zf.writestr(info, "/etc/passwd")

        restore_dir = tmp_path / "restored"
        with pytest.raises(ValueError, match="Symlink"):
            restore(restore_dir, corrupted)

    def test_restore_rejects_path_traversal_in_archive(self, tmp_path: pathlib.Path) -> None:
        """A malicious archive with path traversal should be rejected."""
        data_dir = _make_data_dir(tmp_path)
        dest = tmp_path / "backups"
        archive = backup(data_dir, dest)

        corrupted = tmp_path / "traversal_archive.zip"
        with zipfile.ZipFile(archive, "r") as zf:
            meta_raw = zf.read(_META_NAME)
            file_data = {}
            for name in zf.namelist():
                if name != _META_NAME:
                    file_data[name] = zf.read(name)

        with zipfile.ZipFile(corrupted, "w") as zf:
            zf.writestr(_META_NAME, meta_raw)
            for name, data in file_data.items():
                zf.writestr(name, data)
            # Add a traversal entry
            zf.writestr("../../../tmp/evil.txt", "malicious content")

        restore_dir = tmp_path / "restored"
        with pytest.raises(ValueError, match="traversal|escapes"):
            restore(restore_dir, corrupted)

    def test_restore_rejects_absolute_path_in_archive(self, tmp_path: pathlib.Path) -> None:
        """A malicious archive with an absolute path should be rejected."""
        data_dir = _make_data_dir(tmp_path)
        dest = tmp_path / "backups"
        archive = backup(data_dir, dest)

        corrupted = tmp_path / "abs_archive.zip"
        with zipfile.ZipFile(archive, "r") as zf:
            meta_raw = zf.read(_META_NAME)
            file_data = {}
            for name in zf.namelist():
                if name != _META_NAME:
                    file_data[name] = zf.read(name)

        with zipfile.ZipFile(corrupted, "w") as zf:
            zf.writestr(_META_NAME, meta_raw)
            for name, data in file_data.items():
                zf.writestr(name, data)
            zf.writestr("/etc/passwd", "leaked")

        restore_dir = tmp_path / "restored"
        with pytest.raises(ValueError, match="absolute"):
            restore(restore_dir, corrupted)

    def test_restore_rejects_windows_drive_in_archive(self, tmp_path: pathlib.Path) -> None:
        """A malicious archive with a Windows drive letter should be rejected."""
        data_dir = _make_data_dir(tmp_path)
        dest = tmp_path / "backups"
        archive = backup(data_dir, dest)

        corrupted = tmp_path / "drive_archive.zip"
        with zipfile.ZipFile(archive, "r") as zf:
            meta_raw = zf.read(_META_NAME)
            file_data = {}
            for name in zf.namelist():
                if name != _META_NAME:
                    file_data[name] = zf.read(name)

        with zipfile.ZipFile(corrupted, "w") as zf:
            zf.writestr(_META_NAME, meta_raw)
            for name, data in file_data.items():
                zf.writestr(name, data)
            zf.writestr("C:/Windows/system32/config/sam", "evil")

        restore_dir = tmp_path / "restored"
        with pytest.raises(ValueError, match="Windows"):
            restore(restore_dir, corrupted)


# =============================================================================
# safe_extract
# =============================================================================


class Test_safe_extract:
    def test_extract_valid_archive(self, tmp_path: pathlib.Path) -> None:
        data_dir = _make_data_dir(tmp_path, {"file.txt": "hello"})
        dest = tmp_path / "backups"
        archive = backup(data_dir, dest)

        out = tmp_path / "output"
        safe_extract(archive, out)

        assert (out / "file.txt").read_text(encoding="utf-8") == "hello"
        assert (out / _DB_NAME).exists()

    def test_extract_rejects_symlink_in_archive(self, tmp_path: pathlib.Path) -> None:
        data_dir = _make_data_dir(tmp_path)
        dest = tmp_path / "backups"
        archive = backup(data_dir, dest)

        corrupted = tmp_path / "symlink_ext.zip"
        with zipfile.ZipFile(archive, "r") as zf:
            meta_raw = zf.read(_META_NAME)
            file_data = {}
            for name in zf.namelist():
                if name != _META_NAME:
                    file_data[name] = zf.read(name)

        with zipfile.ZipFile(corrupted, "w") as zf:
            zf.writestr(_META_NAME, meta_raw)
            for name, data in file_data.items():
                zf.writestr(name, data)
            info = zipfile.ZipInfo("evil_link.txt")
            info.external_attr = (0o120000 << 16) | 0o644
            zf.writestr(info, "/tmp/evil")

        out = tmp_path / "output"
        with pytest.raises(ValueError, match="Symlink"):
            safe_extract(corrupted, out)

    def test_extract_rejects_path_traversal(self, tmp_path: pathlib.Path) -> None:
        data_dir = _make_data_dir(tmp_path)
        dest = tmp_path / "backups"
        archive = backup(data_dir, dest)

        corrupted = tmp_path / "traversal_ext.zip"
        with zipfile.ZipFile(archive, "r") as zf:
            meta_raw = zf.read(_META_NAME)
            file_data = {}
            for name in zf.namelist():
                if name != _META_NAME:
                    file_data[name] = zf.read(name)

        with zipfile.ZipFile(corrupted, "w") as zf:
            zf.writestr(_META_NAME, meta_raw)
            for name, data in file_data.items():
                zf.writestr(name, data)
            zf.writestr("../../etc/passwd", "leaked")

        out = tmp_path / "output"
        with pytest.raises(ValueError, match="traversal|escapes"):
            safe_extract(corrupted, out)

    def test_extract_rejects_absolute_path(self, tmp_path: pathlib.Path) -> None:
        data_dir = _make_data_dir(tmp_path)
        dest = tmp_path / "backups"
        archive = backup(data_dir, dest)

        corrupted = tmp_path / "abs_ext.zip"
        with zipfile.ZipFile(archive, "r") as zf:
            meta_raw = zf.read(_META_NAME)
            file_data = {}
            for name in zf.namelist():
                if name != _META_NAME:
                    file_data[name] = zf.read(name)

        with zipfile.ZipFile(corrupted, "w") as zf:
            zf.writestr(_META_NAME, meta_raw)
            for name, data in file_data.items():
                zf.writestr(name, data)
            zf.writestr("/etc/shadow", "leaked")

        out = tmp_path / "output"
        with pytest.raises(ValueError, match="absolute"):
            safe_extract(corrupted, out)

    def test_extract_excludes_metadata_from_output(self, tmp_path: pathlib.Path) -> None:
        """Metadata file should not be extracted to the destination."""
        data_dir = _make_data_dir(tmp_path)
        dest = tmp_path / "backups"
        archive = backup(data_dir, dest)

        out = tmp_path / "output"
        safe_extract(archive, out)

        assert not (out / _META_NAME).exists()

    def test_extract_no_meta_in_archive(self, tmp_path: pathlib.Path) -> None:
        """Ordinary project ZIP archives do not need MyNotes metadata."""
        archive = _create_malicious_zip(tmp_path)
        out = tmp_path / "output"
        safe_extract(archive, out)


# =============================================================================
# Malicious zip comprehensive tests
# =============================================================================


class TestMaliciousZip:
    """Tests specifically for malicious archive edge cases."""

    def test_empty_archive(self, tmp_path: pathlib.Path) -> None:
        archive = tmp_path / "empty.zip"
        with zipfile.ZipFile(archive, "w"):
            pass
        with pytest.raises(ValueError, match="missing metadata|No files"):
            _validate_archive(archive)

    def test_archive_with_only_metadata(self, tmp_path: pathlib.Path) -> None:
        archive = _create_malicious_zip(
            tmp_path,
            include_meta=True,
            meta_override={
                "version": 1,
                "timestamp": 0,
                "data_dir": "/",
                "files": [{"archive_path": "ghost.txt", "size": 10}],
            },
        )
        # Should fail because "ghost.txt" isn't actually in the zip
        with pytest.raises(ValueError, match="not in archive"):
            _validate_archive(archive)

    def test_archive_with_unicode_path(self, tmp_path: pathlib.Path) -> None:
        """Archives with Unicode paths should be handled gracefully."""
        data_dir = _make_data_dir(tmp_path, {"café.txt": "latin"})
        dest = tmp_path / "backups"
        archive = backup(data_dir, dest)

        restore_dir = tmp_path / "restored"
        result = restore(restore_dir, archive)
        assert result["status"] == "ok"

    def test_archive_with_deeply_nested_paths(self, tmp_path: pathlib.Path) -> None:
        """Files in deeply nested directories should be handled."""
        deep = "a/b/c/d/e/f/g/file.txt"
        data_dir = _make_data_dir(tmp_path, {deep: "deep content"})
        dest = tmp_path / "backups"
        archive = backup(data_dir, dest)

        restore_dir = tmp_path / "restored"
        result = restore(restore_dir, archive)
        assert result["status"] == "ok"
        assert (restore_dir / deep).read_text(encoding="utf-8") == "deep content"

    def test_archive_with_empty_files(self, tmp_path: pathlib.Path) -> None:
        """Files with 0 bytes should be handled."""
        data_dir = _make_data_dir(tmp_path, {"empty.txt": ""})
        dest = tmp_path / "backups"
        archive = backup(data_dir, dest)

        restore_dir = tmp_path / "restored"
        result = restore(restore_dir, archive)
        assert result["status"] == "ok"
        assert (restore_dir / "empty.txt").read_text(encoding="utf-8") == ""

    def test_archive_with_large_number_of_small_files(self, tmp_path: pathlib.Path) -> None:
        """Many small files should work."""
        extra = {f"file_{i:04d}.txt": f"content {i}" for i in range(50)}
        data_dir = _make_data_dir(tmp_path, extra)
        dest = tmp_path / "backups"
        archive = backup(data_dir, dest)

        restore_dir = tmp_path / "restored"
        result = restore(restore_dir, archive)
        assert result["status"] == "ok"
        assert len(result["files_restored"]) >= 50
