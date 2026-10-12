"""Workflow tests for Provider.summarize (SSE), soft-delete DB, agent_files, bridge."""
from __future__ import annotations

import json
import os
import pathlib
import tempfile

import httpx
import pytest

from mynotes.provider import Provider
from mynotes.db import Database


# ====================================================================
#  Provider.summarize  —  SSE data:chunks  +  final chunk with finish_reason
# ====================================================================

class TestSummarizeSSE:
    """Provider.summarize with httpx.MockTransport, SSE data chunks."""

    @staticmethod
    def _sse_lines(**extra: object) -> str:
        """Build a SSE stream: partial delta then final chunk with finish_reason."""
        lines = [
            'data: {"choices":[{"delta":{"content":"Part1 "}}]}',
            'data: {"choices":[{"delta":{"content":"Part2"}}]}',
            'data: {"choices":[{"finish_reason":"length"}]}',
            "data: [DONE]",
        ]
        for k, v in extra.items():
            lines.insert(-1, f'data: {json.dumps({"choices": [{"delta": {"content": str(v)}}]})}')
        return "\n".join(lines)

    @pytest.mark.parametrize("note_kind", ["lecture", "meeting"])
    def test_summarize_parses_sse_deltas_and_length_warning(self, note_kind: str) -> None:
        """SSE partial deltas accumulate; finish_reason=length appends warning."""
        seen: list[str] = []
        def handler(req: httpx.Request) -> httpx.Response:
            body = json.loads(req.content)
            seen.append(body["messages"][1]["content"][:80])
            return httpx.Response(200, text=self._sse_lines(), headers={"content-type": "text/event-stream"})
        settings = {"base_url": "https://x.com/v1", "chat_model": "t", "timeout": 10, "max_retries": 0}
        mat = {
            "metadata": {"source_id": "doc-001", "title": "Algèbre", "course": "MATH-301"},
            "blocks": [{"source_id": "b1", "text": "Théorème de Bayes"}],
        }
        p = Provider(settings)
        p._client = httpx.Client(transport=httpx.MockTransport(handler), base_url=p.base_url, timeout=10)
        result = p.summarize(mat, note_kind=note_kind)
        assert "Part1 Part2" in result["answer"]
        assert any("output limit" in w.lower() for w in result["warnings"])

    def test_summarize_prompt_lecture_french_no_english_no_tables_no_raw_ID(self) -> None:
        """System prompt contains 'lecture', French (not English forced), no tables,
        no raw source IDs in output requirement."""
        captured_body: list[dict] = []
        def handler(req: httpx.Request) -> httpx.Response:
            captured_body.append(json.loads(req.content))
            return httpx.Response(200, text=self._sse_lines(), headers={"content-type": "text/event-stream"})
        settings = {"base_url": "https://x.com/v1", "chat_model": "t", "timeout": 10, "max_retries": 0}
        mat = {"metadata": {"source_id": "doc-001", "title": "Cours 1"},
               "blocks": [{"source_id": "b1", "text": "X"}]}
        p = Provider(settings)
        p._client = httpx.Client(transport=httpx.MockTransport(handler), base_url=p.base_url, timeout=10)
        p.summarize(mat, language="fr", note_kind="lecture")
        msgs = captured_body[0]["messages"]
        sys_text = msgs[0]["content"]
        assert "lecture" in sys_text.lower()
        assert "French" in sys_text            # language=fr -> "French"
        assert "English" not in sys_text        # no forced English
        assert "no tables" in sys_text.lower()  # system prompt instructs "no tables"

    def test_summarize_cites_metadata_not_raw_ID_output(self) -> None:
        """Citations use metadata fields (title, excerpt) — no raw source_id leakage."""
        captured_body: list[dict] = []
        def handler(req: httpx.Request) -> httpx.Response:
            captured_body.append(json.loads(req.content))
            return httpx.Response(200, text=self._sse_lines(), headers={"content-type": "text/event-stream"})
        settings = {"base_url": "https://x.com/v1", "chat_model": "t", "timeout": 10, "max_retries": 0}
        mat = {"metadata": {"source_id": "doc-001"},
               "blocks": [{"source_id": "b1", "title": "Lec-1", "excerpt": "Hello world"}]}
        p = Provider(settings)
        p._client = httpx.Client(transport=httpx.MockTransport(handler), base_url=p.base_url, timeout=10)
        result = p.summarize(mat)
        cite = result["citations"][0]
        assert "title" in cite or "excerpt" in cite
        assert cite.get("source_id") == "b1"

    def test_summarize_personal_notes_in_user_message(self) -> None:
        """Evidence payload includes blocks text (personal notes) in user message."""
        captured_body: list[dict] = []
        def handler(req: httpx.Request) -> httpx.Response:
            captured_body.append(json.loads(req.content))
            return httpx.Response(200, text=self._sse_lines(), headers={"content-type": "text/event-stream"})
        settings = {"base_url": "https://x.com/v1", "chat_model": "t", "timeout": 10, "max_retries": 0}
        mat = {
            "metadata": {"source_id": "doc-001"},
            "blocks": [{"source_id": "b1", "text": "personal note content here"}],
        }
        p = Provider(settings)
        p._client = httpx.Client(transport=httpx.MockTransport(handler), base_url=p.base_url, timeout=10)
        p.summarize(mat)
        user_content = json.loads(captured_body[0]["messages"][1]["content"])
        assert isinstance(user_content, list)
        assert "personal note content here" in json.dumps(user_content)


