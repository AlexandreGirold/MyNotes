import json
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from mynotes.api import create_app
from mynotes.db import Database
from mynotes.security import CredentialStore
from mynotes.worker import Worker


def test_isolated_conversations_history_sessions_and_summary_separation(tmp_path):
    db = Database(tmp_path / "library.sqlite3")
    first, second = db.create_conversation(), db.create_conversation()
    db.save_chat("First question", "First answer", [], "global", first["id"], "ses_first")
    db.save_chat("Other topic", "Other answer", [], "global", second["id"], "ses_other")
    credentials = Mock()
    credentials.get.return_value = "placeholder"
    worker = Worker(db=db, data_dir=tmp_path, credentials=credentials)
    provider = Mock()
    provider.general_chat.return_value = {"answer": "Followup", "citations": [], "warnings": []}
    with patch.object(worker, "_provider", return_value=provider):
        worker.schedule("chat", {"question": "Continue", "mode": "global", "conversation_id": first["id"]})
        assert worker.run_once()["status"] == "succeeded"
    history = provider.general_chat.call_args.kwargs["history"]
    assert history == [{"role": "user", "content": "First question"}, {"role": "assistant", "content": "First answer"}]
    # Starting direct chat clears that thread's stale harness session only.
    assert db.get_conversation(first["id"])["session_id"] is None
    assert db.get_conversation(second["id"])["session_id"] == "ses_other"
    agent = Mock(return_value={"answer": "Agent followup", "citations": [], "warnings": [], "session_id": "ses_other"})
    with patch.object(worker, "_provider", return_value=provider), patch("mynotes.agent_harness.run_agent", agent):
        worker.schedule("chat", {"question": "Continue agent", "mode": "global", "harness": True, "conversation_id": second["id"], "session_id": "ses_first"})
        assert worker.run_once()["status"] == "succeeded"
    assert agent.call_args.args[2]["session_id"] == "ses_other"
    provider.summarize.return_value = {"answer": "Audio summary", "citations": [], "warnings": []}
    with patch.object(worker, "_provider", return_value=provider):
        worker.schedule("chat", {"question": "Summarize audio", "mode": "audio_summary", "conversation_id": first["id"]})
        assert worker.run_once()["status"] == "succeeded"
    assert "Audio summary" not in json.dumps(db.list_chats())
    assert [m["answer"] for m in db.list_chats(first["id"])] == ["First answer", "Followup"]
    reopened = Database(tmp_path / "library.sqlite3")
    assert reopened.get_conversation(first["id"])["title"] == "First question"
    assert len(reopened.list_conversations()) == 2


def test_existing_history_migrates_without_losing_summary_records(tmp_path):
    path = tmp_path / "library.sqlite3"
    db = Database(path)
    db.save_chat("Assistant question", "Assistant answer", [], "global")
    db.save_chat("Summarize", "Old summary", [], "full")
    job = db.create_job("chat", {"mode": "audio_summary", "question": "Summarize"})
    db.finish_job(job["id"], {"answer": "Old summary"})
    # Reproduce the previous schema, then exercise the real startup migration twice.
    db._execute("DROP INDEX chats_conversation")
    db._execute("ALTER TABLE chats DROP COLUMN conversation_id")
    reopened = Database(path)
    conversations = reopened.list_conversations()
    assert len(conversations) == 1
    assert [m["answer"] for m in reopened.list_chats(conversations[0]["id"])] == ["Assistant answer"]
    assert reopened._one("SELECT answer FROM chats WHERE mode='audio_summary'")["answer"] == "Old summary"
    assert len(Database(path).list_conversations()) == 1


def test_conversation_api_validates_scope_and_keeps_summary_jobs_unthreaded(tmp_path):
    db = Database(tmp_path / "library.sqlite3")
    app = create_app(db=db, data_dir=tmp_path, token="test", tool_token="tools", credentials=CredentialStore(tmp_path))
    with TestClient(app) as client:
        assert client.post("/conversations").status_code == 401
        client.headers.update({"Authorization": "Bearer test"})
        created = client.post("/conversations")
        assert created.status_code == 201
        cid = created.json()["id"]
        assert client.get("/chats", params={"conversation_id": cid}).json() == []
        assert client.post("/chat", json={"question": "Hi", "mode": "global", "conversation_id": "a" * 32}).status_code == 404
        assert client.get("/chats", params={"conversation_id": "a" * 32}).status_code == 404
        ack = client.post("/chat", json={"question": "Summary", "mode": "audio_summary", "conversation_id": cid}).json()
        assert json.loads(db.get_job(ack["job_id"])["payload"])["conversation_id"] is None


def test_deleting_a_conversation_removes_its_turns_and_preserves_other_threads(tmp_path):
    import pytest
    from mynotes.db import Conflict, NotFound
    db = Database(tmp_path / "library.sqlite3")
    first, second = db.create_conversation(), db.create_conversation()
    db.save_chat("Delete this", "First answer", [], "global", first["id"])
    db.save_chat("Keep this", "Other answer", [], "global", second["id"])
    pending = db.create_job("chat", {"conversation_id": first["id"]})
    with pytest.raises(Conflict):
        db.delete_conversation(first["id"])
    assert len(db.list_chats(first["id"])) == 1
    db.finish_job(pending["id"], {})
    db.delete_conversation(first["id"])
    with pytest.raises(NotFound):
        db.get_conversation(first["id"])
    assert [c["id"] for c in db.list_conversations()] == [second["id"]]
    assert [m["answer"] for m in db.list_chats()] == ["Other answer"]


def test_delete_conversation_endpoint(tmp_path):
    db = Database(tmp_path / "library.sqlite3")
    conversation = db.create_conversation()
    app = create_app(db=db, data_dir=tmp_path, token="test", tool_token="tools", credentials=CredentialStore(tmp_path))
    with TestClient(app) as client:
        route = "/conversations/" + conversation["id"]
        assert client.delete(route).status_code == 401
        client.headers.update({"Authorization": "Bearer test"})
        assert client.delete(route).status_code == 200
        assert client.delete(route).status_code == 404
