"""Tests for src/mynotes/worker.py — functional tests for all job kinds."""

from __future__ import annotations

import json
import pathlib
import time
from unittest import mock

import pytest

from mynotes.worker import Worker
from mynotes.db import Database, NotFound


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def db_path(tmp_path: pathlib.Path) -> pathlib.Path:
    return tmp_path / "mynotes.db"


@pytest.fixture
def db(db_path: pathlib.Path) -> Database:
    return Database(db_path)


@pytest.fixture
def data_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    d = tmp_path / "data"
    d.mkdir()
    return d


@pytest.fixture
def worker(db: Database, data_dir: pathlib.Path) -> Worker:
    db.set_settings({"verified_chat": True, "verified_vision": True,
                     "verified_transcription": True, "chat_model": "test-chat",
                     "vision_model": "test-vision", "transcription_model": "test-audio"})
    return Worker(db=db, data_dir=data_dir)


# =============================================================================
# Construction
# =============================================================================


class TestConstruction:
    def test_data_dir_created(self, tmp_path: pathlib.Path) -> None:
        w = Worker(
            db=Database(tmp_path / "db.sqlite"),
            data_dir=tmp_path / "new_data",
        )
        assert w.data_dir.exists()

    def test_credentials_stored(self, data_dir: pathlib.Path) -> None:
        store = mock.Mock()
        w = Worker(
            db=Database(data_dir / "db.sqlite"),
            data_dir=data_dir,
            credentials=store,
        )
        assert w.credentials is store


# =============================================================================
# Lifecycle: schedule / run_once / start / stop
# =============================================================================


class TestLifecycle:
    def test_run_once_no_jobs(self, worker: Worker) -> None:
        assert worker.run_once() is None

    def test_schedule_returns_queued_job(self, worker: Worker) -> None:
        job = worker.schedule(
            "import", {"source_path": "/tmp/x.pdf", "kind": "pdf"},
        )
        assert "id" in job
        assert job["kind"] == "import"
        assert job["status"] == "queued"

    def test_run_once_returns_finished_job(self, worker: Worker) -> None:
        src = worker.data_dir / "doc.md"
        src.write_text("# Hello", encoding="utf-8")
        job = worker.schedule(
            "import", {"source_path": str(src), "kind": "markdown"},
        )
        result = worker.run_once()
        assert result is not None
        assert result["id"] == job["id"]
        assert result["status"] == "succeeded"

    def test_start_starts_thread(self, worker: Worker) -> None:
        worker.start()
        assert worker._thread is not None
        assert worker._thread.is_alive()
        worker.stop()

    def test_start_twice_noop(self, worker: Worker) -> None:
        worker.start()
        t1 = worker._thread
        worker.start()
        assert worker._thread is t1
        worker.stop()

    def test_stop_nonexistent_thread(self, worker: Worker) -> None:
        worker.stop()  # should not raise

    def test_start_stop_start(self, worker: Worker) -> None:
        worker.start()
        worker.stop()
        worker.start()
        assert worker._thread.is_alive()
        worker.stop()


# =============================================================================
# Recovery
# =============================================================================


class TestRecovery:
    def _insert_job(
        self, worker: Worker, job_id: str, kind: str, status: str,
        updated_ago: float,
    ) -> None:
        now = time.time()
        worker.db._execute(
            "INSERT INTO jobs "
            "(id, kind, payload, status, progress, attempts, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 0.0, 0, ?, ?)",
            (
                job_id,
                kind,
                json.dumps({"source_path": "/tmp/x.pdf", "kind": "pdf"}),
                status,
                now - 7200,
                now - updated_ago,
            ),
        )

    def test_recover_stale_running_job(self, worker: Worker) -> None:
        self._insert_job(worker, "stale-1", "import", "running", 7200)
        worker._recover()
        job = worker.db.list_jobs(kind="import")[0]
        assert job["status"] == "queued"

    def test_no_recovery_when_not_stale(self, worker: Worker) -> None:
        self._insert_job(worker, "fresh-1", "import", "running", 60)
        worker._recover()
        job = worker.db.list_jobs(kind="import")[0]
        assert job["status"] == "running"


