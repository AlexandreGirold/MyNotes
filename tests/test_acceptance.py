"""Real cross-module checks; only cloud calls are mocked."""
import json
import os
from pathlib import Path
from unittest.mock import patch

import httpx
import pypdfium2 as pdfium
from fastapi.testclient import TestClient
from PIL import Image
from pptx import Presentation
from pptx.util import Inches

from mynotes.api import create_app
from mynotes.db import Database
from mynotes.security import CredentialStore
from mynotes.worker import Worker
from mynotes import bridge


def completed(worker, kind, payload):
    ack = worker.schedule(kind, payload)
    result = worker.run_once()
    assert result["id"] == ack["id"]
    assert result["status"] == "succeeded", result.get("error")
    return json.loads(result["result"])


def test_real_notes_backup_restore_preserves_old_versions_and_jobs(tmp_path):
    db = Database(tmp_path / "library.sqlite3")
    worker = Worker(db, tmp_path)
    first = completed(worker, "note", {"title": "Lecture", "content": "Quantum original evidence", "format": "markdown"})
    original = db.get_document(first["document_id"])
    old_version = original["current_version_id"]
    updated = completed(worker, "note", {"title": "Lecture", "content": "Gravity revised evidence", "document_id": original["id"]})
    assert Path(first["path"]).read_text() == "Quantum original evidence"
    assert Path(updated["path"]).read_text() == "Gravity revised evidence"
    assert db.search("quantum") == []
    assert db.search("gravity")[0]["document_id"] == original["id"]
    backup = completed(worker, "backup", {})
    assert (tmp_path / backup["backup_path"]).is_file()
    completed(worker, "note", {"title": "Later", "content": "Not in the backup"})
    restore_ack = worker.schedule("restore", {"source_path": backup["backup_path"]})
    restored_job = worker.run_once()
    assert restored_job["id"] == restore_ack["id"]
    assert restored_job["status"] == "succeeded", restored_job.get("error")
    assert len(db.list_documents()) == 1
    assert db.get_version(old_version)["blocks"][0]["text"] == "Quantum original evidence"
    assert db.search("gravity")


def test_pptx_import_dispatch_preserves_pictures_after_job_cleanup(tmp_path):
    image = tmp_path / "picture.png"
    Image.new("RGB", (24, 24), "blue").save(image)
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    slide.shapes.add_textbox(Inches(1), Inches(1), Inches(4), Inches(1)).text_frame.text = "PPTX searchable evidence"
    slide.shapes.add_picture(str(image), Inches(1), Inches(3))
    source = tmp_path / "slides.pptx"
    prs.save(source)
    db = Database(tmp_path / "library.sqlite3")
    result = completed(Worker(db, tmp_path), "import", {"source_path": str(source), "kind": "pptx"})
    version = db.get_document(result["document_id"])
    assert db.search("searchable")
    assert (tmp_path / version["original_path"]).is_file()
    assert version["assets"]
    for asset in version["assets"]:
        assert (tmp_path / asset["path"]).is_file()


def test_mcp_bridge_uses_real_tool_router(tmp_path):
    db = Database(tmp_path / "library.sqlite3")
    v = db.save_document("Source", "markdown", "notes/source.md", [{"text": "Evidence", "locator": {"line": 1}}])
    app = create_app(db=db, data_dir=tmp_path, token="app-test", tool_token="tool-test", credentials=CredentialStore(tmp_path))
    class Transport(httpx.BaseTransport):
        def handle_request(self, request):
            response = client.request(request.method, request.url.path, headers=dict(request.headers), content=request.content)
            return httpx.Response(response.status_code, headers=response.headers, content=response.content)
    with TestClient(app) as client, patch.object(bridge, "_discover", return_value={"url": "http://127.0.0.1:12345", "token": "tool-test"}):
        transport = Transport()
        material = bridge._tool_get_material([v["document_id"]], _transport=transport)
        assert material["blocks"][0]["text"] == "Evidence"
        deck = bridge._tool_save_deck("Slides", "# Evidence", [material["blocks"][0]["id"]], _transport=transport)
        ack = bridge._tool_export_deck(deck["id"], _transport=transport)
        assert ack["job_id"]
        assert bridge._tool_get_job(ack["job_id"], _transport=transport)["kind"] == "slide_export"
        assert client.get("/settings", headers={"Authorization": "Bearer tool-test"}).status_code == 401


def test_real_bundled_export_has_selectable_pdf_and_offline_image(tmp_path):
    import pytest
    root = Path(__file__).resolve().parents[1]
    tools = root / "resources/tools"
    if not (tools / "browser.json").exists():
        pytest.skip("Run npm run build:tools for bundled export acceptance")
    browser = json.loads((tools / "browser.json").read_text())["executable"]
    db = Database(tmp_path / "library.sqlite3")
    worker = Worker(db, tmp_path)
    note = completed(worker, "note", {"title": "Lecture source", "content": "Referenced lecture evidence"})
    block = db.get_document(note["document_id"])["blocks"][0]
    deck = db.save_deck("Actual export", "# Actual lecture\n\nSelectable scientific text and $E=mc^2$\n\n---\n\n# Evidence", [block["id"]])
    env = {"MYNOTES_NODE": str(tools / "node"), "MYNOTES_MARP": str(tools / "marp/node_modules/@marp-team/marp-cli/marp-cli.js"), "MYNOTES_CHROMIUM": str(tools / browser)}
    with patch.dict(os.environ, env):
        result = completed(worker, "slide_export", {"deck_id": deck["id"], "revision": deck["revision"]})
    with pdfium.PdfDocument(tmp_path / result["pdf_path"]) as pdf:
        text = " ".join(page.get_textpage().get_text_range() for page in pdf)
        assert "Selectable scientific text" in text
        assert "Lecture source" in text
        assert len(pdf) >= 3
    manifest = json.loads((tmp_path / result["manifest_path"]).read_text())
    assert manifest["citations"][0]["source_id"] == block["id"]
    assert manifest["citations"][0]["version_id"] == block["version_id"]
    assert (tmp_path / result["html_path"]).is_file()
