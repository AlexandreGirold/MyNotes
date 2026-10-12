"""Authenticated Voxtral PCM relay; optional temporary audio for local speaker labels."""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import secrets
import sqlite3
import time
import uuid
import wave
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from fastapi import WebSocket, WebSocketDisconnect
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from mynotes.diarization import MAX_SECONDS, model_status
from mynotes.provider import DEFAULT_SETTINGS


def realtime_url(base: str) -> str:
    parsed = urlparse(base)
    local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Invalid provider base URL")
    if parsed.scheme != "https" and not (local and parsed.scheme == "http"):
        raise ValueError("Realtime connections require HTTPS")
    prefix = parsed.path.rstrip("/")
    path = prefix + "/realtime" if prefix.endswith("/v1") else prefix + "/v1/realtime"
    return urlunparse(("ws" if local and parsed.scheme == "http" else "wss", parsed.netloc, path, "", "", ""))


async def stream_transcript(socket: WebSocket, db, data_dir: Path, credentials, token: str):
    if socket.headers.get("origin") not in {None, "null", "file://", "http://localhost:5173", "http://127.0.0.1:5173"}:
        await socket.close(code=1008)
        return
    await socket.accept()
    text, saved_text = "", ""
    document_id, version_id = None, None
    tasks = []
    session_id = uuid.uuid4().hex
    title, language, num_speakers = "Live meeting notes", "auto", 0
    last_save = 0.0
    authenticated, completed = False, False
    audio, audio_path, pcm_bytes = None, None, 0
    checkpoint = None

    def persist():
        nonlocal document_id, version_id, saved_text
        if not text.strip() or text == saved_text:
            return
        folder = data_dir / "notes" / "live" / session_id
        folder.mkdir(parents=True, exist_ok=True)
        file = folder / f"{uuid.uuid4().hex}.md"
        snapshot = text
        file.write_text(snapshot, encoding="utf-8")
        version = db.save_document(title, "markdown", str(file.relative_to(data_dir)),
            [{"text": snapshot, "origin": "transcribed", "locator": {"file": file.name, "session_id": session_id}}],
            document_id=document_id, warnings=["Live transcript; review recognition errors. " +
                ("Temporary audio captured for local speaker detection." if audio_path else "No audio file was saved.")])
        document_id, version_id = version["document_id"], version["id"]
        saved_text = snapshot

    async def send_error(message):
        with contextlib.suppress(WebSocketDisconnect, RuntimeError):
            await socket.send_json({"type": "error", "message": message})

    try:
        auth = await asyncio.wait_for(socket.receive_json(), 10)
        if not isinstance(auth, dict) or not secrets.compare_digest(str(auth.get("token", "")), token):
            await socket.close(code=1008)
            return
        authenticated = True
        title = str(auth.get("title") or title)[:200]
        if type(auth.get("diarization", False)) is not bool:
            raise ValueError("Speaker detection option must be true or false")
        if auth.get("diarization"):
            language = auth.get("language", "auto")
            num_speakers = auth.get("num_speakers", 0)
            if language not in {"auto", "fr", "en"} or type(num_speakers) is not int or not 0 <= num_speakers <= 10:
                raise ValueError("Invalid speaker detection options")
            if not model_status(data_dir / "models" / "diarization")["ready"]:
                raise ValueError("Download speaker models before enabling speaker detection")
            folder = data_dir / "temporary-audio"
            folder.mkdir(mode=0o700, parents=True, exist_ok=True)
            audio_path = folder / f"{session_id}.wav"
            audio = wave.open(str(audio_path), "wb")  # noqa: SIM115 — closed in async session finally
            audio_path.chmod(0o600)
            audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        key = credentials.get()
        if not key:
            raise ValueError("Add your Cloud Temple API key in Settings before starting live transcription.")
        settings = {**DEFAULT_SETTINGS, **db.get_settings()}
        if not settings.get("verified_realtime"):
            raise ValueError("Enable the documented Voxtral realtime endpoint in Settings.")
        async with connect(realtime_url(settings["base_url"]), additional_headers={"Authorization": f"Bearer {key}"},
                            open_timeout=15, close_timeout=5, max_size=1048576, max_queue=8) as upstream:
            created = json.loads(await asyncio.wait_for(upstream.recv(), 15))
            if created.get("type") != "session.created":
                raise ValueError("Cloud Temple did not start a Voxtral session.")
            await upstream.send(json.dumps({"type": "session.update", "model": settings["realtime_model"]}))
            await upstream.send(json.dumps({"type": "input_audio_buffer.commit"}))
            await socket.send_json({"type": "ready", "model": settings["realtime_model"], "sample_rate": 16000})

            async def upload():
                nonlocal pcm_bytes
                while True:
                    event = await socket.receive()
                    if event["type"] == "websocket.disconnect":
                        raise WebSocketDisconnect()
                    chunk = event.get("bytes")
                    if chunk is not None:
                        if not chunk or len(chunk) > 64000 or len(chunk) % 2:
                            raise ValueError("Invalid PCM audio chunk")
                        if audio:
                            if pcm_bytes + len(chunk) > MAX_SECONDS * 32000:
                                raise ValueError("Speaker detection supports sessions up to four hours; received text is preserved")
                            audio.writeframesraw(chunk)
                            pcm_bytes += len(chunk)
                        await upstream.send(json.dumps({"type": "input_audio_buffer.append", "audio": base64.b64encode(chunk).decode()}))
                    elif event.get("text"):
                        kind = json.loads(event["text"]).get("type")
                        if kind in {"pause", "resume"}:
                            await socket.send_json({"type": "paused" if kind == "pause" else "resumed"})
                            continue
                        if kind != "stop":
                            raise ValueError("Unexpected live transcription message")
                        await upstream.send(json.dumps({"type": "input_audio_buffer.commit", "final": True}))
                        return

            async def download():
                nonlocal text, last_save, checkpoint
                async for raw in upstream:
                    event = json.loads(raw)
                    if not isinstance(event, dict):
                        raise TypeError("Invalid Voxtral response")
                    kind = event.get("type")
                    if kind == "transcription.delta":
                        delta = event.get("delta", "")
                        if not isinstance(delta, str) or len(text) + len(delta) > 2000000:
                            raise ValueError("Transcript exceeded the supported session size")
                        text += delta
                        await socket.send_json({"type": "delta", "delta": delta})
                        if time.monotonic() - last_save > 5 and (checkpoint is None or checkpoint.done()):
                            if checkpoint:
                                checkpoint.result()
                            checkpoint = asyncio.create_task(asyncio.to_thread(persist))
                            last_save = time.monotonic()
                    elif kind == "transcription.done":
                        return
                    elif kind == "error":
                        error = event.get("error", {})
                        message = error.get("message", "Voxtral rejected the audio session") if isinstance(error, dict) else str(error)
                        message = message.replace(key, "[redacted]")
                        if "EngineCore" in message:
                            message = "Cloud Temple's transcription engine stopped (EngineCore). Received text is preserved. Server logs are needed to diagnose the cause; start a new session to retry."
                        raise ValueError(message)
                raise ValueError("Voxtral disconnected before finalizing the transcript")

            tasks = [asyncio.create_task(upload()), asyncio.create_task(download())]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
            if tasks[0] in done:
                await asyncio.wait_for(tasks[1], 30)
            completed = True
    except WebSocketDisconnect:
        pass
    except ValueError as error:
        await send_error(str(error))
    except (KeyError, TypeError, TimeoutError, OSError, WebSocketException):
        await send_error("Live transcription could not finish. Check your API key, Voxtral model access and connection; received text is preserved.")
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if checkpoint:
            try:
                await checkpoint
            except (OSError, ValueError, sqlite3.Error):
                await send_error("A recovery checkpoint failed; attempting to save the final transcript.")
        if audio:
            audio.close()
        job_id = None
        if authenticated and text.strip():
            await asyncio.to_thread(persist)
            if audio_path and pcm_bytes:
                job = db.create_job("diarization", {"session_id": session_id,
                    "audio_path": str(audio_path.relative_to(data_dir)), "transcript_document_id": document_id,
                    "transcript_version_id": version_id, "title": title, "language": language,
                    "num_speakers": num_speakers})
                job_id = job["id"]
            with contextlib.suppress(WebSocketDisconnect, RuntimeError):
                await socket.send_json({"type": "done" if completed else "saved", "text": text,
                    "document_id": document_id, "session_id": session_id, "diarization_job_id": job_id})
        elif audio_path:
            if pcm_bytes:
                await send_error("No transcript was received. Captured audio was retained for recovery in "
                                 + str(audio_path.relative_to(data_dir)))
            else:
                audio_path.unlink(missing_ok=True)
        with contextlib.suppress(WebSocketDisconnect, RuntimeError):
            await socket.close()