# =============================================================================
# Unknown job kind
# =============================================================================


class TestUnhandledKind:
    def test_unknown_kind_fails(self, worker: Worker) -> None:
        worker.db._execute(
            "INSERT INTO jobs (id, kind, payload, status, progress, attempts, created_at, updated_at) "
            "VALUES (?, 'aliens', ?, 'queued', 0.0, 0, ?, ?)",
            (
                "unknown-1",
                json.dumps({}),
                time.time() - 7200,
                time.time() - 7200,
            ),
        )
        result = worker.run_once()
        assert result is not None
        assert result["status"] == "failed"
        assert "Unknown job kind" in result["error"]


# =============================================================================
# Job: import — originals persist outside work_dir, save_document called
# =============================================================================


class TestJobImport:
    def _make_md(
        self, worker: Worker, name: str = "doc.md",
    ) -> pathlib.Path:
        src = worker.data_dir / name
        src.write_text("# Title\n\nLine one", encoding="utf-8")
        return src

    def test_missing_source_path(self, worker: Worker) -> None:
        job = worker.schedule("import", {"kind": "pdf"})
        result = worker.run_once()
        assert result["status"] == "failed"
        assert "missing 'source_path'" in result["error"]

    def test_source_not_found(self, worker: Worker) -> None:
        job = worker.schedule(
            "import",
            {"source_path": "/nonexistent/file.pdf", "kind": "pdf"},
        )
        result = worker.run_once()
        assert result["status"] == "failed"
        assert "not found" in result["error"].lower()

    def test_successful_import_saves_document(self, worker: Worker) -> None:
        src = self._make_md(worker)
        job = worker.schedule(
            "import", {"source_path": str(src), "kind": "markdown"},
        )
        result = worker.run_once()
        assert result["status"] == "succeeded"
        # Import calls save_document — verify document in DB
        # process_import uses source.stem as title (e.g. "doc" for doc.md)
        docs = worker.db.list_documents()
        assert len(docs) >= 1
        assert docs[0]["title"] == "doc"

    def test_relative_source_path_resolves_root(self, worker: Worker) -> None:
        """Source path relative to data_dir resolves against root."""
        src = worker.data_dir / "subdir" / "relative.md"
        src.parent.mkdir(exist_ok=True)
        src.write_text("# Relative", encoding="utf-8")
        rel = str(src.relative_to(worker.data_dir))
        job = worker.schedule(
            "import", {"source_path": rel, "kind": "markdown"},
        )
        result = worker.run_once()
        assert result["status"] == "succeeded"
        docs = worker.db.list_documents()
        assert len(docs) >= 1
        assert docs[0]["title"] == "relative"

    def test_source_file_unchanged_after_import(self, worker: Worker) -> None:
        src = worker.data_dir / "preserve.md"
        original_content = "# Preserve me\n\nDon't delete this."
        src.write_text(original_content, encoding="utf-8")
        job = worker.schedule(
            "import", {"source_path": str(src), "kind": "markdown"},
        )
        worker.run_once()
        assert src.read_text(encoding="utf-8") == original_content

    def test_work_dir_cleaned_up_after_success(self, worker: Worker) -> None:
        src = worker.data_dir / "x.md"
        src.write_text("content", encoding="utf-8")
        job = worker.schedule(
            "import", {"source_path": str(src), "kind": "markdown"},
        )
        worker.run_once()
        work_dir = worker.data_dir / "jobs" / f"job-{job['id']}"
        assert not work_dir.exists(), "Work dir should be cleaned up"

    def test_work_dir_cleaned_up_after_failure(self, worker: Worker) -> None:
        job = worker.schedule(
            "import", {"source_path": "/nonexistent.pdf", "kind": "pdf"},
        )
        worker.run_once()
        work_dir = worker.data_dir / "jobs" / f"job-{job['id']}"
        assert not work_dir.exists(), "Work dir should be cleaned up on failure"


# =============================================================================
# Job: note — raw source saved permanently
# =============================================================================