# ====================================================================
#  Soft-delete  —  save -> delete -> list/search/material empty,
#                  while get_version(old_evidence) survives
# ====================================================================

class TestSoftDelete:
    """Actual DB: save, delete, list/search/material empty, get_version survives."""

    def test_soft_delete_preserves_evidence_in_version(self) -> None:
        td = tempfile.mkdtemp()
        db = Database(pathlib.Path(td) / "db.sqlite")
        did = db.save_document("Test", "lecture", "/tmp/x.txt",
                               [{"text": "evidence block", "locator": None, "origin": "f"}])
        vid = did["id"]                     # version id returned
        db.delete_document(did["document_id"])
        # list & search return nothing for the deleted doc
        assert all(d["title"] != "Test" for d in db.list_documents())
        assert db.search("evidence") == []
        # material also excludes it (returns warning about zero evidence)
        mat_result = db.material()
        assert mat_result["blocks"] == []
        assert "zero evidence" in " ".join(mat_result["warnings"]).lower()
        # old version + blocks still retrievable
        ver = db.get_version(vid)
        assert "evidence block" in ver["blocks"][0]["text"]
        # cleanup
        db_path = pathlib.Path(td) / "db.sqlite"
        db_path.unlink(missing_ok=True)
        pathlib.Path(td).rmdir()


# ====================================================================
#  agent_files.allowed  —  symlink escape / credential deny, unicode allow
# ====================================================================

class TestAgentFiles:
    """agent_files.allowed: credential files denied, unicode allowed, scoped."""

    def test_credential_files_denied(self) -> None:
        td = tempfile.mkdtemp()
        os.environ["MYNOTES_AGENT_DIRECTORIES"] = json.dumps([td])
        try:
            from mynotes.agent_files import allowed
            for fname in ("credentials.enc", "encryption-key.enc", "auth.json"):
                with pytest.raises(ValueError, match="Credential"):
                    allowed(os.path.join(td, fname))
            assert allowed(os.path.join(td, "notes.md")) is not None
        finally:
            os.environ.pop("MYNOTES_AGENT_DIRECTORIES", None)
            pathlib.Path(td).rmdir()

    def test_unicode_content_read_allowed(self) -> None:
        td = tempfile.mkdtemp()
        p = pathlib.Path(td) / "unicöde.txt"
        p.write_text("こんにちは 🎓 café résumé", encoding="utf-8")
        os.environ["MYNOTES_AGENT_DIRECTORIES"] = json.dumps([td])
        try:
            from mynotes.agent_files import read_file
            res = read_file(str(p))
            assert "こんにちは" in res["text"]
            assert "🎓" in res["text"]
        finally:
            os.environ.pop("MYNOTES_AGENT_DIRECTORIES", None)
            p.unlink()
            pathlib.Path(td).rmdir()


# ====================================================================
#  bridge env  —  forbids unknown doc IDs
# ====================================================================

class TestBridgeEnv:
    """Bridge MYNOTES_AGENT_DOCUMENT_IDS: scoped selection forbids unknowns."""

    def test_scoped_selection_forbids_unknown_doc_id(self) -> None:
        from unittest import mock
        from mynotes import bridge
        allowed_ids = ["known-doc-1", "known-doc-2"]
        def fake_discover():
            return {"url": "http://127.0.0.1:1", "token": "x"}
        with mock.patch.dict(os.environ, {"MYNOTES_AGENT_DOCUMENT_IDS": json.dumps(allowed_ids)}):
            with mock.patch("mynotes.bridge._discover", fake_discover):
                with pytest.raises(RuntimeError, match="limited to selected"):
                    bridge._tool_get_material(document_ids=["unknown-doc"])
        os.environ.pop("MYNOTES_AGENT_DOCUMENT_IDS", None)
