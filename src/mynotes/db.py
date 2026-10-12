"""MyNotes persistent storage layer — SQLite + FTS5.

Public API
==========
``Database(path: Path)``
    Initialises schema, creates parent dirs, opens WAL + FKs.

Connection discipline
---------------------
Each public method opens its own connection (WAL, FKs), commits on
success, rolls back on error, and closes the connection.  No shared
connection or thread-local state.

Exceptions
----------
* ``NotFound``  – alias of ``ValueError``, raised when an entity by id
  does not exist.
* ``Conflict``  – alias of ``ValueError``, raised on revision conflicts.

UUIDs
-----
All ``id`` columns use ``uuid4`` hex strings (32 chars).

JSON columns
------------
stored as TEXT; ``json.dumps``/``json.loads`` used transparently.
"""

from __future__ import annotations

import json
import pathlib
import re
import sqlite3
import time
import uuid
from collections.abc import Sequence
from contextlib import closing
from typing import Any

__all__ = [
    "Conflict",
    "Database",
    "NotFound",
]


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class NotFound(ValueError):
    """Entity with the given id was not found."""


class Conflict(ValueError):
    """Revision or uniqueness conflict."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_SCHEMA_VERSION = 1


def _uuid() -> str:
    return uuid.uuid4().hex


def _json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _json_loads(text: str | None) -> Any:
    if text is None:
        return None
    return json.loads(text)


# ---------------------------------------------------------------------------
# SQL fragments
# ---------------------------------------------------------------------------

_DOC_SELECT = "SELECT * FROM documents"


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

class Database:
    """Embedded SQLite database with FTS5 full-text search."""

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def __init__(self, path: pathlib.Path) -> None:
        """Create (or open) the SQLite database and ensure the schema."""
        self._path = pathlib.Path(path).resolve()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # -- internal: schema -----------------------------------------------

    def _init_schema(self) -> None:
        with closing(self._conn()) as conn, conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")

            conn.executescript(_SCHEMA_SQL)
            if "deleted_at" not in {row["name"] for row in conn.execute("PRAGMA table_info(documents)")}:
                conn.execute("ALTER TABLE documents ADD COLUMN deleted_at REAL")
            if "conversation_id" not in {row["name"] for row in conn.execute("PRAGMA table_info(chats)")}:
                conn.execute("ALTER TABLE chats ADD COLUMN conversation_id TEXT REFERENCES conversations(id)")
                # Old saves used the retrieval mode, obscuring which turns were summaries.
                for job in conn.execute("SELECT payload, result FROM jobs WHERE kind='chat' AND status='succeeded'"):
                    payload, result = _json_loads(job["payload"]) or {}, _json_loads(job["result"]) or {}
                    if payload.get("mode") in {"audio_summary", "document_summary", "transcript_cleanup"}:
                        conn.execute("UPDATE chats SET mode=? WHERE question=? AND answer=?", (payload["mode"], payload.get("question", ""), result.get("answer", "")))
                old = conn.execute("SELECT MIN(created_at) AS first, MAX(created_at) AS last FROM chats WHERE mode NOT IN ('audio_summary','document_summary','transcript_cleanup')").fetchone()
                if old["first"] is not None:
                    cid = _uuid()
                    conn.execute("INSERT INTO conversations VALUES (?, ?, NULL, ?, ?)", (cid, "Previous assistant conversation", old["first"], old["last"]))
                    conn.execute("UPDATE chats SET conversation_id=? WHERE mode NOT IN ('audio_summary','document_summary','transcript_cleanup')", (cid,))
            conn.execute("CREATE INDEX IF NOT EXISTS chats_conversation ON chats(conversation_id, created_at)")
            ver = conn.execute(
                "SELECT value FROM settings WHERE key='schema_version'"
            ).fetchone()
            if ver is None:
                conn.execute(
                    "INSERT INTO settings (id, key, value) "
                    "VALUES ('settings', 'schema_version', ?)",
                    (str(_SCHEMA_VERSION),),
                )
                conn.commit()

    # -- connection factory ---------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._path))
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = _row_factory
        return conn

    # -- internal: batch helper -----------------------------------------
    # Deferrable FKs require all statements in one transaction.

    def _batch(self, sqls: Sequence[tuple[str, tuple]]) -> None:
        """Execute several statements in a single connection/transaction."""
        with closing(self._conn()) as conn, conn:
            for sql, params in sqls:
                conn.execute(sql, params)
            conn.commit()

    # -- internal: query helpers ----------------------------------------

    def _query(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with closing(self._conn()) as conn, conn:
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    def _one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        with closing(self._conn()) as conn, conn:
            row = conn.execute(sql, params).fetchone()
            return dict(row) if row else None

    def _execute(self, sql: str, params: tuple = ()) -> int:
        with closing(self._conn()) as conn, conn:
            cur = conn.execute(sql, params)
            conn.commit()
            return cur.rowcount

    # -- internal: document fetcher -------------------------------------

    def _get_doc(self, doc_id: str) -> dict[str, Any]:
        doc = self._one(
            _DOC_SELECT + " WHERE documents.id = ?", (doc_id,)
        )
        if doc is None:
            raise NotFound(f"Document {doc_id} not found")
        return doc

    # ------------------------------------------------------------------
    # Courses
    # ------------------------------------------------------------------

    def create_course(self, name: str) -> dict[str, Any]:
        """Create a new course.  Returns the course dict."""
        cid = _uuid()
        self._execute(
            "INSERT INTO courses (id, name) VALUES (?, ?)", (cid, name)
        )
        return self._one("SELECT * FROM courses WHERE id = ?", (cid,))  # type: ignore[return-value]

    def list_courses(self) -> list[dict[str, Any]]:
        """Return all courses."""
        return self._query("SELECT * FROM courses ORDER BY name")

    def delete_course(self, course_id: str) -> None:
        if not self._one("SELECT id FROM courses WHERE id=?", (course_id,)):
            raise NotFound("Course not found")
        self._batch([("UPDATE documents SET course_id=NULL WHERE course_id=?", (course_id,)),
                     ("DELETE FROM courses WHERE id=?", (course_id,))])

    # ------------------------------------------------------------------
    # Documents
    # ------------------------------------------------------------------

    def save_document(
        self,
        title: str,
        kind: str,
        original_path: str,
        blocks: Sequence[dict[str, Any]],
        course_id: str | None = None,
        document_id: str | None = None,
        metadata: Any = None,
        assets: Any = None,
        warnings: Any = None,
    ) -> dict[str, Any]:
        """Save a document (new or update).

        Always returns a consistent VERSION dict::

            {id: version_id, document_id: ..., original_path: ..., ...}

        Creates a new version with new immutable block ids when the
        original_path changes.  Old versions and blocks remain intact
        so existing citations stay valid.  Title and course_id updates
        are committed in a transaction.
        """
        now = time.time()

        # --- create or refresh document row ---------------------------
        if document_id:
            self._get_doc(document_id)
            did = document_id
            # If the original source changed, create a new version.
            current_vid = None  # Every content save creates an immutable version.
        else:
            did = _uuid()
            current_vid = None

        # --- create new version + blocks (only when needed) -----------
        if current_vid is None:
            vid = _uuid()
            sqls: list[tuple[str, tuple]] = []
            # 1. version
            sqls.append(
                (
                    ("INSERT INTO versions "
                    "(id, document_id, original_path, created_at, assets, warnings) "
                    "VALUES (?, ?, ?, ?, ?, ?)"),
                    (
                        vid,
                        did,
                        original_path,
                        now,
                        _json_dumps(assets),
                        _json_dumps(warnings),
                    ),
                )
            )
            # 2. blocks
            for b in blocks:
                bid = _uuid()
                sqls.append(
                    (
                        ("INSERT INTO blocks "
                        "(id, version_id, text, locator, origin) "
                        "VALUES (?, ?, ?, ?, ?)"),
                        (
                            bid,
                            vid,
                            b.get("text", ""),
                            _json_dumps(b.get("locator")),
                            b.get("origin"),
                        ),
                    )
                )
            # 3. document (insert first with NULL version, then update)
            if not document_id:
                sqls.append(
                    (
                        ("INSERT INTO documents "
                        "(id, title, kind, course_id, created_at, current_version_id) "
                        "VALUES (?, ?, ?, ?, ?, ?)"),
                        (did, title, kind, course_id, now, vid),
                    )
                )
            else:
                sqls.append(
                    (
                        ("UPDATE documents SET current_version_id = ?, title = ?, kind = ?, "
                        "course_id = ? WHERE id = ?"),
                        (vid, title, kind, course_id, did),
                    )
                )
            self._batch(sqls)
            return self.get_version(vid)

    def get_document(self, doc_id: str) -> dict[str, Any]:
        """Return document with current version, blocks, assets."""
        doc = self._get_doc(doc_id)
        if doc.get("deleted_at") is not None:
            raise NotFound("Document was deleted from the library")
        vid = doc["current_version_id"]
        version = self.get_version(vid)
        for field in ("blocks", "assets", "warnings", "original_path"):
            doc[field] = version[field]
        return doc

    def get_version(self, version_id: str) -> dict[str, Any]:
        """Return an immutable version and its blocks."""
        ver = self._one(
            "SELECT * FROM versions WHERE id = ?", (version_id,)
        )
        if ver is None:
            raise NotFound(f"Version {version_id} not found")
        ver["blocks"] = self._query(
            "SELECT b.id, b.text, b.locator, b.origin, b.version_id, "
            "v.document_id, d.title FROM blocks b JOIN versions v ON v.id=b.version_id "
            "JOIN documents d ON d.id=v.document_id WHERE b.version_id = ? ORDER BY b.rowid",
            (version_id,),
        )
        ver["assets"] = _json_loads(ver.get("assets")) or []
        ver["warnings"] = _json_loads(ver.get("warnings")) or []
        for block in ver["blocks"]:
            block["locator"] = _json_loads(block["locator"])
        return ver

    def list_documents(
        self, course_id: str | None = None
    ) -> list[dict[str, Any]]:
        """List documents, optionally filtered by course."""
        if course_id:
            return self._query(
                "SELECT id, title, kind, course_id, chapter, class_date, "
                "created_at, current_version_id FROM documents "
                "WHERE deleted_at IS NULL AND course_id = ? ORDER BY title",
                (course_id,),
            )
        return self._query(
            "SELECT id, title, kind, course_id, chapter, class_date, "
            "created_at, current_version_id FROM documents WHERE deleted_at IS NULL ORDER BY title"
        )

    def delete_document(self, document_id: str) -> None:
        # Preserve immutable evidence used by existing chats; remove from the library/search.
        if not self._execute("UPDATE documents SET deleted_at=? WHERE id=? AND deleted_at IS NULL", (time.time(), document_id)):
            raise NotFound("Document not found")

    def update_document(
        self,
        doc_id: str,
        fields: dict[str, Any],
    ) -> dict[str, Any]:
        """Update mutable document fields (title, course_id, chapter,
        class_date).  If *original_path* is provided and differs from
        the current version's path a new version is created."""
        doc = self._get_doc(doc_id)
        vid = doc["current_version_id"]

        # Check for original_path change → new version
        op = fields.get("original_path")
        if op is not None:
            path_row = self._one(
                "SELECT id FROM versions WHERE document_id = ? AND original_path = ?",
                (doc_id, op),
            )
            if path_row is None:
                # Need to refresh blocks from the new original_path value
                # passed as 'blocks' in fields (if supplied).
                new_blocks = fields.pop("blocks", [])
                now = time.time()
                self._execute(
                    "INSERT INTO versions "
                    "(id, document_id, original_path, created_at, assets, warnings) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        _uuid(),
                        doc_id,
                        op,
                        now,
                        _json_dumps(fields.pop("assets")),
                        _json_dumps(fields.pop("warnings")),
                    ),
                )
                for b in new_blocks:
                    self._execute(
                        "INSERT INTO blocks "
                        "(id, version_id, text, locator, origin) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (
                            _uuid(),
                            vid,  # will be updated to new vid below
                            b.get("text", ""),
                            _json_dumps(b.get("locator")),
                            b.get("origin"),
                        ),
                    )
                # Update doc current_version_id
                new_vid = self._one(
                    "SELECT id FROM versions WHERE document_id = ? AND original_path = ?",
                    (doc_id, op),
                )
                self._execute(
                    "UPDATE documents SET current_version_id = ? WHERE id = ?",
                    (new_vid["id"], doc_id),
                )
                return self.get_version(new_vid["id"])

        # Standard field updates
        if "title" in fields:
            self._execute(
                "UPDATE documents SET title = ? WHERE id = ?",
                (fields["title"], doc_id),
            )
        if "course_id" in fields:
            self._execute(
                "UPDATE documents SET course_id = ? WHERE id = ?",
                (fields["course_id"], doc_id),
            )
        if "chapter" in fields:
            self._execute(
                "UPDATE documents SET chapter = ? WHERE id = ?",
                (fields["chapter"], doc_id),
            )
        if "class_date" in fields:
            self._execute(
                "UPDATE documents SET class_date = ? WHERE id = ?",
                (fields["class_date"], doc_id),
            )

        return self._get_doc(doc_id)

    # ------------------------------------------------------------------
    # Search (FTS5)
    # ------------------------------------------------------------------

    def search(
        self,
        q: str,
        course_id: str | None = None,
        document_ids: Sequence[str] | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """FTS5 search over block text, rank by BM25.

        Returns a list of matching block dicts (no truncated text).
        """
        if not q.strip():
            return []
        safe = _safe_fts_query(q)
        if not safe:
            return []

        # Join blocks_fts ON rowid=b.rowid so we can MATCH on the FTS table
        # and filter to CURRENT versions only (v.id = d.current_version_id).
        base_sql = (
            "SELECT b.id, b.text, b.locator, b.origin, "
            "v.id AS version_id, v.document_id, d.title, "
            "bm25(blocks_fts) AS rank "
            "FROM blocks b "
            "JOIN versions v ON v.id = b.version_id "
            "JOIN documents d ON d.id = v.document_id "
            "JOIN blocks_fts ON blocks_fts.rowid = b.rowid "
            "WHERE v.id = d.current_version_id AND d.deleted_at IS NULL "
            "AND blocks_fts MATCH ? "
        )
        params: list[Any] = [safe]

        if course_id:
            base_sql += "AND d.course_id = ? "
            params.append(course_id)
        if document_ids:
            placeholders = ",".join("?" for _ in document_ids)
            base_sql += f"AND v.document_id IN ({placeholders}) "
            params.extend(document_ids)

        base_sql += "ORDER BY rank ASC LIMIT ?"
        params.append(min(limit, 200))

        rows = self._query(base_sql, tuple(params))
        for row in rows:
            row["locator"] = _json_loads(row["locator"])
        return rows

    # ------------------------------------------------------------------
    # Material (context retrieval)
    # ------------------------------------------------------------------

    def material(
        self,
        document_ids: Sequence[str] = [],
        course_id: str | None = None,
        query: str = "",
        budget: int = 24000,
    ) -> dict[str, Any]:
        """Retrieve material from selected documents/courses.

        Returns dict with ``mode``, ``blocks``, ``warnings``,
        ``estimated_tokens``.  Blocks are appended until the budget is
        full; if no query is given full document text is returned.
        Never silently truncates a block — if the budget is reached a
        ``warnings`` list is populated.

        Current-version filter (v.id = d.current_version_id) ensures
        only the latest version of each document is retrieved.
        """
        sql = (
            "SELECT b.id, b.text, b.locator, b.origin, v.id AS version_id, "
            "v.document_id, d.title FROM blocks b "
            "JOIN versions v ON v.id=b.version_id JOIN documents d ON d.id=v.document_id "
            "WHERE v.id=d.current_version_id AND d.deleted_at IS NULL "
        )
        params: list[Any] = []
        if document_ids:
            sql += "AND d.id IN (" + ",".join("?" for _ in document_ids) + ") "
            params.extend(document_ids)
        if course_id:
            sql += "AND d.course_id=? "
            params.append(course_id)
        rows = self._query(sql + "ORDER BY d.id, b.rowid", tuple(params)) if document_ids or course_id else []
        # ponytail: conservative byte-based estimate; replace with model tokenizer if needed.
        def cost(row: dict) -> int:
            return (len((row["text"] or "").encode("utf-8")) + 2) // 3 + 20

        total = sum(cost(row) for row in rows)
        mode = "full"
        warnings: list[str] = []
        if total > budget:
            mode = "lexical"
            warnings.append("Context budget exceeded; using scoped lexical passages, not full documents.")
            rows = self.search(query, course_id=course_id, document_ids=document_ids, limit=200)
        results = []
        total = 0
        for row in rows:
            if total + cost(row) > budget:
                continue
            total += cost(row)
            locator = row["locator"]
            if isinstance(locator, str):
                locator = _json_loads(locator)
            results.append({**row, "locator": locator, "source_id": row["id"], "content": row["text"], "excerpt": row["text"]})
        if not results:
            warnings.append("No selected matching material; zero evidence returned.")
        assets = []
        for version_id in dict.fromkeys(row["version_id"] for row in results):
            version = self.get_version(version_id)
            assets.extend({**asset, "version_id": version_id} for asset in version["assets"] if isinstance(asset, dict))
        return {"mode": mode, "blocks": results, "warnings": warnings, "estimated_tokens": total, "assets": assets}

    # ------------------------------------------------------------------
    # Jobs
    # ------------------------------------------------------------------

    def create_job(
        self,
        kind: str,
        payload: Any,
    ) -> dict[str, Any]:
        """Create a new queued job.  Returns the job dict."""
        now = time.time()
        jid = _uuid()
        self._execute(
            "INSERT INTO jobs "
            "(id, kind, payload, status, progress, attempts, created_at, updated_at) "
            "VALUES (?, ?, ?, 'queued', 0.0, 0, ?, ?)",
            (jid, kind, _json_dumps(payload), now, now),
        )
        return self._one(
            "SELECT * FROM jobs WHERE id = ?", (jid,)
        )  # type: ignore[return-value]

    def get_job(self, job_id: str) -> dict[str, Any]:
        """Return a job by id, or raise ``NotFound``."""
        job = self._one(
            "SELECT * FROM jobs WHERE id = ?", (job_id,)
        )
        if job is None:
            raise NotFound(f"Job {job_id} not found")
        return job

    def list_jobs(
        self,
        kind: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """List jobs, optionally filtered by kind/status."""
        sql = "SELECT * FROM jobs WHERE 1=1"
        params: list[Any] = []
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC"
        return self._query(sql, tuple(params))

    def claim_job(
        self,
        kind: str,
        worker_id: str = "default",
    ) -> dict[str, Any] | None:
        """Atomically claim the next queued job for *kind*.

        Uses BEGIN IMMEDIATE so the SELECT + UPDATE happen in a single
        locked transaction on one connection — no two workers can claim
        the same job.

        Returns the job dict or ``None`` if no job is available.
        """
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            job = conn.execute(
                "SELECT * FROM jobs WHERE kind = ? AND status = 'queued' "
                "ORDER BY created_at ASC LIMIT 1",
                (kind,),
            ).fetchone()
            if job is None:
                conn.execute("ROLLBACK")
                return None
            job = dict(job)
            conn.execute(
                "UPDATE jobs SET status = 'running', "
                "worker_id = ?, updated_at = ?, "
                "attempts = attempts + 1 "
                "WHERE id = ?",
                (worker_id, time.time(), job["id"]),
            )
            conn.commit()
            return self._one("SELECT * FROM jobs WHERE id = ?", (job["id"],))
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def finish_job(
        self,
        job_id: str,
        result: Any = None,
    ) -> None:
        """Mark a running job as succeeded with an optional result."""
        self._execute(
            "UPDATE jobs SET status = 'succeeded', "
            "progress = 1.0, result = ?, "
            "updated_at = ? WHERE id = ?",
            (_json_dumps(result), time.time(), job_id),
        )

    def fail_job(
        self,
        job_id: str,
        error: str,
    ) -> None:
        """Mark a running job as failed."""
        self._execute(
            "UPDATE jobs SET status = 'failed', error = ?, "
            "updated_at = ? WHERE id = ?",
            (error, time.time(), job_id),
        )

    def retry_job(self, job_id: str) -> dict[str, Any]:
        """Reset a failed/finished job back to 'queued' for retry."""
        job = self.get_job(job_id)
        if job["status"] not in ("failed", "succeeded"):
            raise ValueError(
                f"Cannot retry job in state '{job['status']}'"
            )
        self._execute(
            "UPDATE jobs SET status = 'queued', progress = 0.0, "
            "error = '', result = NULL, attempts = attempts + 1, "
            "updated_at = ? WHERE id = ?",
            (time.time(), job_id),
        )
        return self._one("SELECT * FROM jobs WHERE id = ?", (job_id,))

    def recover_jobs(
        self,
        max_age: float = 3600.0,
        max_retries: int = 3,
    ) -> int:
        """Recover interrupted running jobs after a crash.

        Only jobs in ``'running'`` state older than *max_age* AND with
        ``attempts < max_retries`` are recovered.  Failed jobs are NOT
        automatically requeued — they require explicit retry.

        Returns the count of jobs that were changed from running→queued.
        """
        now = time.time()
        recovered = self._execute(
            "UPDATE jobs SET status = 'queued', error = 'interrupted', "
            "updated_at = ? "
            "WHERE status = 'running' AND updated_at < ? "
            "AND attempts < ?",
            (now, now - max_age, max_retries),
        )
        return recovered  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Decks
    # ------------------------------------------------------------------

    def save_deck(
        self,
        title: str,
        markdown: str,
        source_ids: Sequence[str],
        deck_id: str | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Save (create or update) a deck.

        Validates *source_ids* against existing block ids.  Increments
        revision; raises ``Conflict`` when *expected_revision* does not
        match the current revision.

        Uses ``BEGIN IMMEDIATE`` so the conditional revision check and
        UPDATE happen atomically within a single transaction, preventing
        stale-concurrency overwrites.
        """
        # Validate source_ids exist
        if source_ids:
            placeholders = ",".join("?" for _ in source_ids)
            rows = self._query(
                f"SELECT COUNT(*) AS c FROM blocks WHERE id IN ({placeholders})",
                tuple(source_ids),
            )
            found = rows[0]["c"]
            if found != len(source_ids):
                raise ValueError(
                    f"{len(source_ids) - found} source block(s) not found "
                    f"({len(source_ids)} provided, {found} exist)"
                )

        if deck_id:
            conn = self._conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                deck_row = conn.execute(
                    "SELECT * FROM decks WHERE id = ?", (deck_id,)
                ).fetchone()
                if deck_row is None:
                    conn.execute("ROLLBACK")
                    raise NotFound(f"Deck {deck_id} not found")
                deck_row = dict(deck_row)
                rev = deck_row["revision"]
                if expected_revision is not None and rev != expected_revision:
                    conn.execute("ROLLBACK")
                    raise Conflict(
                        f"Revision conflict: expected {expected_revision}, "
                        f"got {rev}"
                    )
                new_rev = rev + 1
                conn.execute(
                    "UPDATE decks SET title = ?, markdown = ?, "
                    "source_ids = ?, revision = ?, updated_at = ? "
                    "WHERE id = ?",
                    (
                        title,
                        markdown,
                        _json_dumps(list(source_ids)),
                        new_rev,
                        time.time(),
                        deck_id,
                    ),
                )
                conn.commit()
                return self._one(
                    "SELECT * FROM decks WHERE id = ?", (deck_id,)
                )  # type: ignore[return-value]
            except (NotFound, Conflict):
                raise
            except Exception:
                conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()

        did = _uuid()
        self._execute(
            "INSERT INTO decks "
            "(id, title, markdown, source_ids, revision, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 1, ?, ?)",
            (
                did,
                title,
                markdown,
                _json_dumps(list(source_ids)),
                time.time(),
                time.time(),
            ),
        )
        return self._one("SELECT * FROM decks WHERE id = ?", (did,))

    def get_deck(self, deck_id: str) -> dict[str, Any]:
        """Return a deck by id, or raise ``NotFound``."""
        deck = self._one(
            "SELECT * FROM decks WHERE id = ?", (deck_id,)
        )
        if deck is None:
            raise NotFound(f"Deck {deck_id} not found")
        deck["source_ids"] = _json_loads(deck.get("source_ids")) or []
        return deck

    def list_decks(self) -> list[dict[str, Any]]:
        """Return all decks."""
        decks = self._query(
            "SELECT * FROM decks ORDER BY updated_at DESC"
        )
        for d in decks:
            d["source_ids"] = _json_loads(d.get("source_ids")) or []
        return decks

    # ------------------------------------------------------------------
    # Chat
    # ------------------------------------------------------------------

    def create_conversation(self, title: str = "New conversation") -> dict[str, Any]:
        cid, now = _uuid(), time.time()
        self._execute("INSERT INTO conversations VALUES (?, ?, NULL, ?, ?)", (cid, title, now, now))
        return self.get_conversation(cid)

    def get_conversation(self, conversation_id: str) -> dict[str, Any]:
        row = self._one("SELECT * FROM conversations WHERE id=?", (conversation_id,))
        if row is None:
            raise NotFound("Conversation not found")
        return row

    def list_conversations(self) -> list[dict[str, Any]]:
        return self._query("SELECT * FROM conversations ORDER BY updated_at DESC, id")

    def delete_conversation(self, conversation_id: str) -> None:
        with closing(self._conn()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT id FROM conversations WHERE id=?", (conversation_id,)).fetchone() is None:
                raise NotFound("Conversation not found")
            if conn.execute("SELECT id FROM jobs WHERE status IN ('queued','running') AND json_extract(payload,'$.conversation_id')=?", (conversation_id,)).fetchone():
                raise Conflict("This conversation is still processing a reply. Wait for it to finish before deleting it.")
            conn.execute("DELETE FROM chats WHERE conversation_id=?", (conversation_id,))
            conn.execute("DELETE FROM conversations WHERE id=?", (conversation_id,))

    def save_chat(
        self,
        question: str,
        answer: str,
        citations: Any,
        mode: str,
        conversation_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Save a chat turn.  Returns the chat record."""
        now = time.time()
        cid = _uuid()
        if mode in {"audio_summary", "document_summary", "transcript_cleanup"}:
            conversation_id = None
        sqls = [("INSERT INTO chats (id, question, answer, citations, mode, created_at, conversation_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (cid, question, answer, _json_dumps(citations), mode, now, conversation_id))]
        if conversation_id:
            self.get_conversation(conversation_id)
            sqls.append(("UPDATE conversations SET title=CASE WHEN title='New conversation' THEN ? ELSE title END, session_id=?, updated_at=? WHERE id=?",
                         (question[:80], session_id, now, conversation_id)))
        self._batch(sqls)
        return self._one("SELECT * FROM chats WHERE id = ?", (cid,))

    def list_chats(self, conversation_id: str | None = None) -> list[dict[str, Any]]:
        """Return chat history ordered by creation."""
        chats = self._query(
            "SELECT id, question, answer, citations, mode, created_at, conversation_id "
            "FROM chats WHERE mode NOT IN ('audio_summary','document_summary','transcript_cleanup')"
            + (" AND conversation_id=?" if conversation_id else "") + " ORDER BY created_at ASC, rowid",
            (conversation_id,) if conversation_id else (),
        )
        for c in chats:
            c["citations"] = _json_loads(c.get("citations")) or []
        return chats

    # ------------------------------------------------------------------
    # Settings (singleton)
    # ------------------------------------------------------------------

    def get_settings(self) -> dict[str, Any]:
        """Return application settings dict."""
        row = self._one(
            "SELECT value FROM settings WHERE key = 'app_settings'"
        )
        if row is None:
            return {}
        return _json_loads(row["value"]) or {}

    def set_settings(self, settings: dict[str, Any]) -> None:
        """Overwrite application settings."""
        self._execute(
            "INSERT OR REPLACE INTO settings (id, key, value) "
            "VALUES ('settings', 'app_settings', ?)",
            (_json_dumps(settings),),
        )


# ---------------------------------------------------------------------------
# Row factory — make sqlite3.Row behave like a dict
# ---------------------------------------------------------------------------

def _row_factory(
    cursor: sqlite3.Cursor,
    row: sqlite3.Row,
) -> dict[str, Any]:
    """Convert a ``sqlite3.Row`` to a plain dict keyed by column name."""
    if cursor.description is None:
        return {}
    return {desc[0]: row[idx] for idx, desc in enumerate(cursor.description)}


# ---------------------------------------------------------------------------
# Schema SQL (run once via ``executescript``)
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """\
-- Courses
CREATE TABLE IF NOT EXISTS courses (
    id   TEXT PRIMARY KEY,
    name TEXT NOT NULL
);

-- Documents (one row per document)
CREATE TABLE IF NOT EXISTS documents (
    id                    TEXT PRIMARY KEY,
    title                 TEXT NOT NULL,
    kind                  TEXT NOT NULL,
    course_id             TEXT,
    chapter               TEXT,
    class_date            TEXT,
    created_at            REAL NOT NULL,
    current_version_id    TEXT,
    FOREIGN KEY (course_id) REFERENCES courses(id)
);

-- Versions (immutable snapshots)
CREATE TABLE IF NOT EXISTS versions (
    id           TEXT PRIMARY KEY,
    document_id  TEXT NOT NULL,
    original_path TEXT,
    created_at   REAL NOT NULL,
    assets       TEXT,
    warnings     TEXT
);

-- Blocks (immutable, owned by a version)
CREATE TABLE IF NOT EXISTS blocks (
    id         TEXT PRIMARY KEY,
    version_id TEXT NOT NULL,
    text       TEXT NOT NULL DEFAULT '',
    locator    TEXT,   -- JSON object
    origin     TEXT,
    FOREIGN KEY (version_id) REFERENCES versions(id) ON DELETE CASCADE
);

-- Index on original_path for version lookup
CREATE INDEX IF NOT EXISTS idx_versions_doc_path
    ON versions(document_id, original_path);

-- FTS5 virtual table for block full-text search
CREATE VIRTUAL TABLE IF NOT EXISTS blocks_fts
    USING fts5(text, content='blocks', content_rowid='rowid');

-- Trigger: insert new blocks into FTS
CREATE TRIGGER IF NOT EXISTS blocks_ai AFTER INSERT ON blocks BEGIN
    INSERT INTO blocks_fts(rowid, text)
        VALUES (NEW.rowid, NEW.text);
END;

-- Trigger: delete blocks from FTS
CREATE TRIGGER IF NOT EXISTS blocks_ad AFTER DELETE ON blocks BEGIN
    DELETE FROM blocks_fts WHERE rowid = OLD.rowid;
END;

-- Trigger: update blocks in FTS
CREATE TRIGGER IF NOT EXISTS blocks_au AFTER UPDATE ON blocks BEGIN
    DELETE FROM blocks_fts WHERE rowid = OLD.rowid;
    INSERT INTO blocks_fts(rowid, text)
        VALUES (NEW.rowid, NEW.text);
END;

-- Jobs (asynchronous work)
CREATE TABLE IF NOT EXISTS jobs (
    id         TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    payload    TEXT,  -- JSON
    status     TEXT NOT NULL DEFAULT 'queued',
    worker_id  TEXT,
    progress   REAL NOT NULL DEFAULT 0.0,
    error      TEXT,
    result     TEXT,  -- JSON
    attempts   INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    CHECK (status IN ('queued', 'running', 'succeeded', 'failed'))
);

-- Decks (slides / presentations)
CREATE TABLE IF NOT EXISTS decks (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    markdown    TEXT NOT NULL,
    source_ids  TEXT,  -- JSON array of block ids
    revision    INTEGER NOT NULL DEFAULT 1,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);

-- Chat history
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    session_id TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS chats (
    id         TEXT PRIMARY KEY,
    question   TEXT NOT NULL,
    answer     TEXT NOT NULL,
    citations  TEXT,  -- JSON array
    mode       TEXT NOT NULL,
    created_at REAL NOT NULL
);

-- Settings (singleton; key = 'app_settings')
CREATE TABLE IF NOT EXISTS settings (
    id    TEXT NOT NULL DEFAULT 'settings',
    key   TEXT NOT NULL,
    value TEXT,
    PRIMARY KEY (id, key)
);
"""


# ---------------------------------------------------------------------------
# FTS5 query sanitiser
# ---------------------------------------------------------------------------

def _safe_fts_query(raw: str) -> str | None:
    """Build a safe FTS5 boolean query from untrusted user input.

    - Strips control characters.
    - Requires every remaining word to be ≥ 3 chars.
    - Joins words with ``|`` (OR) — the default boolean query uses AND.
    - Returns ``None`` when no valid words remain.
    """
    # Strip zero-width / control chars that break FTS parsing
    clean = re.sub(r"[\x00-\x1f\x7f-\x9f\u200b\u200c\u200d]", " ", raw)
    # Split on whitespace and filter short tokens
    words = [w for w in clean.split() if len(w) >= 3]
    if not words:
        return None
    # Quote each token to handle embedded operators (+-@~"()*)
    quoted = [f'"{w}"' for w in words]
    return " | ".join(quoted)