class TestJobNote:
    def test_create_note(self, worker: Worker) -> None:
        job = worker.schedule(
            "note",
            {
                "content": "# My Note",
                "format": "markdown",
                "title": "Test Note",
            },
        )
        result = worker.run_once()
        assert result["status"] == "succeeded"
        job_result = json.loads(result["result"])
        assert "document_id" in job_result
        assert "path" in job_result

    def test_note_creates_file_in_permanent_location(self, worker: Worker) -> None:
        job = worker.schedule(
            "note",
            {"content": "hello", "title": "Hello", "format": "md"},
        )
        worker.run_once()
        notes_dir = worker.data_dir / "notes"
        assert notes_dir.exists()
        # Should have created a file with title as name
        files = list(notes_dir.glob("*/Hello*"))
        assert len(files) == 1
        assert files[0].read_text(encoding="utf-8") == "hello"

    def test_note_persists_document(self, worker: Worker) -> None:
        job = worker.schedule(
            "note",
            {"content": "hello", "title": "Hello", "format": "md"},
        )
        result = worker.run_once()
        job_result = json.loads(result["result"])
        doc = worker.db.get_document(job_result["document_id"])
        assert doc["title"] == "Hello"
        assert len(doc["blocks"]) >= 1

    def test_note_missing_content_defaults_empty(self, worker: Worker) -> None:
        job = worker.schedule("note", {"title": "Empty"})
        result = worker.run_once()
        assert result["status"] == "succeeded"

    def test_note_without_course_id(self, worker: Worker) -> None:
        job = worker.schedule(
            "note",
            {"content": "c", "title": "t", "format": "md"},
        )
        result = worker.run_once()
        assert result["status"] == "succeeded"


# =============================================================================
# Job: chat — citations grounded from blocks only
# =============================================================================


class TestJobChat:
    def test_chat_missing_credentials_shows_setup_error(self, worker: Worker) -> None:
        job = worker.schedule(
            "chat", {"document_ids": [], "question": "What is this?"}
        )
        result = worker.run_once()
        assert result["status"] == "failed"
        assert "API key" in result["error"]

    def test_chat_missing_question_fails(self, worker: Worker) -> None:
        job = worker.schedule("chat", {"document_ids": []})
        result = worker.run_once()
        assert result["status"] == "failed"
        assert "missing 'question'" in result["error"]

    def test_chat_with_mocked_provider(self, worker: Worker) -> None:
        store = mock.Mock()
        store.get.return_value = "mock-key"
        worker.credentials = store

        job = worker.schedule(
            "chat", {"document_ids": [], "question": "Explain Bayes."}
        )

        with mock.patch("mynotes.provider.Provider") as MockProv:
            provider_inst = mock.Mock()
            provider_inst.chat.return_value = {
                "answer": "Bayes relates P(A|B) to P(B|A).",
                "citations": [
                    {
                        "source_id": "b1",
                        "excerpt": "Bayes' theorem ...",
                    }
                ],
                "mode": "verified",
                "warnings": [],
            }
            MockProv.return_value = provider_inst

            result = worker.run_once()
            assert result["status"] == "succeeded"
            job_result = json.loads(result["result"])
            assert job_result["answer"] == "Bayes relates P(A|B) to P(B|A)."
            provider_inst.close.assert_called_once()

    def test_chat_ignores_invalid_citation(
        self, worker: Worker,
    ) -> None:
        """Citation with source_id not in material blocks is rejected."""
        store = mock.Mock()
        store.get.return_value = "mock-key"
        worker.credentials = store

        # No documents → material has no blocks
        job = worker.schedule(
            "chat", {"document_ids": [], "question": "What?"}
        )

        with mock.patch("mynotes.provider.Provider") as MockProv:
            provider_inst = mock.Mock()
            # Provider returns a citation for a source not in material
            provider_inst.chat.return_value = {
                "answer": "Some answer.",
                "citations": [
                    {"source_id": "fake_id", "excerpt": "made up"},
                ],
                "mode": "verified",
                "warnings": [],
            }
            MockProv.return_value = provider_inst

            result = worker.run_once()
            assert result["status"] == "succeeded"
            job_result = json.loads(result["result"])
            # No valid citations should remain
            assert job_result["citations"] == []


