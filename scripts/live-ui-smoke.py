"""Run Electron's real microphone/summary UI against a local provider simulator."""
import json
import os
import socket
import subprocess
import tempfile
import threading
import shutil
from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket
from mynotes.provider import REALTIME_MODEL_ID

app = FastAPI()
observed = {"audio": 0, "model": "", "web": False, "files": False}


@app.websocket("/v1/realtime")
async def realtime(ws: WebSocket):
    assert ws.headers.get("authorization") == "Bearer placeholder-test-key"
    await ws.accept()
    await ws.send_json({"type": "session.created"})
    assert (await ws.receive_json())["model"] == REALTIME_MODEL_ID
    assert (await ws.receive_json())["type"] == "input_audio_buffer.commit"
    while True:
        event = await ws.receive_json()
        if event["type"] == "input_audio_buffer.append":
            observed["audio"] += 1
            if observed["audio"] == 1:
                await ws.send_json({"type": "transcription.delta", "delta": "Live sentence: we decided to test the workflow."})
            elif observed["audio"] == 3:
                await ws.send_json({"type": "transcription.delta", "delta": " Second live sentence arrives while the page is hidden."})
        elif event.get("final"):
            await ws.send_json({"type": "transcription.done"})
            break


@app.get("/v1/models")
async def models():
    # The public catalogue uses the alias; realtime requires the engine ID.
    return {"data": [{"id": "voxtral"}, {"id": "gpt-oss:120b"}]}


@app.post("/v1/chat/completions")
async def chat(body: dict):
    if "Allowed laptop files" in json.dumps(body["messages"]):
        evidence = json.loads(body["messages"][0]["content"].split("Material:\n", 1)[1])
        assert "CANONICAL_FILE_SIGNAL" in evidence[0]["text"]
        observed["files"] = True
        return {"choices": [{"message": {"content": f"Local file source read: 42. [source:{evidence[0]['source_id']}]"}}]}
    observed["model"] = body["model"]
    if "Public Internet research" in json.dumps(body["messages"]):
        assert "qwen3.6:27b" in json.dumps(body["messages"])
        observed["web"] = True
        return {"choices": [{"message": {"content": "Public Internet source read. [source:https://www.cloud-temple.com/en/products/large-language-model-as-a-service-llmaas/]"}}]}
    assert "Live sentence" in json.dumps(body["messages"])
    assert "PERSONAL_NOTE_SIGNAL" in json.dumps(body["messages"])
    if os.environ.get("MYNOTES_SPEAKER_SMOKE_TEST"):
        assert "Speaker unknown" in json.dumps(body["messages"])  # Fake mic audio differs from simulator text.
    return {"choices": [{"message": {"content": "## Meeting summary\n\nWe decided to test the workflow.\n\n## Action items\n\n- Run acceptance tests."}}]}


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    test_data = tempfile.mkdtemp(prefix="mynotes-live-ui-", dir="/tmp/opencode")
    files = Path(test_data) / "files"
    files.mkdir()
    for i in range(32):
        (files / f"Project {i + 1:02}").mkdir()
    (files / "Project 01" / "note.md").write_text("## Canonical file\n\nCANONICAL_FILE_SIGNAL: 42")
    if os.environ.get("MYNOTES_SPEAKER_SMOKE_TEST"):
        shutil.copytree(os.environ.get("MYNOTES_SPEAKER_MODELS", "/tmp/opencode/mynotes-speaker-models"),
                        Path(test_data) / "data" / "models" / "diarization")
    env = {**os.environ, "MYNOTES_LIVE_SMOKE_TEST": "1", "MYNOTES_TEST_PROVIDER_URL": f"http://127.0.0.1:{port}/v1",
           "MYNOTES_TEST_DATA_DIR": test_data, "MYNOTES_SAVE_TEST_PATH": str(Path(test_data) / "Chosen session name.md"),
           "MYNOTES_SCREENSHOT": "/tmp/opencode/mynotes-live-ui.png"}
    try:
        executable = root / "out/MyNotes-linux-x64/MyNotes"
        command = [str(executable)] if os.environ.get("MYNOTES_PACKAGED_TEST") else [str(root / "node_modules/electron/dist/electron"), "."]
        try:
            result = subprocess.run(command, cwd=root, env=env, text=True, capture_output=True, timeout=90, check=False)
        except subprocess.TimeoutExpired as error:
            print((error.stdout or b"").decode() if isinstance(error.stdout, bytes) else error.stdout)
            print((error.stderr or b"").decode() if isinstance(error.stderr, bytes) else error.stderr)
            raise
        print(result.stdout)
        print(result.stderr)
        assert result.returncode == 0
        assert "Live desktop UI acceptance passed" in result.stdout
        assert observed["audio"] > 0 and observed["model"] == "gpt-oss:120b"
        assert observed["files"]
        if os.environ.get("MYNOTES_WEB_SMOKE_TEST"):
            assert observed["web"]
        assert "Second live sentence" in (Path(test_data) / "Chosen session name.md").read_text()
    finally:
        server.should_exit = True
        thread.join(5)
        shutil.rmtree(test_data, ignore_errors=True)
