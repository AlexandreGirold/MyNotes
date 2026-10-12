"""MyNotes FastAPI — root paths, 202 Accepted for mutations.

Lifespan creates one Worker(db, data_dir, credentials), calls .start() on
startup, .stop() on shutdown.  Long-running ops create a DB job and return
202 immediately; request handlers never block on actual work.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import secrets
import shutil
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import PurePath
from typing import Annotated, Any, Literal
from urllib.parse import urlparse

import httpx
from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    WebSocket,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from mynotes.db import Conflict, Database, NotFound
from mynotes.provider import DEFAULT_SETTINGS
from mynotes.security import CredentialStore
from mynotes.worker import Worker

logger = logging.getLogger(__name__)

MAX_UPLOAD_SIZE = 200 * 1024 * 1024  # 200 MB
_ALLOWED_PATH_PREFIXES = (
    "originals", "notes", "assets", "derived", "exports",
    "recordings", "transcriptions", "backups", "uploads",
)
_KIND_MAP: dict[str, str] = {
    ".pdf": "pdf", ".md": "markdown", ".markdown": "markdown",
    ".html": "html", ".htm": "html", ".typ": "typst", ".typst": "typst",
    ".pptx": "pptx",
}
_PROVIDER_DEFAULTS = DEFAULT_SETTINGS

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class CourseCreate(BaseModel):
    name: str


class DeckCreate(BaseModel):
    title: str
    markdown: str
    source_ids: list[str] = []


class DeckUpdate(BaseModel):
    expected_revision: int
    title: str | None = None
    markdown: str | None = None
    source_ids: list[str] | None = None


class DocumentUpdate(BaseModel):
    title: str | None = None
    course_id: str | None = None
    chapter: str | None = None
    class_date: str | None = None


class MaterialInput(BaseModel):
    document_ids: list[str] = []
    course_id: str | None = None
    query: str = ""
    budget: int = 24000


class SlideExportInput(BaseModel):
    deck_id: str
    revision: int


class SlideDraftInput(BaseModel):
    document_ids: list[str] = []
    course_id: str | None = None
    topic: str
    count: int = 10
    language: str = "English"


class ChatInput(BaseModel):
    document_ids: list[str] = []
    course_id: str | None = None
    question: str
    mode: Literal["documents", "global", "web", "files", "audio_summary", "document_summary", "transcript_cleanup"] = "documents"
    note_kind: Literal["lecture", "meeting"] = "lecture"
    language: Literal["auto", "fr", "en"] = "auto"
    glossary: str = Field(default="", max_length=2000)
    harness: bool = False
    session_id: str | None = None
    conversation_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")


class SuggestionInput(BaseModel):
    document_ids: list[str] = []
    course_id: str | None = None


class VisualInput(BaseModel):
    version_id: str
    page: int | None = None
    asset: str | None = None
    question: str


class NoteCreate(BaseModel):
    title: str
    content: str = ""
    format: str = "markdown"
    course_id: str | None = None


class NoteUpdate(BaseModel):
    title: str | None = None
    content: str | None = None
    format: str | None = None
    course_id: str | None = None


class SettingsUpdate(BaseModel):
    base_url: str | None = None
    chat_model: str | None = None
    fast_model: str | None = None
    global_chat_model: str | None = None
    audio_summary_model: str | None = None
    document_summary_model: str | None = None
    timeout: float | None = Field(default=None, gt=0, le=600)
    chat_path: str | None = None
    transcription_path: str | None = None
    context_budget: int | None = None
    verified_chat: bool | None = None
    verified_transcription: bool | None = None
    verified_vision: bool | None = None
    api_key: str | None = None
    vision_path: str | None = None
    transcription_model: str | None = None
    vision_model: str | None = None
    realtime_model: str | None = None
    verified_realtime: bool | None = None
    agent_directories: list[str] | None = None
    chat_harness: bool | None = None


class WorkspaceSave(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    text: str = Field(max_length=2 * 1024 * 1024)
    expected_sha256: str | None = Field(default=None, pattern="^[a-fA-F0-9]{64}$")


class JobAck(BaseModel):
    job_id: str
    status_url: str
    status: str


class ToolExportInput(BaseModel):
    revision: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ct(a: str, b: str) -> bool:
    return secrets.compare_digest(a.encode(), b.encode())


def _get_bearer_token(r: Request) -> str | None:
    auth = r.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        t = auth[7:].strip()
        return t if t else None
    return None


def _safe_file(data_root: pathlib.Path, path: str) -> pathlib.Path | None:
    """Return a safe, allowlisted file path or None."""
    if not path:
        return None
    parts = PurePath(path).parts
    if not parts:
        return None
    first = parts[0]
    if first not in _ALLOWED_PATH_PREFIXES:
        return None
    resolved = (data_root / path).resolve()
    if not resolved.is_relative_to(data_root.resolve()):
        return None
    n = resolved.name.lower()
    if n.startswith(("credentials", "runtime")):
        return None
    if any(n.endswith(s) for s in (".db", ".sqlite", ".sqlite3", ".key", ".enc")):
        return None
    return resolved if resolved.is_file() else None


def _sanitize_filename(name: str) -> str:
    """Sanitize a filename: extract leaf, strip nulls/slashes."""
    name = PurePath(name).name
    name = name.replace("\x00", "")
    name = name.replace("/", "_").replace("\\", "_")
    name = name.strip(" .")
    return name if name else "unnamed"


def _infer_kind(filename: str) -> str | None:
    return _KIND_MAP.get(PurePath(filename).suffix.lower())


def _ack(j: dict[str, Any]) -> JobAck:
    return JobAck(job_id=j["id"], status_url=f"/jobs/{j['id']}", status=j["status"])


# ---------------------------------------------------------------------------
# Lifespan — single Worker lifecycle with try/finally cleanup
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    db: Database = app.state.db
    w = Worker(db, app.state.data_root, app.state.credentials)
    try:
        db.recover_jobs()
        w.start()
        app.state.worker = w
        yield
    finally:
        w.stop()
        app.state.worker = None


# ---------------------------------------------------------------------------
# create_app
# ---------------------------------------------------------------------------


def create_app(
    db: Database,
    data_dir: pathlib.Path,
    token: str,
    tool_token: str,
    credentials: CredentialStore,
    runtime_file: pathlib.Path | None = None,
) -> FastAPI:
    dr = pathlib.Path(data_dir).resolve()
    dr.mkdir(parents=True, exist_ok=True)

    app = FastAPI(title="MyNotes", version="0.1.0", lifespan=_lifespan)
    app.state.db = db
    app.state.data_root = dr
    app.state.credentials = credentials
    @app.websocket("/live-transcription")
    async def live_transcription(socket: WebSocket):
        from mynotes.realtime import stream_transcript
        await stream_transcript(socket, db, dr, credentials, token)

    app.add_middleware(
        CORSMiddleware, allow_origins=["http://localhost:5173", "null"],
        allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
    )

    def _auth(r: Request) -> None:
        if not _get_bearer_token(r) or not _ct(_get_bearer_token(r), token):
            raise HTTPException(401, "Unauthorized")

    def _auth_tool(r: Request) -> None:
        if not _get_bearer_token(r) or not _ct(_get_bearer_token(r), tool_token):
            raise HTTPException(403, "Forbidden")

    # === App router ===
    rtr = APIRouter(dependencies=[Depends(_auth)])

    @rtr.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @rtr.get("/courses")
    def list_courses() -> list[dict]:
        return [{"id": c["id"], "name": c["name"]} for c in db.list_courses()]

    @rtr.post("/courses", status_code=201)
    def create_course(b: CourseCreate) -> dict:
        c = db.create_course(b.name)
        return {"id": c["id"], "name": c["name"]}

    @rtr.delete("/courses/{course_id}", status_code=204)
    def delete_course(course_id: str) -> None:
        try:
            db.delete_course(course_id)
        except NotFound:
            raise HTTPException(404, "Course not found")

    @rtr.get("/documents")
    def list_documents(course_id: str | None = Query(None)) -> list[dict]:
        return [{"id": d["id"], "title": d["title"], "kind": d["kind"],
                 "course_id": d.get("course_id"), "chapter": d.get("chapter"),
                 "class_date": d.get("class_date"), "created_at": d["created_at"],
                 "current_version_id": d["current_version_id"]}
                for d in db.list_documents(course_id=course_id)]

    def workspace_error(error):
        from mynotes.workspace import WorkspaceConflict
        if isinstance(error, WorkspaceConflict):
            return HTTPException(409, str(error))
        if isinstance(error, FileNotFoundError):
            return HTTPException(404, "File no longer exists")
        return HTTPException(400, str(error))

    @rtr.get("/workspace/entries")
    def workspace_entries(path: str = "", offset: int = Query(0, ge=0)) -> dict:
        from mynotes.workspace import list_directory
        try:
            return list_directory(path, db.get_settings().get("agent_directories", []), offset)
        except (ValueError, OSError) as error:
            raise workspace_error(error) from error

    @rtr.get("/workspace/text")
    def workspace_text(path: str) -> dict:
        from mynotes.workspace import read_text
        try:
            return read_text(path, db.get_settings().get("agent_directories", []))
        except (ValueError, OSError) as error:
            raise workspace_error(error) from error

    @rtr.put("/workspace/text")
    def workspace_save(body: WorkspaceSave) -> dict:
        from mynotes.workspace import atomic_write
        try:
            return atomic_write(body.path, body.text, db.get_settings().get("agent_directories", []), body.expected_sha256)
        except (ValueError, OSError) as error:
            raise workspace_error(error) from error

    @rtr.get("/workspace/file")
    def workspace_file(path: str) -> FileResponse:
        from mynotes.agent_files import allowed
        try:
            file = allowed(path, db.get_settings().get("agent_directories", []))
            if not file.is_file():
                raise FileNotFoundError(path)
            return FileResponse(file, filename=file.name)
        except (ValueError, OSError) as error:
            raise workspace_error(error) from error

    @rtr.get("/documents/{doc_id}")
    def get_document(doc_id: str) -> dict:
        try:
            d = db.get_document(doc_id)
        except NotFound:
            raise HTTPException(404, "Document not found")
        return {**{k: d.get(k) for k in ("id", "title", "kind", "course_id", "chapter",
                 "class_date", "created_at", "current_version_id", "assets", "warnings")},
                "blocks": d.get("blocks", [])}

    @rtr.patch("/documents/{doc_id}")
    def update_document(doc_id: str, b: DocumentUpdate) -> dict:
        f = b.model_dump(exclude_unset=True)
        if not f:
            raise HTTPException(400, "No fields to update")
        try:
            return {"id": db.update_document(doc_id, f)["id"]}
        except NotFound:
            raise HTTPException(404, "Document not found")
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @rtr.delete("/documents/{doc_id}", status_code=204)
    def delete_document(doc_id: str) -> None:
        try:
            db.delete_document(doc_id)
        except NotFound:
            raise HTTPException(404, "Document not found")

    @rtr.get("/versions/{vid}")
    def get_version(vid: str) -> dict:
        """Return full version: original_path, assets, warnings, blocks, metadata."""
        try:
            v = db.get_version(vid)
        except NotFound:
            raise HTTPException(404, "Version not found")
        return {
            "id": v["id"],
            "document_id": v["document_id"],
            "original_path": v.get("original_path"),
            "created_at": v.get("created_at"),
            "assets": v.get("assets"),
            "warnings": v.get("warnings"),
            "blocks": v.get("blocks", []),
        }

    @rtr.get("/search")
    def search(q: str = Query(..., min_length=1), course_id: str | None = Query(None)) -> list[dict]:
        return db.search(q, course_id=course_id)

    @rtr.post("/material")
    def get_material(b: MaterialInput) -> dict:
        return db.material(document_ids=b.document_ids, course_id=b.course_id,
                           query=b.query, budget=b.budget)

    @rtr.get("/jobs")
    def list_jobs(kind: str | None = Query(None), status: str | None = Query(None)) -> list[dict]:
        return [{"id": j["id"], "kind": j["kind"], "status": j["status"],
                 "progress": float(j.get("progress", 0)), "attempts": int(j.get("attempts", 0)),
                 "created_at": float(j["created_at"]), "updated_at": float(j["updated_at"])}
                for j in db.list_jobs(kind=kind, status=status)]

    @rtr.get("/jobs/{jid}")
    def get_job(jid: str) -> dict:
        try:
            j = db.get_job(jid)
        except NotFound:
            raise HTTPException(404, "Job not found")
        return {"id": j["id"], "kind": j["kind"], "status": j["status"],
                "payload": json.loads(j["payload"]) if j.get("payload") else None,
                "progress": float(j.get("progress", 0)), "error": j.get("error"),
                "result": json.loads(j["result"]) if j.get("result") else None,
                "attempts": int(j.get("attempts", 0)),
                "created_at": float(j["created_at"]), "updated_at": float(j["updated_at"])}

    @rtr.post("/jobs/{jid}/retry")
    def retry_job(jid: str) -> dict:
        try:
            j = db.retry_job(jid)
        except NotFound:
            raise HTTPException(404, "Job not found")
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return {"id": j["id"], "status": j["status"]}

    @rtr.get("/decks")
    def list_decks() -> list[dict]:
        return db.list_decks()

    @rtr.post("/decks", status_code=201)
    def create_deck(b: DeckCreate) -> dict:
        try:
            return db.save_deck(title=b.title, markdown=b.markdown, source_ids=b.source_ids)
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @rtr.get("/decks/{did}")
    def get_deck(did: str) -> dict:
        try:
            return db.get_deck(did)
        except NotFound:
            raise HTTPException(404, "Deck not found")

    @rtr.put("/decks/{did}")
    def update_deck(did: str, b: DeckUpdate) -> dict:
        try:
            cur = db.get_deck(did)
        except NotFound:
            raise HTTPException(404, "Deck not found")
        try:
            return db.save_deck(
                title=b.title if b.title is not None else cur["title"],
                markdown=b.markdown if b.markdown is not None else cur["markdown"],
                source_ids=b.source_ids if b.source_ids is not None else cur.get("source_ids", []),
                deck_id=did, expected_revision=b.expected_revision)
        except Conflict:
            raise HTTPException(409, "Revision conflict")
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    # --- 202: long-running (no in-request execution) ---

    @rtr.get("/diarization")
    def diarization_status() -> dict:
        from mynotes.diarization import model_status
        return model_status(dr / "models" / "diarization")

    @rtr.post("/diarization/setup", status_code=202)
    def setup_diarization() -> JobAck:
        existing = db.list_jobs(kind="diarization_setup")
        active = next((j for j in existing if j["status"] in {"queued", "running"}), None)
        return _ack(active or db.create_job("diarization_setup", {}))

    @rtr.post("/slide-exports", status_code=202)
    def create_slide_export(b: SlideExportInput) -> JobAck:
        try:
            deck = db.get_deck(b.deck_id)
        except NotFound:
            raise HTTPException(404, "Deck not found")
        if deck["revision"] != b.revision:
            raise HTTPException(409, "Revision conflict")
        return _ack(db.create_job("slide_export", {"deck_id": b.deck_id, "revision": b.revision}))

    @rtr.post("/slide-drafts", status_code=202)
    def create_slide_draft(b: SlideDraftInput) -> JobAck:
        return _ack(db.create_job("slide_draft", b.model_dump()))

    @rtr.post("/chat", status_code=202)
    def chat(b: ChatInput) -> JobAck:
        if b.mode in {"global", "documents", "web", "files"}:
            if b.conversation_id:
                try:
                    db.get_conversation(b.conversation_id)
                except NotFound:
                    raise HTTPException(404, "Conversation not found")
            else:
                b.conversation_id = db.create_conversation(b.question[:80])["id"]
        else:
            b.conversation_id = None
        return _ack(db.create_job("chat", b.model_dump()))

    @rtr.get("/conversations")
    def list_conversations() -> list[dict]:
        return db.list_conversations()

    @rtr.post("/conversations", status_code=201)
    def create_conversation() -> dict:
        return db.create_conversation()

    @rtr.delete("/conversations/{cid}")
    def delete_conversation(cid: str) -> dict:
        try:
            db.delete_conversation(cid)
        except NotFound as exc:
            raise HTTPException(404, str(exc))
        except Conflict as exc:
            raise HTTPException(409, str(exc))
        return {"deleted": cid}

    @rtr.get("/chats")
    def list_chats(conversation_id: str | None = None) -> list[dict]:
        if conversation_id:
            try:
                db.get_conversation(conversation_id)
            except NotFound:
                raise HTTPException(404, "Conversation not found")
        return db.list_chats(conversation_id)

    @rtr.post("/suggestions", status_code=202)
    def create_suggestion(b: SuggestionInput) -> JobAck:
        return _ack(db.create_job("suggestions", b.model_dump()))

    @rtr.post("/recordings", status_code=202)
    def upload_recording(file: Annotated[UploadFile, File()]) -> JobAck:
        """Accept webm (normal Chromium output) + standard audio types."""
        if not file.content_type:
            raise HTTPException(400, "Missing Content-Type")
        if file.content_type not in ("audio/wav", "audio/mpeg", "audio/ogg",
                                      "audio/flac", "audio/mp3", "audio/webm"):
            raise HTTPException(400, f"Unsupported: {file.content_type}")
        sd = dr / "recordings"
        sd.mkdir(parents=True, exist_ok=True)
        ext = PurePath(file.filename).suffix if file.filename else ""
        dest = sd / f"{uuid.uuid4().hex}{ext}"
        total = 0
        with open(dest, "wb") as f:
            while True:
                chunk = file.file.read(8 * 1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_UPLOAD_SIZE:
                    raise HTTPException(413, "File too large")
                f.write(chunk)
        job = db.create_job("transcription", {"source_path": str(dest.relative_to(dr))})
        return _ack(job)

    @rtr.post("/visuals", status_code=202)
    def request_visual(b: VisualInput) -> JobAck:
        try:
            db.get_version(b.version_id)
        except NotFound:
            raise HTTPException(404, "Version not found")
        return _ack(db.create_job("visual", b.model_dump()))

    @rtr.get("/settings")
    def get_settings() -> dict:
        return {
            **_PROVIDER_DEFAULTS,
            **db.get_settings(),
            "has_key": bool(credentials.get()),
            "credential_storage": "encrypted" if getattr(credentials, "persistent", False) is True else "session",
            "credential_error": credentials.storage_error if isinstance(getattr(credentials, "storage_error", None), str) else None,
            "agent_available": bool(os.environ.get("MYNOTES_OPENCODE") or shutil.which("opencode")),
            "chat_harness": db.get_settings().get("chat_harness", True),
            "agent_directories": db.get_settings().get("agent_directories", []),
            "persistent_fields": ["vision_path", "transcription_model", "vision_model"],
            "defaults": {
                "vision_path": None,
                "transcription_model": None,
                "vision_model": None,
            },
        }

    @rtr.put("/settings")
    def update_settings(b: SettingsUpdate) -> dict:
        cur = {**_PROVIDER_DEFAULTS, **db.get_settings()}
        if b.agent_directories is not None:
            directories = [str(pathlib.Path(p).expanduser().resolve()) for p in b.agent_directories]
            if any(not pathlib.Path(p).is_dir() for p in directories):
                raise HTTPException(400, "Allowed agent directories must exist")
            b.agent_directories = list(dict.fromkeys(directories))
        for k, v in b.model_dump(exclude_none=True, exclude={"api_key"}).items():
            cur[k] = v
        if b.api_key:
            try:
                credentials.set(b.api_key)
            except OSError as error:
                raise HTTPException(500, str(error)) from error
        db.set_settings(cur)
        return get_settings()

    @rtr.get("/provider/models")
    def provider_models() -> list[str]:
        key = credentials.get()
        if not key:
            raise HTTPException(400, "Save your Cloud Temple API key in Settings first")
        settings = get_settings()
        base = settings["base_url"].rstrip("/")
        parsed = urlparse(base)
        local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        secure = parsed.scheme == "https" or (local and parsed.scheme == "http")
        if not secure or not parsed.hostname or parsed.username or parsed.password:
            raise HTTPException(400, "Provider base URL must use HTTPS (except localhost), without embedded credentials")
        try:
            response = httpx.get(base + "/models", headers={"Authorization": f"Bearer {key}"},
                                 timeout=settings.get("timeout", 20.0))
            response.raise_for_status()
            return sorted({item["id"] for item in response.json()["data"]
                           if isinstance(item.get("id"), str)})
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            raise HTTPException(502, "Could not load provider models; check your key and base URL")

    @rtr.post("/imports", status_code=202)
    def import_file(file: Annotated[UploadFile, File()],
                          kind: str | None = Form(None),
                          entrypoint: str | None = Form(None),
                          document_id: str | None = Form(None),
                          course_id: str | None = Form(None)) -> JobAck:
        inferred = _infer_kind(file.filename or "")
        kind = kind or inferred
        if kind not in ("pdf", "markdown", "html", "typst", "folder", "pptx"):
            raise HTTPException(400, f"Unsupported kind: {kind}")
        safe_name = _sanitize_filename(file.filename or "upload")
        sd = dr / "uploads"
        sd.mkdir(parents=True, exist_ok=True)
        dest = sd / f"{uuid.uuid4().hex}_{safe_name}"
        total = 0
        with open(dest, "wb") as f:
            while True:
                chunk = file.file.read(8 * 1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_UPLOAD_SIZE:
                    raise HTTPException(413, "File too large")
                f.write(chunk)
        job = db.create_job("import", {"source_path": str(dest.relative_to(dr)), "kind": kind,
                                        "course_id": course_id, "entrypoint": entrypoint,
                                        "document_id": document_id})
        return _ack(job)

    @rtr.post("/backups", status_code=202)
    def create_backup() -> JobAck:
        job = db.create_job("backup", {"data_root": str(dr)})
        return _ack(job)

    @rtr.post("/restore", status_code=202)
    def restore_backup(file: Annotated[UploadFile, File()]) -> JobAck:
        safe_name = _sanitize_filename(file.filename or "backup")
        sd = dr / "uploads"
        sd.mkdir(parents=True, exist_ok=True)
        dest = sd / f"{uuid.uuid4().hex}_{safe_name}"
        total = 0
        with open(dest, "wb") as f:
            while True:
                chunk = file.file.read(8 * 1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_UPLOAD_SIZE:
                    raise HTTPException(413, "File too large")
                f.write(chunk)
        job = db.create_job("restore", {"source_path": str(dest.relative_to(dr))})
        return _ack(job)

    @rtr.get("/files/{path:path}")
    def serve_file(path: str) -> FileResponse:
        r = _safe_file(dr, path)
        if r is None:
            raise HTTPException(403, "Access denied")
        return FileResponse(r)

    @rtr.post("/notes", status_code=202)
    def create_note(b: NoteCreate) -> JobAck:
        if b.format not in ("markdown", "typst"):
            raise HTTPException(400, f"Unsupported format: {b.format}")
        sd = dr / "notes"
        sd.mkdir(parents=True, exist_ok=True)
        nid = uuid.uuid4().hex
        ext = "typ" if b.format == "typst" else "md"
        dest = sd / f"{nid}.{ext}"
        dest.write_text(b.content, encoding="utf-8")
        return _ack(db.create_job("note", {"title": b.title, "content": b.content,
                                             "format": b.format, "course_id": b.course_id,
                                             "source_path": str(dest.relative_to(dr))}))

    @rtr.put("/notes/{nid}", status_code=202)
    def update_note(nid: str, b: NoteUpdate) -> JobAck:
        """nid is immutable (from URL); file path {nid}.{ext} is set at creation."""
        if not nid.isalnum() or len(nid) != 32:
            raise HTTPException(400, "Invalid note ID")
        try:
            doc = db.get_document(nid)
        except NotFound:
            doc = None
        fmt = b.format if b.format is not None else "markdown"
        if fmt not in ("markdown", "typst"):
            raise HTTPException(400, f"Unsupported format: {fmt}")
        sd = dr / "notes"
        sd.mkdir(parents=True, exist_ok=True)
        ext = "typ" if fmt == "typst" else "md"
        dest = sd / f"{uuid.uuid4().hex}.{ext}"
        dest.write_text(b.content if b.content is not None else "", encoding="utf-8")
        return _ack(db.create_job("note", {"note_id": nid, "title": b.title,
                                              "content": b.content, "format": fmt,
                                              "document_id": nid if doc else None,
                                              "course_id": doc.get("course_id") if doc else None,
                                              "source_path": str(dest.relative_to(dr))}))

    app.include_router(rtr)

    # === Tool router (separate auth) ===
    tools = APIRouter(prefix="/tools", dependencies=[Depends(_auth_tool)])

    @tools.post("/material")
    def tools_material(b: MaterialInput) -> dict:
        return db.material(document_ids=b.document_ids, course_id=b.course_id,
                           query=b.query, budget=b.budget)

    @tools.get("/decks")
    def tools_list_decks() -> list[dict]:
        return db.list_decks()

    @tools.post("/decks", status_code=201)
    def tools_create_deck(b: DeckCreate) -> dict:
        try:
            return db.save_deck(title=b.title, markdown=b.markdown, source_ids=b.source_ids)
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @tools.get("/decks/{did}")
    def tools_get_deck(did: str) -> dict:
        try:
            return db.get_deck(did)
        except NotFound:
            raise HTTPException(404, "Deck not found")

    @tools.put("/decks/{did}")
    def tools_update_deck(did: str, b: DeckUpdate) -> dict:
        try:
            cur = db.get_deck(did)
        except NotFound:
            raise HTTPException(404, "Deck not found")
        try:
            return db.save_deck(
                title=b.title if b.title is not None else cur["title"],
                markdown=b.markdown if b.markdown is not None else cur["markdown"],
                source_ids=b.source_ids if b.source_ids is not None else cur.get("source_ids", []),
                deck_id=did, expected_revision=b.expected_revision)
        except Conflict:
            raise HTTPException(409, "Revision conflict")
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @tools.post("/decks/{did}/exports", status_code=202)
    def tools_export_deck(did: str, b: ToolExportInput) -> JobAck:
        try:
            deck = db.get_deck(did)
        except NotFound:
            raise HTTPException(404, "Deck not found")
        if deck["revision"] != b.revision:
            raise HTTPException(409, "Revision conflict")
        return _ack(db.create_job("slide_export", {"deck_id": did, "revision": b.revision}))

    @tools.get("/artifacts/{path:path}")
    def tools_artifact(path: str) -> FileResponse:
        """Tool artifact download: exports only (backups denied from tool)."""
        parts = PurePath(path).parts
        if not parts:
            raise HTTPException(403, "Access denied")
        if parts[0] != "exports":
            raise HTTPException(403, "Only exports are accessible from tools")
        resolved = (dr / path).resolve()
        if not resolved.is_relative_to(dr.resolve()):
            raise HTTPException(403, "Access denied")
        if not resolved.is_file():
            raise HTTPException(404, "File not found")
        return FileResponse(resolved)

    # No /tools/jobs list — denied
    @tools.get("/jobs/{jid}")
    def tools_get_job(jid: str) -> dict:
        """Status/result/errors for slide_export / slide_draft only."""
        try:
            j = db.get_job(jid)
        except NotFound:
            raise HTTPException(404, "Job not found")
        if j["kind"] not in ("slide_export", "slide_draft"):
            raise HTTPException(403, "Only slide_export and slide_draft jobs are accessible")
        return {"id": j["id"], "kind": j["kind"], "status": j["status"],
                "result": json.loads(j["result"]) if j.get("result") else None,
                "error": j.get("error"),
                "progress": float(j.get("progress", 0)),
                "created_at": float(j["created_at"])}

    app.include_router(tools)
    return app
