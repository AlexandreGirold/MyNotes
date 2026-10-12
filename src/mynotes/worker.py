"""Background worker — persistent, single-thread job processor.

Public API
==========
Worker(db, data_dir, credentials)
    Initialise the worker. No threads are started yet.

Worker.start() -> None
    Begin the main loop in a background thread. Recovers stale jobs
    before entering the loop.

Worker.stop() -> None
    Signal the worker to finish the current job (if any) and exit the
    main loop. Blocks up to 30 seconds for thread join.

Worker.run_once() -> dict | None
    Claim and process exactly one queued job (blocking). Returns the
    finished job record or None if no jobs are available.

Worker.schedule(kind, payload) -> dict
    Enqueue a new job. Writes to DB, returns the created job record.

Job kinds and payloads
----------------------
import   {source_path, kind, course_id?, document_id?, entrypoint?, title?}
note     {content, format, title, course_id, document_id?}
chat     {document_ids?, course_id?, question}
suggestions  {document_ids}
slide_draft  {document_ids?, topic, count, language}
slide_export {deck_id, revision?}
transcription {source_path, course_id?}
visual   {version_id?, page?, question?}
backup   {destination?}
restore  {source_path}

Directory strategy
------------------
Each job gets a unique work directory under ``data_dir / jobs / job-{job_id}``
as temporary scratch space. All originals and derived assets are persisted
outside this directory under ``data_dir/originals/``, ``data_dir/notes/``,
``data_dir/transcriptions/``, and ``data_dir/assets/<job-id>/`` so they
survive cleanup.

Source preservation
-------------------
Original files are never moved or deleted. Paths are resolved relative to
*data_dir* when not already absolute.

Idempotency
-----------
save_document checks for existing versions with the same original_path
before creating new ones, so restarts after partial completion don't
produce duplicate versions.
"""

from __future__ import annotations

import json
import logging
import pathlib
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import Any

from mynotes.db import Database, NotFound

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_RETRIES = 3
RECOVER_MAX_AGE = 3600.0  # 1 hour
KNOWN_KINDS = (
    "import",
    "note",
    "chat",
    "suggestions",
    "slide_draft",
    "slide_export",
    "transcription",
    "visual",
    "backup",
    "restore",
)

# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