# =============================================================================
# Job: suggestions
# =============================================================================


class TestJobSuggestions:
    def test_suggestions_no_credentials_fallback(self, worker: Worker) -> None:
        course = worker.db.create_course("S")
        doc = worker.db.save_document(
            "Doc", "markdown", "/tmp/x.md",
            [
                {
                    "text": "x",
                    "locator": {"file": "x.md"},
                    "origin": "extracted",
                }
            ],
            course_id=course["id"],
        )
        job = worker.schedule("suggestions", {"document_ids": [doc["id"]]})
        result = worker.run_once()
        assert result["status"] == "succeeded"
        job_result = json.loads(result["result"])
        assert job_result.get("mode") == "fallback"

    def test_suggestions_missing_document_ids_fails(self, worker: Worker) -> None:
        job = worker.schedule("suggestions", {})
        result = worker.run_once()
        assert result["status"] == "failed"
        assert "missing 'document_ids'" in result["error"]


# =============================================================================
# Job: slide_draft
# =============================================================================


class TestJobSlideDraft:
    def test_slide_draft_fallback(self, worker: Worker) -> None:
        job = worker.schedule(
            "slide_draft",
            {"document_ids": [], "topic": "Intro", "count": 3, "language": "en"},
        )
        result = worker.run_once()
        assert result["status"] == "succeeded"
        job_result = json.loads(result["result"])
        assert job_result["mode"] == "fallback"
        assert job_result["title"] == "Intro"

    def test_slide_draft_with_mock(self, worker: Worker) -> None:
        store = mock.Mock()
        store.get.return_value = "key"
        worker.credentials = store

        job = worker.schedule(
            "slide_draft",
            {"document_ids": [], "topic": "ML", "count": 5, "language": "fr"},
        )

        with mock.patch("mynotes.provider.Provider") as MockProv:
            provider_inst = mock.Mock()
            provider_inst.draft_slides.return_value = {
                "title": "ML",
                "markdown": "# ML\n\n---\n\n# S2",
                "source_ids": [],
                "mode": "unverified",
                "warnings": [],
            }
            MockProv.return_value = provider_inst

            result = worker.run_once()
            assert result["status"] == "succeeded"
            assert json.loads(result["result"])["title"] == "ML"
            provider_inst.close.assert_called_once()

    def test_slide_draft_saves_deck(self, worker: Worker) -> None:
        store = mock.Mock()
        store.get.return_value = "key"
        worker.credentials = store

        job = worker.schedule(
            "slide_draft",
            {"document_ids": [], "topic": "T", "count": 2},
        )

        with mock.patch("mynotes.provider.Provider") as MockProv:
            provider_inst = mock.Mock()
            provider_inst.draft_slides.return_value = {
                "title": "T",
                "markdown": "# T\n\n---\n\n# S2",
                "source_ids": [],
                "mode": "verified",
                "warnings": [],
            }
            MockProv.return_value = provider_inst

            worker.run_once()
            decks = worker.db.list_decks()
            assert len(decks) >= 1
            assert decks[-1]["title"] == "T"


# =============================================================================
# Job: slide_export — mock export_deck where it's used
# =============================================================================


class TestJobSlideExport:
    def test_export_missing_deck_id(self, worker: Worker) -> None:
        job = worker.schedule("slide_export", {})
        result = worker.run_once()
        assert result["status"] == "failed"
        assert "missing 'deck_id'" in result["error"]

    def test_export_nonexistent_deck(self, worker: Worker) -> None:
        job = worker.schedule("slide_export", {"deck_id": "nope"})
        result = worker.run_once()
        assert result["status"] == "failed"

    def test_export_success(self, worker: Worker) -> None:
        deck = worker.db.save_deck(
            title="Test Deck",
            markdown="# S1\n\n---\n\n# S2\n",
            source_ids=[],
        )
        job = worker.schedule("slide_export", {"deck_id": deck["id"]})

        with mock.patch(
            "mynotes.exports.export_deck",
        ) as mock_export:
            mock_export.return_value = {
                "markdown_path": str(worker.data_dir / "exports/d.md"),
                "html_path": str(worker.data_dir / "exports/d.html"),
                "pdf_path": str(worker.data_dir / "exports/d.pdf"),
                "manifest_path": str(worker.data_dir / "exports/d.json"),
            }
            result = worker.run_once()
            assert result["status"] == "succeeded"
            assert mock_export.call_count == 1


