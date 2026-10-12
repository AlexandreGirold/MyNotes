import json
import subprocess
import wave

import pytest

from mynotes.diarization import MODEL_FILES
from mynotes.worker import Worker
from test_realtime import client_for, provider


def ready_models(root):
    for name in MODEL_FILES:
        path = root / "models" / "diarization" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"model")


def test_capture_is_closed_before_job_is_announced_and_raw_version_is_preserved(tmp_path):
    ready_models(tmp_path)
    with provider() as (base, _):
        db, client = client_for(tmp_path, base)
        with client:
            client.app.state.worker.stop()
            with client.websocket_connect("/live-transcription") as ws:
                ws.send_json({"token": "app-test", "diarization": True, "language": "fr", "num_speakers": 2})
                assert ws.receive_json()["type"] == "ready"
                pcm = b"\x01\x00\x02\x00"
                ws.send_bytes(pcm)
                assert ws.receive_json()["type"] == "delta"
                ws.send_json({"type": "stop"})
                done = ws.receive_json()
                assert done["type"] == "done" and done["diarization_job_id"]
                job = db.get_job(done["diarization_job_id"])
                payload = json.loads(job["payload"])
                with wave.open(str(tmp_path / payload["audio_path"]), "rb") as wav:
                    assert wav.getframerate() == 16000 and wav.readframes(2) == pcm
                version = db.get_version(payload["transcript_version_id"])
                assert version["document_id"] == done["document_id"]
                assert version["blocks"][0]["text"] == "A live sentence."


def test_speaker_request_without_models_fails_before_audio_or_provider(tmp_path):
    db, client = client_for(tmp_path)
    with client, client.websocket_connect("/live-transcription") as ws:
        ws.send_json({"token": "app-test", "diarization": True})
        assert "Download speaker models" in ws.receive_json()["message"]
    assert not list(tmp_path.rglob("*.wav"))
    assert not db.list_jobs(kind="diarization")


def test_provider_returns_no_text_but_captured_audio_is_not_lost(tmp_path):
    ready_models(tmp_path)
    with provider(text="") as (base, _):
        _, client = client_for(tmp_path, base)
        with client, client.websocket_connect("/live-transcription") as ws:
            ws.send_json({"token": "app-test", "diarization": True})
            assert ws.receive_json()["type"] == "ready"
            ws.send_bytes(b"\x01\x00")
            assert ws.receive_json()["type"] == "delta"
            ws.send_json({"type": "stop"})
            assert "audio was retained" in ws.receive_json()["message"]
        captured = list((tmp_path / "temporary-audio").glob("*.wav"))
        assert len(captured) == 1
        with wave.open(str(captured[0]), "rb") as audio:
            assert audio.readframes(1) == b"\x01\x00"


def test_worker_keeps_raw_and_retains_audio_on_failure_then_deletes_after_success(tmp_path, monkeypatch):
    db, _ = client_for(tmp_path)
    raw_file = tmp_path / "notes" / "raw.md"
    raw_file.parent.mkdir()
    raw_file.write_text("Bonjour. Non.")
    raw = db.save_document("Discussion", "markdown", "notes/raw.md", [{"text": raw_file.read_text()}])
    session = "a" * 32
    audio = tmp_path / "temporary-audio" / (session + ".wav")
    audio.parent.mkdir()
    audio.write_bytes(b"retained audio")
    payload = {"session_id": session, "transcript_document_id": raw["document_id"],
               "transcript_version_id": raw["id"], "title": "Discussion"}
    worker = Worker(db, tmp_path)
    job = db.create_job("diarization", payload)
    class Child:
        returncode = 1
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def communicate(self, *a, **kw): return "", "test failure"
    monkeypatch.setattr(subprocess, "Popen", Child)
    assert worker.run_once()["status"] == "failed"
    assert audio.exists()
    def complete(self, raw, **kwargs):
        options = json.loads(raw)
        from pathlib import Path
        Path(options["output_path"]).write_text(json.dumps({"text": "Speaker 1: Bonjour.\nSpeaker 2: Non.",
                                                          "segments": [], "warnings": []}))
        return "", ""
    Child.communicate = complete
    Child.returncode = 0
    db.retry_job(job["id"])
    result = worker.run_once()
    assert result["status"] == "succeeded" and not audio.exists()
    assert db.get_version(raw["id"])["blocks"][0]["text"] == "Bonjour. Non."
    derived = json.loads(result["result"])["document_id"]
    assert derived != raw["document_id"]
    assert "Speaker 2" in db.get_document(derived)["blocks"][0]["text"]
    with pytest.raises(ValueError, match="Invalid audio session"):
        worker._process_diarization({**payload, "session_id": "../credentials"}, tmp_path, job["id"])