class Worker:
    """Single-threaded background job processor.

    Parameters
    ----------
    db:
        ``Database`` instance (SQLite + FTS5).
    data_dir:
        Directory where processed artifacts and originals are stored.
    credentials:
        Credential store or ``None``. If present, ``credentials.get()``
        must return an API key string or ``None``.
    """

    def __init__(
        self,
        db: Database,
        data_dir: pathlib.Path | str,
        credentials: Any = None,
    ) -> None:
        self.db = db
        self.data_dir = pathlib.Path(data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.credentials = credentials
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._analysis_process: subprocess.Popen | None = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Start the worker loop in a background thread.

        Recover interrupted jobs before starting.
        """
        if self._thread and self._thread.is_alive():
            return  # already running
        self._stop_event.clear()
        self._recover()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="mynotes-worker",
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal the worker to finish and exit.

        Blocks up to 30 seconds for the thread to join.
        """
        self._stop_event.set()
        child = self._analysis_process
        if child is not None and child.poll() is None:
            child.terminate()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=30)

    def run_once(self) -> dict[str, Any] | None:
        """Process exactly one queued job (blocking).

        Returns the finished job record or ``None`` if no jobs are
        available.
        """
        return self._process_one()

    # -- scheduling --------------------------------------------------------

    def schedule(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Enqueue a job. Returns the created job record."""
        job = self.db.create_job(kind, payload)
        return job

    # -- internal: recovery ------------------------------------------------

    def _recover(self) -> None:
        """Recover interrupted / stale jobs from the database."""
        recovered = self.db.recover_jobs(
            max_age=RECOVER_MAX_AGE,
            max_retries=MAX_RETRIES,
        )
        if recovered:
            logger.info("Recovered %d interrupted jobs", recovered)

    # -- internal: main loop -----------------------------------------------

    def _loop(self) -> None:
        """Main worker loop -- claims and processes jobs until stopped."""
        while not self._stop_event.is_set():
            processed = self._process_one()
            if processed is None:
                self._stop_event.wait(timeout=2.0)

    # -- internal: single job processing -----------------------------------

    def _process_one(self) -> dict[str, Any] | None:
        """Claim and process one job. Returns the finished job or None."""
        job = self._claim_any()
        if job is None:
            return None

        job_id = job["id"]
        kind = job["kind"]
        payload = job.get("payload") or {}
        if isinstance(payload, str):
            payload = json.loads(payload)

        work_dir = self._make_work_dir(job_id)

        logger.info("Processing job %s (kind=%s, work_dir=%s)", job_id, kind, work_dir)

        try:
            handler = self._handlers.get(kind)
            if handler is None:
                raise ValueError(f"Unknown job kind: {kind}")

            result = handler(payload, work_dir, job_id)
            self.db.finish_job(job_id, result=result)
            logger.info("Job %s completed successfully", job_id)
        except Exception as exc:
            logger.exception("Job %s failed", job_id)
            self.db.fail_job(job_id, error=str(exc))
        finally:
            self._cleanup_work_dir(work_dir)

        try:
            return self.db.get_job(job_id)
        except NotFound:
            return None

    # -- internal: claim (try all kinds) -----------------------------------

    def _claim_any(self) -> dict[str, Any] | None:
        """Atomically claim the next queued job across all kinds."""
        for kind in KNOWN_KINDS:
            job = self.db.claim_job(kind=kind, worker_id="default")
            if job is not None:
                return job

        with self.db._conn() as conn:
            now = time.time()
            row = conn.execute(
                "SELECT * FROM jobs WHERE status = 'queued' "
                "ORDER BY created_at ASC LIMIT 1",
            ).fetchone()
            if row is None:
                return None

            job_id = row["id"]
            conn.execute(
                "UPDATE jobs SET status = 'running', "
                "worker_id = ?, updated_at = ? WHERE id = ?",
                ("default", now, job_id),
            )
            conn.commit()
            return dict(self.db._one("SELECT * FROM jobs WHERE id = ?", (job_id,)))

    # -- internal: directory management ------------------------------------

    def _make_work_dir(self, job_id: str) -> pathlib.Path:
        """Create a unique work directory for the given job ID."""
        work_dir = self.data_dir / "jobs" / f"job-{job_id}"
        work_dir.mkdir(parents=True, exist_ok=True)
        return work_dir

    def _cleanup_work_dir(self, work_dir: pathlib.Path) -> None:
        """Remove the job's temporary work directory if it exists."""
        if work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)

    # -- internal: credential / settings -----------------------------------

    def _get_api_key(self) -> str | None:
        if self.credentials:
            return self.credentials.get()
        return None

    def _get_settings(self) -> dict[str, Any]:
        from mynotes.provider import DEFAULT_SETTINGS
        return {**DEFAULT_SETTINGS, **self.db.get_settings()}

    def _provider(self, feature: str, model_field: str | None = None):
        from mynotes.provider import Provider
        settings = self._get_settings()
        if not settings.get(f"verified_{feature}"):
            raise ValueError(f"Confirm the {feature} endpoint in Settings before using it")
        model_field = model_field or {"chat": "chat_model", "vision": "vision_model", "transcription": "transcription_model"}[feature]
        if not settings.get(model_field):
            raise ValueError(f"Choose {model_field} in Settings")
        if feature == "chat":
            settings = {**settings, "chat_model": settings[model_field]}
        return Provider(settings=settings, api_key=self._get_api_key())

    # -- helpers -----------------------------------------------------------

    def _persist_original(self, source: pathlib.Path, job_id: str) -> pathlib.Path:
        """Copy *source* to ``data_dir/originals/<name>`` permanently."""
        dest = self.data_dir / "originals" / job_id / source.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, dest, dirs_exist_ok=True)
        else:
            dest.write_bytes(source.read_bytes())
        return dest

    def _persist_asset_dir(self, src_dir: pathlib.Path, job_id: str) -> None:
        """Copy everything under *src_dir* to ``data_dir/assets/<job_id>``."""
        dest = self.data_dir / "assets" / job_id
        if src_dir.exists() and list(src_dir.iterdir()):
            dest.mkdir(parents=True, exist_ok=True)
            for item in src_dir.iterdir():
                target = dest / item.name
                if item.is_dir():
                    shutil.copytree(item, target)
                else:
                    shutil.copy2(item, target)

    # -- handlers ----------------------------------------------------------

    def _process_import(
        self, payload: dict, work_dir: pathlib.Path, job_id: str
    ) -> dict:
        """Handle an import job -- persist originals outside work_dir."""
        from mynotes.importers import process_import

        source_path = payload.get("source_path")
        if not source_path:
            raise ValueError("import job missing 'source_path'")

        # Resolve source path: relative paths resolve against data_dir root
        raw_source = pathlib.Path(source_path)
        if not raw_source.is_absolute():
            source = (self.data_dir / raw_source).resolve()
        else:
            source = raw_source.resolve()

        if not source.exists():
            raise FileNotFoundError(f"Source not found: {source_path}")

        kind = payload.get("kind", "pdf")
        entrypoint = payload.get("entrypoint")
        if kind == "typst" and source.suffix.lower() == ".zip":
            from mynotes.backup import safe_extract
            project = work_dir / "project"
            safe_extract(source, project)
            source = project

        # Import -- work_dir is the temporary processing destination
        result = process_import(source, work_dir, kind, entrypoint)

        # Persist the copied original to a permanent location
        orig_file = pathlib.Path(result.get("original_path", ""))
        if orig_file.exists():
            permanent_orig = self._persist_original(orig_file, job_id)
            result["original_path"] = str(
                permanent_orig.relative_to(self.data_dir)
            )

        # Persist any extracted assets permanently
        self._persist_asset_dir(work_dir, job_id)
        for asset in result.get("assets", []):
            asset_path = pathlib.Path(asset["path"])
            if asset_path.is_relative_to(work_dir):
                asset["path"] = str(pathlib.Path("assets") / job_id / asset_path.relative_to(work_dir))

        # Persist document in DB with correct permanent paths
        if result.get("blocks") or result.get("original_path"):
            version = self.db.save_document(
                title=result["title"],
                kind=kind,
                original_path=result.get("original_path", ""),
                blocks=result["blocks"],
                assets=result.get("assets"),
                warnings=result.get("warnings"),
                metadata={},
                course_id=payload.get("course_id"),
                document_id=payload.get("document_id"),
            )
            result["document_id"] = version["document_id"]
        if kind == "typst" and not result.get("blocks") and result.get("warnings"):
            raise ValueError("; ".join(result["warnings"]))

        return result

    def _process_note(
        self, payload: dict, work_dir: pathlib.Path, job_id: str
    ) -> dict:
        """Handle a note creation job -- persist raw source permanently."""
        content = payload.get("content", "")
        title = payload.get("title") or "Untitled"
        fmt = payload.get("format", "markdown")

        # Normalise format to a file extension
        ext_map = {"markdown": "md", "md": "md", "typst": "typ", "typ": "typ"}
        ext = ext_map.get(fmt, "txt" if fmt not in ("md", "typ") else fmt)
        doc_kind = ext if ext in ("md", "typ") else "text"

        # Save the raw immutable source permanently BEFORE any compilation
        notes_dir = self.data_dir / "notes"
        notes_dir.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r"[^\w-]", "_", title or "Untitled")[:80]
        notes_dir = notes_dir / job_id
        notes_dir.mkdir(parents=True, exist_ok=True)
        note_path = notes_dir / f"{safe_name}.{ext}"
        note_path.write_text(content, encoding="utf-8")
        if fmt == "typst":
            return self._process_import({**payload, "source_path": str(note_path), "kind": "typst"}, work_dir, job_id)

        rel_path = str(note_path.relative_to(self.data_dir))

        # Persist document with correct permanent path
        doc = self.db.save_document(
            title=title,
            kind=doc_kind,
            original_path=rel_path,
            blocks=[
                {
                    "text": content,
                    "locator": {"file": note_path.name},
                    "origin": "created",
                }
            ],
            course_id=payload.get("course_id"),
            document_id=payload.get("document_id"),
        )

        return {"document_id": doc["document_id"], "path": str(note_path)}

    def _process_chat(
        self, payload: dict, work_dir: pathlib.Path, job_id: str
    ) -> dict:
        """Handle a chat job -- material retrieval + provider call."""
        document_ids = payload.get("document_ids", [])
        course_id = payload.get("course_id")
        question = payload.get("question", "")
        if not question:
            raise ValueError("chat job missing 'question'")

        mode = payload.get("mode", "documents")
        conversation_id = payload.get("conversation_id") if mode in {"global", "documents", "web", "files"} else None
        history = []
        if conversation_id:
            conversation = self.db.get_conversation(conversation_id)
            payload = {**payload, "session_id": conversation["session_id"]}
            # ponytail: direct-chat context retains the newest 20 turns / 24000 characters; older turns stay on disk.
            size = 0
            for turn in reversed(self.db.list_chats(conversation_id)[-20:]):
                size += len(turn["question"]) + len(turn["answer"])
                if size > 24000:
                    break
                history[0:0] = [{"role": "user", "content": turn["question"]}, {"role": "assistant", "content": turn["answer"]}]
            payload = {**payload, "history": history}
        material = {} if mode in {"web", "files"} else self.db.material(
            document_ids=document_ids, course_id=course_id,
            query=question, budget=60000 if mode.endswith("summary") or mode == "transcript_cleanup" else int(self._get_settings().get("context_budget", 12000)),
        )

        api_key = self._get_api_key()
        if not api_key:
            raise ValueError("Connect Cloud Temple in Settings: add your API key, then click Save & connect.")

        if mode == "web":
            from mynotes.web_tools import research_web
            self.db._execute("UPDATE jobs SET result=? WHERE id=?", (json.dumps({"stage": "researching", "steps": [{"tool": "search_web", "status": "running", "title": "Searching public Internet sources"}]}), job_id))
            material = research_web(question)
        elif mode == "files":
            from mynotes.agent_files import research_files
            self.db._execute("UPDATE jobs SET result=? WHERE id=?", (json.dumps({"stage": "researching", "steps": [{"tool": "search_allowed_files", "status": "running", "title": "Searching Settings' allowed laptop folders"}]}), job_id))
            material = research_files(question, self._get_settings().get("agent_directories", []))

        model_field = {"documents": "chat_model", "global": "global_chat_model", "web": "global_chat_model", "files": "chat_model",
                       "audio_summary": "audio_summary_model", "document_summary": "document_summary_model",
                       "transcript_cleanup": "audio_summary_model"}[mode]
        provider = self._provider("chat", model_field)
        try:
            if payload.get("harness") and mode not in {"web", "files"}:
                from mynotes.agent_harness import run_agent
                result = run_agent(self.db, self.data_dir, payload, self._get_settings(), api_key, job_id)
            elif mode == "global":
                result = provider.general_chat(question, history=history)
            elif mode.endswith("summary") or mode == "transcript_cleanup":
                last_update = 0.0
                def partial(answer):
                    nonlocal last_update
                    if time.monotonic() - last_update > 0.2:
                        self.db._execute("UPDATE jobs SET result=? WHERE id=?", (json.dumps({"answer": answer, "stage": "reviewing" if mode == "transcript_cleanup" else "summarizing", "streaming": True}), job_id))
                        last_update = time.monotonic()
                result = provider.summarize(material, language=payload.get("language", "auto"),
                    note_kind=payload.get("note_kind", "lecture"), glossary=payload.get("glossary", ""),
                    cleanup=mode == "transcript_cleanup", on_delta=partial)
            else:
                result = provider.chat(material=material, question=question, history=history)
            # Grounded citations: only valid block IDs from material
            grounded = []
            allowed_ids = {b["source_id"] for b in material.get("blocks", [])}
            allowed_ids.add(material.get("metadata", {}).get("source_id", ""))
            for c in result.get("citations", []):
                sid = c.get("source_id", "")
                if sid in allowed_ids and sid:
                    # Enrich from blocks only -- find the matching block excerpt
                    for b in material.get("blocks", []):
                        if b.get("source_id") == sid:
                            c["excerpt"] = b.get("text", "")
                            c.update({k: b.get(k) for k in ("version_id", "document_id", "title", "locator", "origin")})
                            grounded.append(c)
                            break
                # Invalid / ungrounded citations are rejected silently
            result["citations"] = grounded
            result["mode"] = "global" if mode == "global" else material["mode"]
            result["warnings"] = list(dict.fromkeys(result.get("warnings", []) + ([] if mode == "global" else material["warnings"])))

            self.db.save_chat(
                question=question,
                answer=result["answer"],
                citations=result["citations"],
                mode=mode,
                conversation_id=conversation_id,
                session_id=result.get("session_id"),
            )
            result["conversation_id"] = conversation_id
            if mode in {"web", "files"}:
                result["steps"] = [{"tool": "read_web" if mode == "web" else "read_allowed_file", "status": "completed", "title": b["title"]} for b in material["blocks"]]
            return result
        finally:
            provider.close()

    def _process_suggestions(
        self, payload: dict, work_dir: pathlib.Path, job_id: str
    ) -> dict:
        """Handle a metadata suggestions job."""
        document_ids = payload.get("document_ids", [])
        if not document_ids:
            raise ValueError("suggestions job missing 'document_ids'")

        material = self.db.material(document_ids=document_ids)
        if not material.get("blocks"):
            material["metadata"] = {
                "source_id": document_ids[0] if document_ids else "?"
            }

        api_key = self._get_api_key()
        if not api_key:
            return {"suggestions": {}, "mode": "fallback"}

        provider = self._provider("chat")
        try:
            return provider.suggest(material=material)
        finally:
            provider.close()

    def _process_slide_draft(
        self, payload: dict, work_dir: pathlib.Path, job_id: str
    ) -> dict:
        """Handle a slide draft generation job."""
        document_ids = payload.get("document_ids", [])
        topic = payload.get("topic", "General")
        count = payload.get("count", 5)
        language = payload.get("language", "English")

        material = self.db.material(document_ids=document_ids)

        api_key = self._get_api_key()
        if not api_key:
            return {
                "title": topic,
                "markdown": f"# {topic}\n\nNo provider configured.",
                "source_ids": [],
                "mode": "fallback",
            }

        provider = self._provider("chat")
        try:
            result = provider.draft_slides(
                material=material,
                topic=topic,
                count=count,
                language=language,
            )
            if "markdown" not in result:
                slides = result.get("slides", [])
                result["markdown"] = "\n\n---\n\n".join("# " + slide["heading"] + "\n\n" + slide["body"] for slide in slides)
                result["source_ids"] = list(dict.fromkeys(sid for slide in slides for sid in slide.get("source_ids", [])))
            deck = self.db.save_deck(
                title=result["title"],
                markdown=result["markdown"],
                source_ids=result.get("source_ids", []),
            )
            return {**result, "deck": deck}
        finally:
            provider.close()

    def _process_slide_export(
        self, payload: dict, work_dir: pathlib.Path, job_id: str
    ) -> dict:
        """Handle a slide export job -- append human citations to manifest."""
        from mynotes.exports import export_deck

        deck_id = payload.get("deck_id")
        if not deck_id:
            raise ValueError("slide_export job missing 'deck_id'")

        deck = self.db.get_deck(deck_id)
        if payload.get("revision") is not None and payload["revision"] != deck["revision"]:
            raise ValueError("Deck changed after export was queued; export the current revision")
        markdown = deck.get("markdown", "")
        source_ids = deck.get("source_ids", [])

        # Gather citations from blocks only
        citations: list[dict[str, Any]] = []
        assets: list[dict[str, Any]] = []
        asset_paths = set()
        for block_id in source_ids:
            try:
                block = self.db._one("SELECT b.text, b.locator, b.version_id, d.title FROM blocks b "
                    "JOIN versions v ON v.id=b.version_id JOIN documents d ON d.id=v.document_id WHERE b.id=?", (block_id,))
                if block:
                    citations.append({"source_id": block_id, "excerpt": block["text"],
                        "title": block["title"], "version_id": block["version_id"],
                        "locator": json.loads(block["locator"]) if block["locator"] else {},
                        "source": block["locator"]})
                    version = self.db.get_version(block["version_id"])
                    for asset in version["assets"]:
                        if asset["path"] in asset_paths or not re.search(r"\.(png|jpe?g|svg|webp)$", asset["path"], re.IGNORECASE):
                            continue
                        path = (self.data_dir / asset["path"]).resolve()
                        if path.is_relative_to(self.data_dir) and path.is_file():
                            assets.append({**asset, "path": str(path), "version_id": version["id"]})
                            asset_paths.add(asset["path"])
            except (NotFound, KeyError):
                pass

        destination = self.data_dir / "exports" / job_id
        result = export_deck(
            markdown=markdown,
            destination=destination,
            assets=assets,
            citations=citations,
        )
        return {k: str(pathlib.Path(v).relative_to(self.data_dir)) for k, v in result.items()}

    def _process_transcription(
        self, payload: dict, work_dir: pathlib.Path, job_id: str
    ) -> dict:
        """Handle a transcription job -- save original BEFORE network calls."""
        source_path = payload.get("source_path")
        if not source_path:
            raise ValueError("transcription job missing 'source_path'")

        raw_source = pathlib.Path(source_path)
        if not raw_source.is_absolute():
            source = (self.data_dir / raw_source).resolve()
        else:
            source = raw_source.resolve()

        if not source.exists():
            raise FileNotFoundError(f"Audio not found: {source_path}")

        # Save original audio to permanent location BEFORE any network call
        # so retry never loses data
        audio_name = source.name
        orig_dir = self.data_dir / "originals" / "audio"
        orig_dir.mkdir(parents=True, exist_ok=True)
        permanent_audio = orig_dir / audio_name
        permanent_audio.write_bytes(source.read_bytes())
        audio_document = self.db.save_document(title=f"Recording: {source.stem}", kind="audio",
            original_path=str(permanent_audio.relative_to(self.data_dir)), blocks=[],
            assets=[{"path": str(permanent_audio.relative_to(self.data_dir))}])
        if not self._get_api_key():
            return {"text": "", "segments": [], "mode": "local-only", "document_id": audio_document["document_id"],
                    "warnings": ["Recording saved locally. Configure a transcription provider to transcribe it."]}

        # Cloud Temple's official upload example supports WebM directly.
        audio_to_use = permanent_audio

        api_key = self._get_api_key()
        if not api_key:
            return {
                "text": "",
                "segments": [],
                "mode": "fallback",
                "warnings": ["Provider not configured"],
            }

        provider = self._provider("transcription")
        try:
            result = provider.transcribe(audio_to_use)


            # Save transcription result permanently
            tx_dir = self.data_dir / "transcriptions" / job_id
            tx_dir.mkdir(parents=True, exist_ok=True)
            tx_path = tx_dir / "result.json"
            tx_path.write_text(
                json.dumps(result, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            # Save the original audio document in DB
            transcript = self.db.save_document(
                title=f"Transcription: {source.stem}",
                kind="transcription",
                original_path=str(permanent_audio.relative_to(self.data_dir)),
                document_id=audio_document["document_id"],
                blocks=[{"text": seg["text"], "locator": {"file": audio_name, "start": seg["start"], "end": seg["end"]}, "origin": "transcribed"}
                    for seg in result.get("segments", [])] or [{"text": result.get("text", ""), "locator": {"file": audio_name}, "origin": "transcribed"}],
                assets=[{"path": str(permanent_audio.relative_to(self.data_dir))}],
                warnings=result.get("warnings"),
            )
            result["document_id"] = transcript["document_id"]

            return result
        finally:
            provider.close()

    def _process_visual(
        self, payload: dict, work_dir: pathlib.Path, job_id: str
    ) -> dict:
        """Handle a visual (image description) job."""
        version_id = payload.get("version_id")
        page = payload.get("page")
        question = payload.get("question", "Describe this image.")

        asset_path: pathlib.Path | None = None
        context = ""

        if not version_id:
            raise ValueError("A source version is required")
        ver = self.db.get_version(version_id)
        context = "\n".join(b["text"] for b in ver["blocks"] if not page or (b.get("locator") or {}).get("page") == page)
        if page is not None:
            from mynotes.importers import render_page
            pdf = ver["original_path"] if ver["original_path"].endswith(".pdf") else next((a["path"] for a in ver["assets"] if a["path"].endswith(".pdf")), None)
            if pdf:
                asset_path = render_page(self.data_dir / pdf, page, work_dir)
        elif payload.get("asset") in {a["path"] for a in ver["assets"]}:
            candidate = (self.data_dir / payload["asset"]).resolve()
            if candidate.is_relative_to(self.data_dir) and candidate.is_file():
                asset_path = candidate

        api_key = self._get_api_key()

        if not asset_path:
            return {
                "description": "",
                "warnings": ["No visual asset found"],
                "mode": "fallback",
            }

        if not api_key:
            return {
                "description": "",
                "warnings": ["Provider not configured"],
                "mode": "fallback",
            }

        provider = self._provider("vision")
        try:
            result = provider.describe(
                image=asset_path, context=context, question=question,
            )
            derived = self.data_dir / "derived" / job_id
            derived.mkdir(parents=True, exist_ok=True)
            image = derived / asset_path.name
            shutil.copy2(asset_path, image)
            description = derived / "description.json"
            description.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            generated = self.db.save_document(title=f"Visual interpretation: {version_id[:8]} page {page or ''}",
                kind="visual_description", original_path=str(description.relative_to(self.data_dir)),
                blocks=[{"text": result["description"], "origin": "generated",
                    "locator": {"source_version_id": version_id, "page": page, "asset": str(image.relative_to(self.data_dir))}}],
                assets=[{"path": str(image.relative_to(self.data_dir))}], warnings=["AI-generated interpretation; consult the original visual."])
            result.update({"document_id": generated["document_id"], "origin": "generated", "source_version_id": version_id})
            return result
        finally:
            provider.close()

    def _process_diarization_setup(self, payload, work_dir, job_id):
        from mynotes.diarization import setup_models
        def progress(stage, fraction):
            self.db._execute("UPDATE jobs SET progress=?, result=? WHERE id=?",
                             (fraction, json.dumps({"stage": stage}), job_id))
        return setup_models(self.data_dir / "models" / "diarization", on_progress=progress)

    def _process_diarization(self, payload, work_dir, job_id):
        session = payload.get("session_id", "")
        if not re.fullmatch(r"[0-9a-f]{32}", session):
            raise ValueError("Invalid audio session")
        audio = (self.data_dir / "temporary-audio" / (session + ".wav")).resolve()
        if not audio.is_relative_to(self.data_dir / "temporary-audio") or not audio.is_file():
            raise ValueError("Temporary session audio is unavailable")
        version = self.db.get_version(payload["transcript_version_id"])
        if version["document_id"] != payload["transcript_document_id"]:
            raise ValueError("Transcript version does not belong to this session")
        original = "\n".join(b["text"] for b in version["blocks"])
        output = work_dir / "speakers.json"
        command = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, "-m", "mynotes"]
        options = {"audio_path": str(audio), "transcript": original,
                   "models_dir": str(self.data_dir / "models" / "diarization"),
                   "output_path": str(output), "language": payload.get("language", "auto"),
                   "num_speakers": payload.get("num_speakers", 0)}
        self.db._execute("UPDATE jobs SET result=? WHERE id=?",
                         (json.dumps({"stage": "Detecting speakers and matching words locally"}), job_id))
        with subprocess.Popen(command + ["diarize"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True) as child:
            self._analysis_process = child
            try:
                if self._stop_event.is_set():
                    child.terminate()
                _, error = child.communicate(json.dumps(options), timeout=4 * 3600)
            except subprocess.TimeoutExpired:
                child.kill()
                child.communicate()
                raise ValueError("Local speaker processing timed out; audio retained for Retry") from None
            finally:
                self._analysis_process = None
        if child.returncode:
            raise ValueError("Local speaker detection failed; raw transcript and audio retained for Retry. "
                             + error[-1000:])
        result = json.loads(output.read_text(encoding="utf-8"))
        folder = self.data_dir / "notes" / "speakers" / job_id
        folder.mkdir(parents=True, exist_ok=True)
        note = folder / "transcript.md"
        note.write_text(result["text"], encoding="utf-8")
        doc = self.db.save_document(title=payload.get("title", "Live session") + " — Speakers",
            kind="markdown", original_path=str(note.relative_to(self.data_dir)),
            blocks=[{"text": result["text"], "origin": "transcribed",
                     "locator": {"session_id": session, "raw_version_id": version["id"]}}],
            warnings=result.get("warnings", []), metadata={"segments": result["segments"]})
        audio.unlink()  # Only after the derived transcript is durably saved.
        return {**result, "document_id": doc["document_id"], "raw_document_id": version["document_id"]}

    def _process_backup(
        self, payload: dict, work_dir: pathlib.Path, job_id: str
    ) -> dict:
        """Handle a backup job."""
        from mynotes.backup import backup

        destination = payload.get("destination")
        if not destination:
            destination = str(self.data_dir / "backups")

        result = backup(str(self.data_dir), destination)
        return {"backup_path": str(pathlib.Path(result).relative_to(self.data_dir)), "status": "completed"}

    def _process_restore(
        self, payload: dict, work_dir: pathlib.Path, job_id: str
    ) -> dict:
        """Handle a restore job -- cannot overwrite a running job."""
        from mynotes.backup import restore

        source_path = payload.get("source_path")
        if not source_path:
            raise ValueError("restore job missing 'source_path'")

        archive = pathlib.Path(source_path)
        if not archive.is_absolute():
            archive = self.data_dir / archive
        jobs = self.db._query("SELECT * FROM jobs")
        result = restore(self.data_dir, archive)
        self.db._init_schema()  # Migrate older backups before library/search queries resume.
        for job in jobs:
            columns = list(job)
            self.db._execute("INSERT OR REPLACE INTO jobs (" + ",".join(columns) + ") VALUES (" + ",".join("?" for _ in columns) + ")", tuple(job.values()))
        return {
            "restored_keys": result.get("restored_keys", []),
            "status": "completed",
        }

    # -- handler dispatch --------------------------------------------------

    @property
    def _handlers(self) -> dict[str, callable]:
        return {
            "import": self._process_import,
            "note": self._process_note,
            "chat": self._process_chat,
            "suggestions": self._process_suggestions,
            "slide_draft": self._process_slide_draft,
            "slide_export": self._process_slide_export,
            "transcription": self._process_transcription,
            "visual": self._process_visual,
            "backup": self._process_backup,
            "restore": self._process_restore,
            "diarization_setup": self._process_diarization_setup,
            "diarization": self._process_diarization,
        }
