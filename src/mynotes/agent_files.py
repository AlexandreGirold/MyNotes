"""Read-only file tools constrained by user-selected directories and real paths."""
import json
import os
import re
from pathlib import Path


def roots(directories: list[str] | None = None) -> list[Path]:
    return [Path(p).expanduser().resolve() for p in (directories if directories is not None else json.loads(os.environ.get("MYNOTES_AGENT_DIRECTORIES", "[]")))]


def allowed(path: str, directories: list[str] | None = None) -> Path:
    return _allowed_in(path, roots(directories))


def _allowed_in(path: str, selected: list[Path]) -> Path:
    """Shared guard with already-resolved roots; resolve each candidate, never cache it."""
    resolved = Path(path).expanduser().resolve()
    if not any(resolved.is_relative_to(root) for root in selected):
        raise ValueError("Path is outside the allowed directories")
    secret_names = {"credentials.enc", "encryption-key.enc", "auth.json", ".ssh", ".aws", ".gnupg", ".kube", ".azure", ".netrc", ".npmrc", ".pypirc", ".git-credentials", ".git"}
    if any(part.lower().startswith(".env") or part.lower() in secret_names for part in resolved.parts) or resolved.suffix.lower() in {".key", ".pem", ".p12", ".pfx"}:
        raise ValueError("Credential files are not accessible to the agent")
    return resolved


def list_files(directory: str = "", pattern: str = "*", *, directories: list[str] | None = None, terms: list[str] | None = None) -> dict:
    if ".." in Path(pattern).parts or Path(pattern).is_absolute():
        raise ValueError("Invalid file pattern")
    selected = [allowed(directory, directories)] if directory else roots(directories)
    files = []
    scanned = 0
    for root in selected:
        for path in root.rglob(pattern):
            scanned += 1
            # ponytail: cap traversal at 10000 entries; use narrower roots/patterns for very large trees.
            if scanned > 10000:
                return {"files": files, "truncated": True}
            if path.is_file():
                try:
                    allowed(str(path), directories)
                    if terms and not any(term in str(path).casefold() for term in terms):
                        continue
                    files.append(str(path))
                except ValueError:
                    continue
                if len(files) >= 200:
                    return {"files": files, "truncated": True}
    return {"directories": [str(root) for root in selected], "files": files, "truncated": False}


def read_file(path: str, offset: int = 1, limit: int = 200, *, directories: list[str] | None = None) -> dict:
    file = allowed(path, directories)
    if not file.is_file() or file.stat().st_size > 20 * 1024 * 1024:
        raise ValueError("File is missing or exceeds the 20 MB tool limit")
    if file.suffix.lower() == ".pdf":
        import pypdfium2 as pdfium
        with pdfium.PdfDocument(file) as pdf:
            text = "\n".join(f"Page {i + 1}: {page.get_textpage().get_text_range()}" for i, page in enumerate(pdf))
        lines = text.splitlines()
    else:
        lines = file.read_text(encoding="utf-8").splitlines()
    start = max(0, offset - 1)
    selected = lines[start:start + min(max(limit, 1), 500)]
    return {"path": str(file), "line_start": start + 1, "text": "\n".join(selected)[:30000],
            "truncated": start + len(selected) < len(lines) or len("\n".join(selected)) > 30000}


def search_files(query: str, directory: str = "", *, directories: list[str] | None = None) -> dict:
    matches = []
    inventory = list_files(directory, directories=directories)
    truncated = inventory["truncated"]
    for filename in inventory["files"]:
        try:
            result = read_file(filename, 1, 500, directories=directories)
        except (ValueError, UnicodeError, OSError):
            continue
        truncated |= result["truncated"]
        for i, line in enumerate(result["text"].splitlines(), 1):
            if query.casefold() in line.casefold():
                matches.append({"path": filename, "line": i, "text": line[:500]})
                if len(matches) >= 50:
                    return {"matches": matches, "truncated": True}
    return {"matches": matches, "truncated": truncated}


def research_files(question: str, directories: list[str]) -> dict:
    """Provide actual allowed-file evidence without requiring model tool calls."""
    if not directories:
        raise ValueError("Add allowed folders in Settings before searching laptop files.")
    ignored = {"the", "and", "for", "can", "you", "look", "find", "read", "files", "file", "relevant", "about", "numbers", "les", "des", "dans", "pour", "avec", "que", "peux", "veux", "fichiers", "fichier", "pertinents", "cherche", "chercher", "regarder", "chiffres", "donne", "quoi", "sur", "est"}
    terms = [term for term in dict.fromkeys(re.findall(r"[\w-]{3,}", question.casefold())) if term not in ignored][:8]
    if not terms:
        raise ValueError("Give a topic, project name or filename to search for.")
    terms.sort(key=len, reverse=True)
    inventory = list_files(directories=directories, terms=terms)
    files = inventory["files"]
    warnings = ["Filename search was truncated; narrow your folders or topic."] if inventory["truncated"] else []
    if not files:
        found = search_files(terms[0], directories=directories)
        files = list(dict.fromkeys(match["path"] for match in found["matches"]))
        if found["truncated"]:
            warnings.append("Content search covered bounded file/line excerpts, not the entire folder tree.")
    files.sort(key=lambda path: sum(term in Path(path).name.casefold() for term in terms), reverse=True)
    blocks = []
    # ponytail: read up to five matching files / 60000 total characters; full exploration uses agent tools.
    for path in files[:5]:
        try:
            evidence = read_file(path, limit=500, directories=directories)
        except (ValueError, UnicodeError, OSError) as exc:
            warnings.append(f"Cannot read {Path(path).name}: {exc}")
            continue
        blocks.append({"source_id": path, "title": Path(path).name, "text": evidence["text"][:12000], "origin": "allowed laptop file", "locator": {"path": path, "line_start": evidence["line_start"]}})
        if evidence["truncated"] or len(evidence["text"]) > 12000:
            warnings.append(f"Read a bounded excerpt of {Path(path).name}.")
    if len(files) > 5:
        warnings.append("Read the five highest filename matches; narrow the topic for other files.")
    if not blocks:
        raise ValueError("No readable matching files found in allowed folders. Try the project name or a filename. " + " ".join(warnings))
    return {"mode": "files", "metadata": {"title": "Allowed laptop files"}, "blocks": blocks, "warnings": warnings}
