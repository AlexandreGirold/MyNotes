"""MyNotes backup / restore — consistent SQLite snapshot + file manifest.

Public API
==========
``backup(data_dir, destination) -> Path``
    Create a zip archive containing a consistent SQLite snapshot
    (``library.sqlite3``) and all referenced managed originals/derived
    files.  Credentials and runtime files are excluded.

``restore(data_dir, archive) -> dict``
    Validate the archive schema, extract files, check DB integrity,
    replace the live database, and restore managed files.  Roll back
    on any failure.

``safe_extract(archive, destination) -> None``
    Extract a zip archive after the same validation, used for Typst
    uploads and any other untrusted-zip scenario.

Design notes
============
- The SQLite database is copied via the ``sqlite3`` connection-level
  ``backup()`` API so we get a consistent snapshot even while the
  application holds open handles.
- The archive carries a JSON metadata block (``__mynotes_meta.json``)
  describing every included file with its source path, relative path,
  and size.
- No credential, key, or runtime files are ever archived or restored.
"""

from __future__ import annotations

import json
import logging
import pathlib
import shutil
import sqlite3
import stat
import tempfile
import time
import zipfile
from typing import Any

__all__ = ["backup", "restore", "safe_extract"]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DB_NAME = "library.sqlite3"
_META_NAME = "__mynotes_meta.json"

# Maximum total archive size: 1 GiB
MAX_ARCHIVE_SIZE = 1_073_741_824
# Maximum individual extracted file size: 200 MiB
MAX_ENTRY_SIZE = 200 * 1024 * 1024
# Maximum number of entries (zipbomb heuristic)
MAX_ENTRY_COUNT = 50_000

# Files / patterns excluded from backup
EXCLUDE_NAMES = frozenset([
    "credentials.enc",
    "credentials",
    "keys",
    "key",
    ".env",
])
EXCLUDE_SUFFIXES = frozenset([
    ".bak",
    ".backup",
    ".tmp",
    ".pyc",
    "__pycache__",
])


# ---------------------------------------------------------------------------
# Internal: path-safety checks
# ---------------------------------------------------------------------------

def _reject_path(entry_name: str, base: pathlib.Path) -> None:
    """Reject paths that escape *base* via traversal, absolute paths,
    or Windows drive letters."""
    if pathlib.PurePath(entry_name).is_absolute():
        raise ValueError(f"absolute path in archive: {entry_name!r}")

    parts = pathlib.PurePath(entry_name).parts
    if ".." in parts:
        raise ValueError(f"path traversal in archive: {entry_name!r}")

    # Reject Windows drive letters (C:, D:, etc.)
    if len(entry_name) >= 2 and entry_name[1] == ":":
        raise ValueError(f"Windows drive letter in archive: {entry_name!r}")

    resolved = (base / entry_name).resolve()
    try:
        resolved.relative_to(base.resolve())
    except ValueError:
        raise ValueError(f"path escapes destination: {entry_name!r}")


def _is_excluded(name: str) -> bool:
    """Return True if *name* should be excluded from backups."""
    stem = pathlib.PurePath(name).name
    if stem in EXCLUDE_NAMES:
        return True
    for suffix in EXCLUDE_SUFFIXES:
        if name.endswith(suffix) or stem == suffix:
            return True
    return name.endswith(".zip")


def _scan_excluded_dirs() -> frozenset[str]:
    """Return directory names that should be skipped during scanning."""
    return frozenset(
        EXCLUDE_NAMES | EXCLUDE_SUFFIXES | {"__pycache__", "jobs", "backups", "snapshot_tmp", "agent", "models", "temporary-audio"},
    )


# ---------------------------------------------------------------------------
# backup
# ---------------------------------------------------------------------------

