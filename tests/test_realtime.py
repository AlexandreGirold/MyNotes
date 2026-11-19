"""Exercise the actual local and upstream WebSocket protocol without a cloud key."""
import asyncio
import base64
import json
import threading
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from websockets.asyncio.server import serve

from mynotes.api import create_app
from mynotes.db import Database
from mynotes.realtime import realtime_url
from mynotes.security import CredentialStore


@contextmanager
def provider(disconnect=False, text="A live sentence."):
    ready, stop = threading.Event(), threading.Event()
    captured = []
    address = []

    async def handle(socket):
        assert socket.request.headers["Authorization"] == "Bearer placeholder-test-key"
        await socket.send(json.dumps({"type": "session.created", "id": "test"}))
        captured.append(json.loads(await socket.recv()))
        captured.append(json.loads(await socket.recv()))
        audio = json.loads(await socket.recv())
        captured.append(audio)
        await socket.send(json.dumps({"type": "transcription.delta", "delta": text}))
        if disconnect:
            await socket.close()
            return
        captured.append(json.loads(await socket.recv()))
        await socket.send(json.dumps({"type": "transcription.done"}))

    async def run():
        async with serve(handle, "127.0.0.1", 0) as server:
            address.append(server.sockets[0].getsockname()[1])
            ready.set()
            await asyncio.to_thread(stop.wait)

    thread = threading.Thread(target=lambda: asyncio.run(run()), daemon=True)
    thread.start()
    assert ready.wait(5)
    try:
        yield f"http://127.0.0.1:{address[0]}/v1", captured
    finally:
        stop.set(); thread.join(5)
        assert not thread.is_alive()


def client_for(tmp_path, base=None, key=True):
    db = Database(tmp_path / "library.sqlite3")
    credentials = CredentialStore(tmp_path)
    if key:
        credentials.set("placeholder-test-key")
    if base:
        db.set_settings({"base_url": base, "realtime_model": "voxtral"})
    app = create_app(db=db, data_dir=tmp_path, token="app-test", tool_token="tools-test", credentials=credentials)
    return db, TestClient(app)


@pytest.mark.parametrize("initial_text", ["", "Earlier notes."])
def test_live_delta_arrives_before_stop_and_only_text_is_saved(tmp_path, initial_text):
    with provider() as (base, captured):
        db, client = client_for(tmp_path, base)
        with client, client.websocket_connect("/live-transcription") as socket:
            socket.send_json({"token": "app-test", "title": "My meeting", "initial_text": initial_text})
            assert socket.receive_json()["type"] == "ready"
            audio = b"\x01\x00\x02\x00"
            socket.send_bytes(audio)
            delta = socket.receive_json()
            addition = ("\n\n" if initial_text else "") + "A live sentence."
            assert delta == {"type": "delta", "delta": addition}
            assert "placeholder-test-key" not in json.dumps(delta)
            socket.send_json({"type": "stop"})
            done = socket.receive_json()
            assert done["type"] == "done"
            assert done["text"] == initial_text + addition
            document = db.get_document(done["document_id"])
            assert (tmp_path / document["original_path"]).read_text() == done["text"]
        assert captured[0] == {"type": "session.update", "model": "voxtral"}
        assert base64.b64decode(captured[2]["audio"]) == audio
        assert captured[3] == {"type": "input_audio_buffer.commit", "final": True}
        assert not list(tmp_path.rglob("*.wav")) and not list(tmp_path.rglob("*.webm"))


def test_missing_key_is_explicit_and_wrong_app_token_is_rejected(tmp_path):
    db, client = client_for(tmp_path, key=False)
    with client, client.websocket_connect("/live-transcription") as socket:
        socket.send_json({"token": "app-test"})
        assert "API key" in socket.receive_json()["message"]
    with client, client.websocket_connect("/live-transcription") as socket:
        socket.send_json({"token": "wrong"})
        with pytest.raises(WebSocketDisconnect):
            socket.receive_json()
    assert db.list_documents() == []


@pytest.mark.parametrize("initial_text", [["not a transcript"], "x" * 2000001])
def test_invalid_existing_transcript_is_rejected_without_saving(tmp_path, initial_text):
    db, client = client_for(tmp_path)
    with client, client.websocket_connect("/live-transcription") as socket:
        socket.send_json({"token": "app-test", "initial_text": initial_text})
        assert socket.receive_json() == {"type": "error", "message": "Invalid existing transcript"}
    assert db.list_documents() == []


def test_upstream_disconnect_preserves_partial_transcript(tmp_path):
    with provider(disconnect=True) as (base, _):
        db, client = client_for(tmp_path, base)
        with client, client.websocket_connect("/live-transcription") as socket:
            socket.send_json({"token": "app-test"})
            assert socket.receive_json()["type"] == "ready"
            socket.send_bytes(b"\x00\x00")
            assert socket.receive_json()["type"] == "delta"
            assert socket.receive_json()["type"] == "error"
            assert socket.receive_json()["type"] == "saved"
        assert db.get_document(db.list_documents()[0]["id"])["blocks"][0]["text"] == "A live sentence."


def test_provider_url_preserves_v1_and_requires_tls():
    assert realtime_url("https://api.ai.cloud-temple.com/v1") == "wss://api.ai.cloud-temple.com/v1/realtime"
    with pytest.raises(ValueError):
        realtime_url("http://untrusted.example/v1")