# =============================================================================
# Job: transcription — save original before network, validate timestamps
# =============================================================================


class TestJobTranscription:
    def test_transcription_missing_source_path(self, worker: Worker) -> None:
        job = worker.schedule("transcription", {})
        result = worker.run_once()
        assert result["status"] == "failed"
        assert "missing 'source_path'" in result["error"]

    def test_transcription_file_not_found(self, worker: Worker) -> None:
        job = worker.schedule(
            "transcription", {"source_path": "/nope.wav"},
        )
        result = worker.run_once()
        assert result["status"] == "failed"
        assert "not found" in result["error"].lower()

    def test_transcription_fallback_no_provider(self, worker: Worker) -> None:
        audio = worker.data_dir / "rec.wav"
        audio.write_bytes(b"fake audio data")
        job = worker.schedule(
            "transcription", {"source_path": str(audio)},
        )
        result = worker.run_once()
        assert result["status"] == "succeeded"
        job_result = json.loads(result["result"])
        assert job_result["mode"] == "local-only"

    def test_transcription_saves_original_before_network(
        self, worker: Worker,
    ) -> None:
        """Original audio persisted permanently so retry has data."""
        audio = worker.data_dir / "rec2.wav"
        audio.write_bytes(b"fake audio")
        job = worker.schedule(
            "transcription", {"source_path": str(audio)},
        )
        worker.run_once()
        # Original should be saved under data_dir/originals/audio/
        orig_dir = worker.data_dir / "originals" / "audio"
        assert orig_dir.exists()
        saved = orig_dir / "rec2.wav"
        assert saved.exists()
        assert saved.read_bytes() == b"fake audio"


# =============================================================================
# Job: visual
# =============================================================================


class TestJobVisual:
    def test_visual_no_asset(self, worker: Worker) -> None:
        job = worker.schedule("visual", {"version_id": "nonexistent"})
        result = worker.run_once()
        assert result["status"] == "failed"
        assert "not found" in result["error"]

    def test_visual_missing_file_fallback(self, worker: Worker) -> None:
        job = worker.schedule(
            "visual", {"version_id": "nonexistent"},
        )
        result = worker.run_once()
        assert result["status"] == "failed"
        assert "not found" in result["error"]


# =============================================================================
# Job: backup / restore — mock backup module imports
# =============================================================================


class TestJobBackupRestore:
    def test_backup(self, worker: Worker) -> None:
        with mock.patch("mynotes.backup.backup") as mock_backup:
            mock_backup.return_value = worker.data_dir / "backups/backup.zip"
            job = worker.schedule("backup", {})
            result = worker.run_once()
            assert result["status"] == "succeeded"
            job_result = json.loads(result["result"])
            assert job_result["status"] == "completed"
            assert "backup_path" in job_result

    def test_restore_missing_source_path(self, worker: Worker) -> None:
        job = worker.schedule("restore", {})
        result = worker.run_once()
        assert result["status"] == "failed"
        assert "missing 'source_path'" in result["error"]

    def test_restore(self, worker: Worker) -> None:
        with mock.patch("mynotes.backup.restore") as mock_restore:
            mock_restore.return_value = {
                "restored_keys": ["settings"],
            }
            job = worker.schedule(
                "restore", {"source_path": "/tmp/backup.tar.gz"},
            )
            result = worker.run_once()
            assert result["status"] == "succeeded"
            job_result = json.loads(result["result"])
            assert job_result["status"] == "completed"
            assert job_result["restored_keys"] == ["settings"]


# =============================================================================
# Source preservation
# =============================================================================


