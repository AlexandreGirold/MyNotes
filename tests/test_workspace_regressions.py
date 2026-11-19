import os
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from mynotes.api import create_app
from mynotes.db import Database
from mynotes.security import CredentialStore
from mynotes.workspace import atomic_write, list_directory
from mynotes.agent_files import read_file


def test_failed_permission_change_closes_descriptor_and_removes_temporary_file(tmp_path):
    import tempfile
    opened = []
    original = tempfile.mkstemp
    def capture(*args, **kwargs):
        result = original(*args, **kwargs)
        opened.append(result[0])
        return result
    with patch("mynotes.workspace.tempfile.mkstemp", side_effect=capture), patch("mynotes.workspace.os.fchmod", side_effect=OSError("permission failure")):
        with pytest.raises(OSError, match="permission failure"):
            atomic_write(str(tmp_path / "note.md"), "draft", [str(tmp_path)])
    try:
        with pytest.raises(OSError):
            os.fstat(opened[0])
    finally:
        try:
            os.close(opened[0])
        except OSError:
            pass
    assert not list(tmp_path.iterdir())


def test_directory_listing_stats_each_entry_once_and_keeps_sorted_paging(tmp_path):
    for i in range(250):
        (tmp_path / f"file-{i:03}.md").touch()
    (tmp_path / "folder").mkdir()
    original, calls = Path.stat, []
    def count(path, *args, **kwargs):
        calls.append(path)
        return original(path, *args, **kwargs)
    with patch.object(Path, "stat", count):
        page = list_directory(str(tmp_path), [str(tmp_path)])
    assert len(calls) == 251
    assert page["entries"][0]["name"] == "folder"
    assert len(page["entries"]) == 200 and page["next_offset"] == 200


def test_create_folder_api_conflicts_and_containment(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    db = Database(data / "library.sqlite3")
    db.set_settings({"agent_directories": [str(root)]})
    app = create_app(db=db, data_dir=data, token="test", tool_token="tools", credentials=CredentialStore(data))
    with TestClient(app) as client:
        assert client.post("/workspace/directories", json={"path": str(root / "notes")}).status_code == 401
        client.headers.update({"Authorization": "Bearer test"})
        assert client.post("/workspace/directories", json={"path": str(root / "notes")}).status_code == 201
        assert (root / "notes").is_dir()
        assert client.post("/workspace/directories", json={"path": str(root / "notes")}).status_code == 409
        assert client.post("/workspace/directories", json={"path": str(tmp_path / "outside")}).status_code == 400
        assert client.post("/workspace/directories", json={"path": str(root / ".ssh")}).status_code == 400
    assert not (tmp_path / "outside").exists()


def test_allowed_symlink_stays_valid_after_atomic_save(tmp_path):
    actual = tmp_path / "actual.md"
    saved = atomic_write(str(actual), "first", [str(tmp_path)])
    link = tmp_path / "link.md"
    link.symlink_to(actual)
    atomic_write(str(link), "second", [str(tmp_path)], saved["sha256"])
    assert link.is_symlink() and link.read_text() == actual.read_text() == "second"


@pytest.mark.parametrize("name", [".netrc", ".npmrc", ".pypirc", ".git-credentials"])
def test_common_credentials_are_hidden_and_unreadable(tmp_path, name):
    file = tmp_path / name
    file.write_text("placeholder credential")
    assert list_directory(str(tmp_path), [str(tmp_path)])["entries"] == []
    with pytest.raises(ValueError, match="Credential"):
        read_file(str(file), directories=[str(tmp_path)])
