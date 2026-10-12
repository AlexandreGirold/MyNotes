from unittest.mock import Mock, patch

import pytest

from mynotes.agent_files import research_files
from mynotes.db import Database
from mynotes.worker import Worker


def test_finds_relevant_filename_beyond_first_200_entries_and_reads_real_evidence(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    for i in range(220):
        (root / f"unrelated-{i}.md").write_text("Other topic")
    actual = root / "BeBlood-finances.md"
    actual.write_text("BeBlood revenue: 123 EUR.\nSource: original ledger.")
    (root / "BeBlood.env.key").write_text("placeholder secret")
    outside = tmp_path / "BeBlood-outside.md"
    outside.write_text("outside evidence")
    (root / "BeBlood-symlink.md").symlink_to(outside)
    material = research_files("Look for relevant files about BeBlood numbers", [str(root)])
    assert [b["source_id"] for b in material["blocks"]] == [str(actual)]
    assert "123 EUR" in material["blocks"][0]["text"]
    assert "outside evidence" not in str(material) and "placeholder secret" not in str(material)


def test_content_fallback_and_missing_root_errors(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    (root / "lecture.md").write_text("Beblood has a pending funding round.")
    assert "pending funding" in research_files("Beblood", [str(root)])["blocks"][0]["text"]
    with pytest.raises(ValueError, match="allowed folders"):
        research_files("Beblood", [])
    with pytest.raises(ValueError, match="No readable matching"):
        research_files("Nothingmatches", [str(root)])


def test_file_chat_reads_allowed_files_even_without_native_model_tools(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    file = root / "Beblood.md"
    file.write_text("FILE_RESEARCH_SIGNAL: revenue 123 EUR")
    db = Database(tmp_path / "library.sqlite3")
    db.set_settings({"agent_directories": [str(root)]})
    credentials = Mock()
    credentials.get.return_value = "placeholder"
    provider = Mock()
    provider.chat.return_value = {"answer": "123 EUR", "citations": [{"source_id": str(file)}], "warnings": []}
    worker = Worker(db=db, data_dir=tmp_path, credentials=credentials)
    with patch.object(worker, "_provider", return_value=provider):
        worker.schedule("chat", {"mode": "files", "question": "Beblood", "harness": True})
        result = worker.run_once()
    assert result["status"] == "succeeded"
    assert "FILE_RESEARCH_SIGNAL" in provider.chat.call_args.kwargs["material"]["blocks"][0]["text"]
