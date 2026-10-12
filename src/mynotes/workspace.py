"""Browse and atomically edit canonical laptop files in the selected directories."""
import hashlib
import os
import tempfile
from pathlib import Path

from mynotes.agent_files import allowed

MAX_TEXT = 2 * 1024 * 1024
_TEXT_EXTENSIONS = frozenset([".md", ".markdown", ".txt", ".text", ".typ", ".csv", ".log", ".rst", ".org", ".adoc", ".py", ".js", ".ts", ".tsx", ".jsx", ".css", ".scss", ".html", ".java", ".c", ".cpp", ".h", ".hpp", ".cs", ".go", ".rs", ".rb", ".php", ".swift", ".kt", ".scala", ".clj", ".hs", ".m", ".mm", ".r", ".jl", ".dart", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".xml", ".properties", ".sh", ".bash", ".zsh", ".fish", ".sql", ".graphql", ".proto", ".tf", ".note", ".sticky"])


class WorkspaceConflict(ValueError):
    """The source changed externally; do not overwrite it."""


def _allowed(path, directories=None):
    return allowed(path, directories)


def _is_text_file(path):
    return path.suffix.lower() in _TEXT_EXTENSIONS


def list_directory(path, directories, offset=0, limit=200):
    if offset < 0 or not 1 <= limit <= 200:
        raise ValueError("Invalid directory page")
    entries = []
    paths = (Path(p) for p in directories) if not path else _allowed(path, directories).iterdir()
    for child in paths:
        try:
            resolved = _allowed(str(child), directories)
            if not (resolved.is_dir() or resolved.is_file()):
                continue
            entries.append({"name": child.name, "path": str(resolved), "directory": resolved.is_dir(),
                            "size": resolved.stat().st_size,
                            "editable": resolved.is_file() and _is_text_file(resolved)})
        except (OSError, ValueError):
            continue
    entries.sort(key=lambda item: (not item["directory"], item["name"].casefold(), item["name"]))
    end = offset + limit
    return {"path": str(_allowed(path, directories)) if path else "", "entries": entries[offset:end],
            "next_offset": end if end < len(entries) else None}


def read_text(path, directories):
    file = _allowed(path, directories)
    if not _is_text_file(file):
        raise ValueError("File extension is not a recognised text type")
    if not file.is_file():
        raise FileNotFoundError(path)
    if file.stat().st_size > MAX_TEXT:
        raise ValueError("File exceeds 2 MB size limit")
    with file.open("rb") as source:
        raw = source.read(MAX_TEXT + 1)
    if len(raw) > MAX_TEXT or b"\0" in raw:
        raise ValueError("File is binary or exceeds 2 MB")
    return {"path": str(file), "text": raw.decode("utf-8"), "sha256": hashlib.sha256(raw).hexdigest()}


def atomic_write(path, text, directories, expected_sha256=None):
    file = _allowed(path, directories)
    parent = _allowed(str(file.parent), directories)
    if not parent.is_dir():
        raise ValueError("Choose an existing allowed folder")
    if not _is_text_file(file):
        raise ValueError("File extension is not a recognised text type")
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_TEXT or b"\0" in encoded:
        raise ValueError("Content is binary or exceeds 2 MB size limit")
    def check():
        if _allowed(path, directories) != file:
            raise WorkspaceConflict("File path changed externally")
        if file.exists():
            if not file.is_file() or file.stat().st_size > MAX_TEXT or expected_sha256 is None:
                raise WorkspaceConflict("File already exists; reopen it before saving")
            if hashlib.sha256(file.read_bytes()).hexdigest() != expected_sha256.lower():
                raise WorkspaceConflict("File changed externally; draft retained")
        elif expected_sha256 is not None:
            raise WorkspaceConflict("File was removed externally; draft retained")
    check()
    fd, temporary = tempfile.mkstemp(dir=parent, prefix=f".mynotes-{file.name}.")
    temp = Path(temporary)
    try:
        os.fchmod(fd, (file.stat().st_mode & 0o777) if file.exists() else 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        check()
        if expected_sha256 is None:
            try:
                os.link(temp, file)  # Atomic create without clobbering a concurrently created file.
            except FileExistsError as error:
                raise WorkspaceConflict("File already exists; choose another filename") from error
        else:
            temp.replace(file)
        directory_fd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temp.unlink(missing_ok=True)
    return {"path": str(file), "sha256": hashlib.sha256(encoded).hexdigest()}
