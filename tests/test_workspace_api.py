from fastapi.testclient import TestClient

from mynotes.api import create_app
from mynotes.db import Database
from mynotes.security import CredentialStore


def test_canonical_file_write_and_external_conflict_without_copies(tmp_path):
    root = tmp_path / "laptop"
    root.mkdir()
    file = root / "note.md"
    file.write_text("Original")
    (root / "subfolder").mkdir()
    (root / "image.bin").write_bytes(b"\x00\x01")
    (root / ".env").write_text("placeholder")
    data = tmp_path / "appdata"
    data.mkdir()
    db = Database(data / "library.sqlite3")
    db.set_settings({"agent_directories": [str(root)]})
    app = create_app(db=db, data_dir=data, token="test", tool_token="tools", credentials=CredentialStore(data))
    with TestClient(app) as client:
        client.headers.update({"Authorization": "Bearer test"})
        tree = client.get("/workspace/entries", params={"path": str(root)}).json()
        assert {e["name"] for e in tree["entries"]} == {"note.md", "subfolder", "image.bin"}
        original = client.get("/workspace/text", params={"path": str(file)}).json()
        write = client.put("/workspace/text", json={"path": str(file), "text": "Updated", "expected_sha256": original["sha256"]})
        assert write.status_code == 200 and file.read_text() == "Updated"
        file.write_text("External change")
        conflict = client.put("/workspace/text", json={"path": str(file), "text": "My draft", "expected_sha256": write.json()["sha256"]})
        assert conflict.status_code == 409 and file.read_text() == "External change"
        assert client.get("/workspace/text", params={"path": str(root / ".env")}).status_code == 400
        assert client.put("/workspace/text", json={"path": str(tmp_path / "outside.md"), "text": "No"}).status_code == 400
    assert not (data / "originals").exists() and not (data / "notes").exists()
