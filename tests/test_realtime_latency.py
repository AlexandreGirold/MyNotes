import asyncio
import json
import threading
from contextlib import contextmanager

from websockets.asyncio.server import serve

from test_realtime import client_for


@contextmanager
def rapid_provider(engine_error=False):
    ready, quit_event = threading.Event(), threading.Event()
    ports = []
    async def handle(ws):
        await ws.send(json.dumps({"type": "session.created"}))
        await ws.recv(); await ws.recv(); await ws.recv()
        await ws.send(json.dumps({"type": "transcription.delta", "delta": "First. "}))
        await ws.send(json.dumps({"type": "transcription.delta", "delta": "Second."}))
        if engine_error:
            await ws.send(json.dumps({"type": "error", "error": {"message": "EngineCore encountered an issue"}}))
        else:
            await ws.recv()
            await ws.send(json.dumps({"type": "transcription.done"}))
    async def run():
        async with serve(handle, "127.0.0.1", 0) as server:
            ports.append(server.sockets[0].getsockname()[1]); ready.set()
            await asyncio.to_thread(quit_event.wait)
    thread = threading.Thread(target=lambda: asyncio.run(run()), daemon=True)
    thread.start(); assert ready.wait(5)
    try:
        yield f"http://127.0.0.1:{ports[0]}/v1"
    finally:
        quit_event.set(); thread.join(5)


def test_slow_checkpoint_does_not_delay_incoming_deltas(tmp_path, monkeypatch):
    with rapid_provider() as base:
        db, client = client_for(tmp_path, base)
        blocked, release = threading.Event(), threading.Event()
        save = db.save_document
        def slow_save(*args, **kwargs):
            blocked.set(); assert release.wait(5)
            return save(*args, **kwargs)
        monkeypatch.setattr(db, "save_document", slow_save)
        try:
            with client, client.websocket_connect("/live-transcription") as ws:
                ws.send_json({"token": "app-test"}); assert ws.receive_json()["type"] == "ready"
                ws.send_bytes(b"\0\0")
                assert ws.receive_json()["delta"] == "First. "
                assert blocked.wait(2)
                # This would deadlock until release with the former awaited-save implementation.
                assert ws.receive_json()["delta"] == "Second."
                release.set(); ws.send_json({"type": "stop"})
                assert ws.receive_json()["text"] == "First. Second."
        finally:
            release.set()


def test_enginecore_failure_is_identified_and_received_text_is_saved(tmp_path):
    with rapid_provider(engine_error=True) as base:
        db, client = client_for(tmp_path, base)
        with client, client.websocket_connect("/live-transcription") as ws:
            ws.send_json({"token": "app-test"}); ws.receive_json()
            ws.send_bytes(b"\0\0"); ws.receive_json(); ws.receive_json()
            error = ws.receive_json()
            assert "Cloud Temple" in error["message"] and "EngineCore" in error["message"]
            saved = ws.receive_json()
            assert saved["type"] == "saved" and saved["text"] == "First. Second."
        assert db.get_document(saved["document_id"])["blocks"][0]["text"] == saved["text"]
