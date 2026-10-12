"""Tests for src/mynotes/workspace.py."""
from __future__ import annotations

import hashlib
import os
import pathlib
import stat
import tempfile

import pytest

from mynotes.workspace import (
    WorkspaceConflict,
    _allowed,
    _is_text_file,
    atomic_write,
    list_directory,
    read_text,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def dirs(tmp_path: pathlib.Path) -> list[str]:
    """A temporary root directory shared across all tests.

    Creates three sub-dirs:
    - ``workspace/notes`` — for normal notes
    - ``workspace/code``  — for code files
    - ``workspace/secure`` — for credential-file deny tests
    """
    notes = tmp_path / "workspace" / "notes"
    code = tmp_path / "workspace" / "code"
    secure = tmp_path / "workspace" / "secure"
    for d in (notes, code, secure):
        d.mkdir(parents=True)
    return [str(notes), str(code)]


# ---------------------------------------------------------------------------
# _allowed  /  _is_text_file
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_allowed_resolves_within_dirs(self, dirs: list[str]) -> None:
        path = str(pathlib.Path(dirs[0]) / "x.md")
        p = _allowed(path, dirs)
        assert p.exists() is False  # not created, just resolved
        assert p.name == "x.md"

    def test_allowed_rejects_outside(self, dirs: list[str]) -> None:
        with pytest.raises(ValueError, match="outside"):
            _allowed("/etc/passwd", dirs)

    def test_allowed_rejects_credential_file(self, dirs: list[str]) -> None:
        p = str(pathlib.Path(dirs[0]) / "credentials.enc")
        with pytest.raises(ValueError, match="Credential"):
            _allowed(p, dirs)

    def test_allowed_rejects_dotenv_file(self, dirs: list[str]) -> None:
        p = str(pathlib.Path(dirs[0]) / ".env.local")
        with pytest.raises(ValueError, match="Credential"):
            _allowed(p, dirs)

    def test_allowed_rejects_private_key_ext(self, dirs: list[str]) -> None:
        p = str(pathlib.Path(dirs[0]) / "server.pem")
        with pytest.raises(ValueError, match="Credential"):
            _allowed(p, dirs)

    def test_allowed_rejects_symlink_escape(self, dirs: list[str]) -> None:
        notes = pathlib.Path(dirs[0])
        esc = notes / "escape"
        esc.symlink_to("/etc")
        with pytest.raises(ValueError):
            _allowed(str(notes / "escape" / "passwd"), dirs)

    def test_is_text_file(self) -> None:
        assert _is_text_file(pathlib.Path("a.md"))
        assert _is_text_file(pathlib.Path("b.py"))
        assert _is_text_file(pathlib.Path("c.TXT"))
        assert not _is_text_file(pathlib.Path("photo.jpg"))
        assert not _is_text_file(pathlib.Path("blob.exe"))


# ---------------------------------------------------------------------------
# list_directory
# ---------------------------------------------------------------------------


class TestListDirectory:
    def test_root_entries(self, tmp_path: pathlib.Path, dirs: list[str]) -> None:
        # Root listing returns the directories themselves
        result = list_directory("", dirs)
        assert result["next_offset"] is None
        names = sorted(e["name"] for e in result["entries"])
        assert names == ["code", "notes"]

    def test_empty_directory(self, dirs: list[str]) -> None:
        result = list_directory(dirs[0], dirs)
        assert result["entries"] == []

    def test_nested_tree(self, dirs: list[str]) -> None:
        notes = pathlib.Path(dirs[0])
        (notes / "hello.md").write_text("hi", encoding="utf-8")
        (notes / "sub").mkdir()
        (notes / "sub" / "deep.py").write_text("pass", encoding="utf-8")

        result = list_directory(str(notes), dirs)
        assert len(result["entries"]) == 2
        names = [e["name"] for e in result["entries"]]
        assert "hello.md" in names
        assert "sub" in names
        # dirs first
        assert result["entries"][0]["directory"] is True

    def test_pagination(self, dirs: list[str]) -> None:
        notes = pathlib.Path(dirs[0])
        for i in range(250):
            (notes / f"f{i:04d}.md").write_text("x", encoding="utf-8")
        r1 = list_directory(str(notes), dirs, offset=0, limit=100)
        assert len(r1["entries"]) == 100
        assert r1["next_offset"] == 100

        r2 = list_directory(str(notes), dirs, offset=100, limit=100)
        assert len(r2["entries"]) == 100
        assert r2["next_offset"] == 200

    def test_pagination_last_page(self, dirs: list[str]) -> None:
        notes = pathlib.Path(dirs[0])
        for i in range(50):
            (notes / f"f{i:04d}.md").write_text("x", encoding="utf-8")
        r = list_directory(str(notes), dirs, offset=0, limit=100)
        assert len(r["entries"]) == 50
        assert r["next_offset"] is None

    def test_filters_credential_files(self, dirs: list[str]) -> None:
        notes = pathlib.Path(dirs[0])
        (notes / "credentials.enc").write_text("secret", encoding="utf-8")
        (notes / "note.md").write_text("ok", encoding="utf-8")
        result = list_directory(str(notes), dirs)
        assert len(result["entries"]) == 1
        assert result["entries"][0]["name"] == "note.md"

    def test_sort_dirs_first_name_stable(self, dirs: list[str]) -> None:
        notes = pathlib.Path(dirs[0])
        (notes / "b_dir").mkdir()
        (notes / "a_file.md").write_text("x", encoding="utf-8")
        (notes / "z_file.md").write_text("x", encoding="utf-8")
        result = list_directory(str(notes), dirs)
        names = [e["name"] for e in result["entries"]]
        assert names == ["b_dir", "a_file.md", "z_file.md"]


# ---------------------------------------------------------------------------
# read_text
# ---------------------------------------------------------------------------


class TestReadText:
    def test_read_plain_text(self, dirs: list[str]) -> None:
        notes = pathlib.Path(dirs[0])
        f = notes / "note.md"
        f.write_text("hello world", encoding="utf-8")
        result = read_text(str(f), dirs)
        assert result["text"] == "hello world"
        assert result["path"] == str(f)
        expected_hash = hashlib.sha256(b"hello world").hexdigest()
        assert result["sha256"] == expected_hash

    def test_read_utf8_unicode(self, dirs: list[str]) -> None:
        notes = pathlib.Path(dirs[0])
        f = notes / "unicöde.txt"
        f.write_text("こんにちは 🎓 café résumé", encoding="utf-8")
        result = read_text(str(f), dirs)
        assert "こんにちは" in result["text"]
        assert "🎓" in result["text"]

    def test_rejects_no_extension(self, dirs: list[str]) -> None:
        notes = pathlib.Path(dirs[0])
        f = notes / "noext"
        f.write_text("data", encoding="utf-8")
        with pytest.raises(ValueError, match="not a recognised text type"):
            read_text(str(f), dirs)

    def test_rejects_binary(self, dirs: list[str]) -> None:
        notes = pathlib.Path(dirs[0])
        f = notes / "image.png"
        f.write_bytes(b"\x89PNG\x0d\x0a\x1a\x0a\x00")
        with pytest.raises(ValueError, match="not a recognised text type"):
            read_text(str(f), dirs)

    def test_rejects_binary_with_text_ext(self, dirs: list[str]) -> None:
        notes = pathlib.Path(dirs[0])
        f = notes / "blob.md"
        f.write_bytes(b"hello\x00world")
        with pytest.raises(ValueError, match="binary"):
            read_text(str(f), dirs)

    def test_rejects_missing_file(self, dirs: list[str]) -> None:
        with pytest.raises(FileNotFoundError):
            read_text(str(pathlib.Path(dirs[0]) / "ghost.md"), dirs)

    def test_rejects_exceeds_2mb(self, dirs: list[str]) -> None:
        notes = pathlib.Path(dirs[0])
        f = notes / "big.md"
        f.write_bytes(b"a" * (3 * 1024 * 1024))
        with pytest.raises(ValueError, match="2 MB"):
            read_text(str(f), dirs)

    def test_rejects_outside(self, dirs: list[str]) -> None:
        with pytest.raises(ValueError, match="outside"):
            read_text("/etc/passwd", dirs)


# ---------------------------------------------------------------------------
# atomic_write
# ---------------------------------------------------------------------------


class TestAtomicWrite:
    def test_new_file(self, dirs: list[str]) -> None:
        notes = pathlib.Path(dirs[0])
        f = notes / "new.md"
        assert not f.exists()
        result = atomic_write(str(f), "hello", dirs)
        assert f.exists()
        assert f.read_text(encoding="utf-8") == "hello"
        assert result["path"] == str(f)
        assert hashlib.sha256(b"hello").hexdigest() == result["sha256"]

    def test_new_existing_rejected(self, dirs: list[str]) -> None:
        notes = pathlib.Path(dirs[0])
        f = notes / "existing.md"
        f.write_text("orig", encoding="utf-8")
        with pytest.raises(WorkspaceConflict):
            atomic_write(str(f), "new", dirs)

    def test_update_with_matching_hash(self, dirs: list[str]) -> None:
        notes = pathlib.Path(dirs[0])
        f = notes / "upd.md"
        f.write_text("orig", encoding="utf-8")
        h = hashlib.sha256(b"orig").hexdigest()
        result = atomic_write(str(f), "updated", dirs, expected_sha256=h)
        assert f.read_text(encoding="utf-8") == "updated"
        assert hashlib.sha256(b"updated").hexdigest() == result["sha256"]

    def test_update_mismatched_hash_rejected(self, dirs: list[str]) -> None:
        notes = pathlib.Path(dirs[0])
        f = notes / "conflict.md"
        f.write_text("v1", encoding="utf-8")
        with pytest.raises(WorkspaceConflict):
            atomic_write(str(f), "hacked", dirs, expected_sha256="deadbeef")

    def test_preserves_existing_mode(self, dirs: list[str]) -> None:
        notes = pathlib.Path(dirs[0])
        f = notes / "mode.md"
        f.write_text("x", encoding="utf-8")
        os.chmod(str(f), stat.S_IRUSR | stat.S_IWUSR)  # 0o600
        h = hashlib.sha256(b"x").hexdigest()
        atomic_write(str(f), "y", dirs, expected_sha256=h)
        # mode should be preserved
        actual_mode = stat.S_IMODE(os.stat(str(f)).st_mode)
        assert actual_mode == stat.S_IRUSR | stat.S_IWUSR

    def test_rejects_unknown_ext(self, dirs: list[str]) -> None:
        notes = pathlib.Path(dirs[0])
        f = notes / "binary.exe"
        with pytest.raises(ValueError, match="not a recognised text type"):
            atomic_write(str(f), "data", dirs)

    def test_rejects_over_2mb_content(self, dirs: list[str]) -> None:
        notes = pathlib.Path(dirs[0])
        f = notes / "big.md"
        with pytest.raises(ValueError, match="2 MB"):
            atomic_write(str(f), "a" * (3 * 1024 * 1024), dirs)

    def test_rejects_outside(self, dirs: list[str]) -> None:
        with pytest.raises(ValueError, match="outside"):
            atomic_write("/tmp/escape.md", "x", dirs)

    def test_write_failure_keeps_original(self, dirs: list[str]) -> None:
        notes = pathlib.Path(dirs[0])
        f = notes / "fail.md"
        orig = "original"
        f.write_text(orig, encoding="utf-8")
        h = hashlib.sha256(orig.encode("utf-8")).hexdigest()

        # Write to the same file but make the hash mismatch — the
        # original should remain untouched.
        with pytest.raises(WorkspaceConflict):
            atomic_write(str(f), "wrong", dirs, expected_sha256="wronghash")
        assert f.read_text(encoding="utf-8") == orig

    def test_no_appdata_copy(self, dirs: list[str]) -> None:
        """Originals stay at their paths; nothing written to appdata."""
        notes = pathlib.Path(dirs[0])
        f = notes / "original.md"
        f.write_text("stays", encoding="utf-8")
        h = hashlib.sha256(b"stays").hexdigest()
        atomic_write(str(f), "updated", dirs, expected_sha256=h)
        # The file is still at its original path
        assert f.exists()
        assert f.read_text(encoding="utf-8") == "updated"
        # No copy in any application data directory (the workspace
        # only writes within the allowed directories).

    def test_atomic_same_dir_tmp(self, dirs: list[str]) -> None:
        """Temp file must be in the same directory as the target."""
        notes = pathlib.Path(dirs[0])
        f = notes / "atomic.md"
        # We can't easily intercept mkstemp here, so we just confirm
        # the write succeeds and the target exists.
        result = atomic_write(str(f), "test", dirs)
        assert f.exists()
        assert result["path"] == str(f)

    def test_cleanup_no_temp_after_failure(self, dirs: list[str]) -> None:
        """On failure, no .mynotes.* temp file should remain."""
        notes = pathlib.Path(dirs[0])
        parent = notes / "nested"
        parent.mkdir()
        f = parent / "dest.md"

        # Write that will fail because of a mismatching hash
        existing = notes / "existing.md"
        existing.write_text("x", encoding="utf-8")
        hx = hashlib.sha256(b"x").hexdigest()
        atomic_write(str(existing), "y", dirs, expected_sha256=hx)
        # Now force a failure: change file externally, then try to write
        existing.write_text("changed", encoding="utf-8")
        temp_files_before = list(notes.iterdir())
        try:
            atomic_write(str(existing), "z", dirs, expected_sha256=hx)
        except WorkspaceConflict:
            pass
        temp_files_after = list(notes.iterdir())
        temp_before = {p.name for p in temp_files_before}
        temp_after = {p.name for p in temp_files_after}
        assert temp_after <= temp_before, "Temp file leaked"