def backup(data_dir: pathlib.Path, destination: pathlib.Path) -> pathlib.Path:
    """Create a consistent SQLite snapshot + file manifest in a zip archive.

    Parameters
    ----------
    data_dir:
        The MyNotes data directory (contains ``library.sqlite3``).
    destination:
        Directory where the ``.zip`` archive will be written.

    Returns
    -------
    pathlib.Path
        Absolute path to the created archive.
    """
    data_dir = pathlib.Path(data_dir).resolve()
    destination = pathlib.Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)

    db_path = data_dir / _DB_NAME
    if not db_path.is_file():
        raise FileNotFoundError(f"Database not found: {db_path}")

    # --- Build file manifest -------------------------------------------
    manifest: list[dict[str, Any]] = []

    # Walk data_dir, collecting regular files (skip symlinks, excludes)
    excluded_dirs = _scan_excluded_dirs()
    for root, dirs, files in _walk_excluding(data_dir, excluded_dirs):
        dirs[:] = [d for d in dirs if d not in excluded_dirs]
        for fname in sorted(files):
            fpath = pathlib.Path(root) / fname
            if fpath.is_symlink():
                continue
            if _is_excluded(fname):
                continue
            if fname in {_DB_NAME, _DB_NAME + "-wal", _DB_NAME + "-shm", ".mynotes.lock", "runtime.json"}:
                continue
            rel = fpath.relative_to(data_dir)
            manifest.append({
                "source_path": str(fpath),
                "archive_path": str(rel),
                "size": fpath.stat().st_size,
            })

    # --- Snapshot the database via sqlite3.backup() --------------------
    snap_path = destination / "snapshot_tmp" / _DB_NAME
    snap_path.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(str(db_path)) as src_conn, sqlite3.connect(str(snap_path)) as dst_conn:
        src_conn.backup(dst_conn)

    snap_size = snap_path.stat().st_size
    manifest.append({
        "source_path": str(db_path),
        "archive_path": _DB_NAME,
        "size": snap_size,
    })

    # --- Write zip archive ---------------------------------------------
    archive_path = destination / f"mynotes_backup_{time.strftime('%Y%m%d_%H%M%S')}.zip"
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as zf:
        # Write DB snapshot first
        zf.write(snap_path, _DB_NAME)
        # Write manifest entry for DB
        # Then all other files
        for entry in manifest:
            if entry["archive_path"] == _DB_NAME:
                continue
            src = pathlib.Path(entry["source_path"])
            if src.exists():
                zf.write(src, entry["archive_path"])

    # Write the manifest as the last archive entry
    meta = {
        "version": 1,
        "timestamp": time.time(),
        "data_dir": str(data_dir),
        "files": manifest,
    }
    with zipfile.ZipFile(archive_path, "a", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(_META_NAME, json.dumps(meta, ensure_ascii=False, indent=2))

    # Clean up temp snapshot
    shutil.rmtree(snap_path.parent)

    return archive_path


def _walk_excluding(
    root: pathlib.Path,
    exclude: frozenset[str],
):
    """Walk *root* yielding (dirpath, dirnames, filenames) but skip
    directories whose name is in *exclude*."""
    yield from _walk_excluding_gen(root, exclude)


def _walk_excluding_gen(
    root: pathlib.Path,
    exclude: frozenset[str],
):
    """Generator version of _walk_excluding."""
    try:
        entries = sorted(root.iterdir())
    except PermissionError:
        return
    dirs = []
    files = []
    for e in entries:
        if e.is_dir() and not e.is_symlink() and e.name not in exclude:
            dirs.append(e.name)
        elif e.is_file() and not e.is_symlink():
            files.append(e.name)
    yield root, dirs, files
    for d in dirs:
        yield from _walk_excluding_gen(root / d, exclude)


# ---------------------------------------------------------------------------
# Restore helpers
# ---------------------------------------------------------------------------

def _validate_archive(archive: pathlib.Path) -> dict[str, Any]:
    """Validate and read the metadata from *archive*.

    Raises
    ------
    ValueError
        If the archive is malformed, contains invalid entries, or
        violates size/safety constraints.
    """
    try:
        st = archive.stat()
    except OSError:
        raise FileNotFoundError(f"Archive not found: {archive}")

    # Check archive file size
    archive_size = st.st_size
    if archive_size > MAX_ARCHIVE_SIZE:
        raise ValueError(
            f"Archive exceeds maximum size ({MAX_ARCHIVE_SIZE} bytes): "
            f"{archive_size}"
        )

    with zipfile.ZipFile(archive, "r") as zf:
        # Read metadata
        if _META_NAME not in zf.namelist():
            raise ValueError("Archive missing metadata file")
        meta_raw = zf.read(_META_NAME)
        meta: dict[str, Any] = json.loads(meta_raw)

        # Validate schema version
        if meta.get("version") != 1:
            raise ValueError(f"Unsupported backup schema version: {meta.get('version')}")

        if "data_dir" not in meta:
            raise ValueError("Missing data_dir in metadata")

        files_meta = meta.get("files", [])
        if not files_meta:
            raise ValueError("No files in backup metadata")

        # Validate individual file entries from metadata
        total_extracted = 0
        seen_paths: set[str] = set()
        for idx, fentry in enumerate(files_meta, start=1):
            archive_path = fentry.get("archive_path", "")

            if idx > MAX_ENTRY_COUNT:
                raise ValueError(
                    f"Too many entries ({idx}); possible zipbomb"
                )

            # Reject duplicates
            if archive_path in seen_paths:
                raise ValueError(f"Duplicate archive path in metadata: {archive_path!r}")
            seen_paths.add(archive_path)

            # Reject symlinks / special entries in the zip listing
            if archive_path.endswith("/") or any(
                p == "" or p == ".." for p in pathlib.PurePath(archive_path).parts
            ):
                raise ValueError(f"Invalid archive path: {archive_path!r}")

            # Check individual file size from metadata
            fsize = fentry.get("size", 0)
            if fsize > MAX_ENTRY_SIZE:
                raise ValueError(
                    f"File {archive_path!r} exceeds max size "
                    f"({MAX_ENTRY_SIZE} bytes): {fsize}"
                )
            total_extracted += fsize

            # Check against the actual zip info
            try:
                info = zf.getinfo(archive_path)
            except KeyError:
                raise ValueError(f"File {archive_path!r} in metadata but not in archive")

            # Reject size disagreement: actual file must not exceed limits
            if info.file_size > MAX_ENTRY_SIZE:
                raise ValueError(
                    f"File {archive_path!r} exceeds max size "
                    f"({MAX_ENTRY_SIZE} bytes): {info.file_size}"
                )
            # Reject size disagreement: actual file must match metadata closely
            if fsize > 0 and info.file_size > 0 and abs(info.file_size - fsize) > fsize:
                raise ValueError(
                    f"File {archive_path!r}: metadata size {fsize} differs "
                    f"from actual {info.file_size}"
                )
            # Zipbomb check: compressed size vs uncompressed
            if info.compress_size > 0:
                ratio = info.file_size / info.compress_size
                if ratio > 1000 and info.file_size > 10_000_000:
                    raise ValueError(
                        f"File {archive_path!r} has extreme compression "
                        f"ratio ({ratio:.1f}); possible zipbomb"
                    )

        # Validate ALL actual zip entries (not just metadata-listed ones)
        for info in zf.infolist():
            if info.filename == _META_NAME:
                continue
            if info.filename in seen_paths:
                continue
            # Unlisted entry: apply full safety checks
            if info.file_size > MAX_ENTRY_SIZE:
                raise ValueError(
                    f"Unlisted file {info.filename!r} exceeds max size "
                    f"({MAX_ENTRY_SIZE} bytes): {info.file_size}"
                )
            if stat.S_ISLNK(info.external_attr >> 16):
                raise ValueError(
                    f"Symlink in archive (unlisted): {info.filename!r}"
                )
            if info.compress_size > 0:
                ratio = info.file_size / info.compress_size
                if ratio > 1000 and info.file_size > 10_000_000:
                    raise ValueError(
                        f"Unlisted file {info.filename!r} has extreme compression "
                        f"ratio ({ratio:.1f}); possible zipbomb"
                    )

        if total_extracted > MAX_ARCHIVE_SIZE:
            raise ValueError(
                f"Total extracted size exceeds maximum ({MAX_ARCHIVE_SIZE} bytes): "
                f"{total_extracted}"
            )

    return meta


# ---------------------------------------------------------------------------
# restore
# ---------------------------------------------------------------------------

def restore(data_dir: pathlib.Path, archive: pathlib.Path) -> dict[str, Any]:
    """Restore a MyNotes backup into *data_dir*.

    Validates the archive, checks DB integrity, replaces the database,
    and restores managed files.  On *any* failure the existing database
    and files are rolled back.

    Parameters
    ----------
    data_dir:
        The MyNotes data directory to restore into.
    archive:
        Path to the backup zip archive.

    Returns
    -------
    dict
        Summary of the restore operation with keys ``status``,
        ``files_restored``, ``database_restored``.

    Raises
    ------
    ValueError
        If the archive fails validation.
    """
    data_dir = pathlib.Path(data_dir).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    archive = pathlib.Path(archive).resolve()

    # --- Validate archive ----------------------------------------------
    meta = _validate_archive(archive)
    files_meta: list[dict[str, Any]] = meta["files"]

    # Build lookup: archive_path -> metadata
    file_map: dict[str, dict[str, Any]] = {}
    for fentry in files_meta:
        file_map[fentry["archive_path"]] = fentry

    # --- Back up current state for rollback ----------------------------
    db_path = data_dir / _DB_NAME
    backup_path = pathlib.Path(tempfile.mkdtemp(prefix="mynotes-rollback-", dir=data_dir.parent))
    backup_path.rmdir()
    staging = pathlib.Path(tempfile.mkdtemp(prefix="mynotes-restore-", dir=data_dir.parent))

    try:
        # Preserve current DB for rollback
        if db_path.exists():
            if backup_path.exists():
                shutil.rmtree(backup_path)
            shutil.copytree(data_dir, backup_path, symlinks=True)

        # --- Extract files to a staging area ---------------------------

        with zipfile.ZipFile(archive, "r") as zf:
            # Extract all entries except metadata into staging
            for info in zf.infolist():
                if info.filename == _META_NAME:
                    continue
                _reject_path(info.filename, staging)

                # Reject symlinks in archive
                # External attribute byte 10 (0o120000) at bits 16-31
                # on Unix, symlink = 0o120000 << 16
                if stat.S_ISLNK(info.external_attr >> 16):
                    raise ValueError(
                        f"Symlink in archive: {info.filename!r}"
                    )

                zf.extract(info, staging)

        # --- Restore database ------------------------------------------
        db_entry = file_map.get(_DB_NAME)
        if db_entry:
            staged_db = staging / _DB_NAME
            # Verify integrity via sqlite3.connection-level check
            with sqlite3.connect(str(staged_db)) as conn:
                try:
                    result = conn.execute("PRAGMA integrity_check").fetchone()
                except sqlite3.DatabaseError as exc:
                    raise ValueError(f"Database integrity check failed: {exc}")
                if result[0] != "ok":
                    raise ValueError(
                        f"Database integrity check failed: {result[0]}"
                    )
            # Replace the live database
            with sqlite3.connect(str(staged_db)) as source_conn, sqlite3.connect(str(db_path)) as live_conn:
                source_conn.backup(live_conn)

        # --- Restore managed files -------------------------------------
        files_restored: list[str] = []
        for archive_path in file_map:
            if archive_path == _DB_NAME:
                continue
            if _is_excluded(archive_path):
                continue
            staged_file = staging / archive_path
            if staged_file.is_file():
                dest = data_dir / archive_path
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(staged_file, dest)
                files_restored.append(archive_path)

        # --- Success: clean up -----------------------------------------
        shutil.rmtree(staging)
        if backup_path.exists():
            shutil.rmtree(backup_path)

        return {
            "status": "ok",
            "files_restored": files_restored,
            "database_restored": bool(db_entry),
            "timestamp": meta.get("timestamp"),
        }

    except Exception:
        # --- Rollback on any failure -----------------------------------
        logger.exception("Restore failed, rolling back")
        # Restore DB from backup
        if backup_path.exists():
            old_db = backup_path / _DB_NAME
            if old_db.exists():
                with sqlite3.connect(str(old_db)) as source_conn, sqlite3.connect(str(db_path)) as live_conn:
                    source_conn.backup(live_conn)
            for archive_path in file_map:
                if archive_path == _DB_NAME or _is_excluded(archive_path):
                    continue
                original = backup_path / archive_path
                target = data_dir / archive_path
                if original.is_file():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(original, target)
                elif target.is_file():
                    target.unlink()
        # Remove staging
        if staging.exists():
            shutil.rmtree(staging)
        # Remove partial backup dir
        if backup_path.exists():
            shutil.rmtree(backup_path)
        raise


# ---------------------------------------------------------------------------
# safe_extract
# ---------------------------------------------------------------------------

def safe_extract(archive: pathlib.Path, destination: pathlib.Path) -> None:
    """Safely extract *archive* into *destination*.

    Uses the same validation as ``restore()`` but writes to a
    caller-specified directory (used for Typst uploads, etc.).

    Parameters
    ----------
    archive:
        Path to the zip archive.
    destination:
        Directory to extract into.

    Raises
    ------
    ValueError
        If the archive fails validation or contains unsafe entries.
    """
    archive = pathlib.Path(archive).resolve()
    destination = pathlib.Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(archive, "r") as zf:
        if _META_NAME in zf.namelist():
            _validate_archive(archive)
        if len(zf.infolist()) > MAX_ENTRY_COUNT or sum(i.file_size for i in zf.infolist()) > MAX_ARCHIVE_SIZE:
            raise ValueError("Archive exceeds extraction limits")
        for info in zf.infolist():
            if info.filename == _META_NAME:
                continue

            _reject_path(info.filename, destination)

            # Reject symlinks
            if stat.S_ISLNK(info.external_attr >> 16):
                raise ValueError(
                    f"Symlink in archive: {info.filename!r}"
                )

            # Size check against actual zip info
            if info.file_size > MAX_ENTRY_SIZE:
                raise ValueError(
                    f"File {info.filename!r} exceeds max size "
                    f"({MAX_ENTRY_SIZE} bytes): {info.file_size}"
                )

            # Extreme compression ratio check
            if info.compress_size > 0:
                ratio = info.file_size / info.compress_size
                if ratio > 1000 and info.file_size > 10_000_000:
                    raise ValueError(
                        f"File {info.filename!r} has extreme compression "
                        f"ratio ({ratio:.1f}); possible zipbomb"
                    )

            zf.extract(info, destination)