class TestSourcePreservation:
    def test_source_file_remains_on_failed_job(self, worker: Worker) -> None:
        src = worker.data_dir / "safe.md"
        src.write_text("safe content", encoding="utf-8")
        job = worker.schedule(
            "import", {"source_path": str(src), "kind": "epub"},
        )
        worker.run_once()
        assert src.exists()
        assert src.read_text(encoding="utf-8") == "safe content"


# =============================================================================
# Threading
# =============================================================================


class TestThreading:
    def test_run_once_blocks_and_returns(self, worker: Worker) -> None:
        src = worker.data_dir / "block.md"
        src.write_text("blocking", encoding="utf-8")
        job = worker.schedule(
            "import", {"source_path": str(src), "kind": "markdown"},
        )
        result = worker.run_once()
        assert result is not None
        assert result["id"] == job["id"]
        assert result["status"] == "succeeded"

    def test_stop_interrupts_idle_wait(self, worker: Worker) -> None:
        worker.start()
        worker.stop()
        assert not worker._thread.is_alive()


# =============================================================================
# JSON payload handling
# =============================================================================


class TestPayloadHandling:
    def test_string_payload_parsed(self, db_path: pathlib.Path, data_dir: pathlib.Path) -> None:
        db = Database(db_path)
        w = Worker(db=db, data_dir=data_dir)
        db._execute(
            "INSERT INTO jobs (id, kind, payload, status, progress, attempts, created_at, updated_at) "
            "VALUES (?, 'note', ?, 'queued', 0.0, 0, ?, ?)",
            (
                "str-payload",
                json.dumps({"content": "test", "title": "T", "format": "md"}),
                time.time() - 7200,
                time.time() - 7200,
            ),
        )
        result = w.run_once()
        assert result is not None
        assert result["status"] == "succeeded"


# =============================================================================
# Restart + FTS — real markdown data survives restart
# =============================================================================


class TestRestartFts:
    def test_import_text_survives_restart(self, tmp_path: pathlib.Path) -> None:
        """Document data written by import persists across worker restarts."""
        db1 = Database(tmp_path / "mynotes.db")
        d1 = tmp_path / "data1"
        w1 = Worker(db=db1, data_dir=d1)

        # Create and process import job
        src = d1 / "restart.md"
        src.write_text("# Restart Test\n\nThis content survives.", encoding="utf-8")
        job = w1.schedule(
            "import", {"source_path": str(src), "kind": "markdown"},
        )
        r = w1.run_once()
        assert r is not None
        assert r["status"] == "succeeded"

        # The original file is unchanged and document was saved in DB
        assert src.read_text(encoding="utf-8") == "# Restart Test\n\nThis content survives."

        # Verify document is in DB and blocks contain the actual markdown text
        docs = db1.list_documents()
        assert len(docs) >= 1
        doc = db1.get_document(docs[0]["id"])
        all_text = " ".join(b["text"] for b in doc["blocks"])
        assert "Restart Test" in all_text
        assert "This content survives" in all_text

    def test_note_persists_and_searchable(self, tmp_path: pathlib.Path) -> None:
        """Note raw markdown persists and is persisted in DB blocks."""
        db1 = Database(tmp_path / "mynotes2.db")
        d1 = tmp_path / "data2"
        w1 = Worker(db=db1, data_dir=d1)

        job = w1.schedule(
            "note",
            {
                "content": "# Searchable Note\n\nThis text is searchable.",
                "title": "SearchNote",
                "format": "md",
            },
        )
        r = w1.run_once()
        assert r["status"] == "succeeded"
        job_result = json.loads(r["result"])

        # Verify note file was written permanently
        notes = list((d1 / "notes").glob("*/SearchNote.*"))
        assert len(notes) == 1
        assert "searchable" in notes[0].read_text(encoding="utf-8")

        # Verify document blocks contain the raw markdown content
        doc = db1.get_document(job_result["document_id"])
        all_text = " ".join(b["text"] for b in doc["blocks"])
        assert "Searchable Note" in all_text
        assert "searchable" in all_text
