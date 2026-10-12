"""MyNotes document importers — extract text from PDF, Markdown, HTML, Typst.

All imports write immutable originals into ``destination / originals`` and return
a structured result that the background worker stores into the FTS5 index.

Public API
----------
process_import(source, destination, kind, entrypoint=None) -> dict
render_page(pdf, page, destination) -> Path
"""

from __future__ import annotations

import logging
import pathlib
import shutil
import subprocess

from mynotes.exports import compile_typst

__all__ = ["process_import", "render_page"]

logger = logging.getLogger(__name__)

# --- helpers ---------------------------------------------------------------


def _safe_subprocess(
    cmd: list[str],
    *,
    timeout: float = 60.0,
    cwd: pathlib.Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run *cmd* as a bounded, non-interactive subprocess.

    * ``shell=False`` — no shell injection, absolute paths only.
    * *timeout* — hard wall (seconds).
    * *cwd* — restricted working directory; ``None`` for ``/``.
    """
    if not cmd:
        raise ValueError("cmd must not be empty")
    return subprocess.run(
        cmd,
        timeout=timeout,
        capture_output=True,
        text=True,
        cwd=cwd,
        check=False,
    )


def _save_original(source: pathlib.Path, destination: pathlib.Path) -> pathlib.Path:
    """Persist *source* under ``destination/originals`` preserving its name."""
    originals = destination / "originals"
    originals.mkdir(parents=True, exist_ok=True)
    dest = originals / source.name

    if source.is_file():
        dest.write_bytes(source.read_bytes())
    elif source.is_dir():
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(source, dest, symlinks=False)
    else:
        raise ValueError(f"source is not a regular file or directory: {source}")

    return dest


def _low_text_ratio(text: str) -> float:
    """Return a rough measure of text density (whitespace ratio).

    A pure-scanned PDF page will have a large image ratio; we only get text
    from the text layer so pages with almost no text are likely scanned.
    """
    if not text:
        return 1.0
    stripped = text.strip()
    if not stripped:
        return 1.0
    return sum(1 for c in stripped if c.isspace()) / len(stripped)


# --- render_page ------------------------------------------------------------


def render_page(pdf: pathlib.Path, page: int, destination: pathlib.Path) -> pathlib.Path:
    """Render *page* (1-based) of *pdf* as a PNG into *destination*.

    Returns the absolute path to the PNG file.
    Raises ``ValueError`` when the page is out of range.
    """
    pdf = pathlib.Path(pdf).resolve()
    if not pdf.is_file():
        raise FileNotFoundError(f"PDF not found: {pdf}")
    if page < 1:
        raise ValueError(f"page must be >= 1, got {page}")

    destination = pathlib.Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)

    import pypdfium2 as pdfium

    with pdfium.PdfDocument(str(pdf)) as doc:
        if page > len(doc):
            raise ValueError(
                f"page {page} out of range (document has {len(doc)} pages)"
            )

        page_obj = doc[page - 1]
        bitmap = page_obj.render(scale=2)  # 2× for OCR readability
        png_path = destination / f"page_{page}.png"
        bitmap.to_pil().save(str(png_path))
        bitmap.close()
    return png_path


# --- PDF import -------------------------------------------------------------

_TEXT_CHUNK_MAX = 3000


def _process_pdf(source: pathlib.Path, destination: pathlib.Path) -> dict:
    """Extract text blocks from a PDF. Low-text pages are rendered and OCR'd."""
    import pypdfium2 as pdfium

    title = source.stem
    blocks: list[dict] = []
    assets: list[dict] = []
    warnings: list[str] = []

    with pdfium.PdfDocument(str(source)) as doc:
        for idx, page_obj in enumerate(doc):
            page_num = idx + 1
            raw_text = page_obj.get_textpage().get_text_range()
            stripped_text = raw_text.strip()
            ratio = _low_text_ratio(stripped_text)

            if ratio > 0.95 and len(stripped_text) < 20:
                png_path = render_page(source, page_num, destination)
                assets.append({"path": str(png_path), "title": f"page {page_num}"})
                # Low-text page → render + OCR
                tess = shutil.which("tesseract")
                if tess is None:
                    warnings.append(
                        f"page {page_num}: low text, tesseract not found — skipping OCR"
                    )
                    continue

                try:
                    result = _safe_subprocess(
                        [tess, str(png_path), "stdout", "-l", "eng"],
                        timeout=60,
                    )
                    if result.returncode != 0:
                        warnings.append(f"page {page_num}: OCR failed — {result.stderr.strip()}")
                        continue
                    ocr_text = result.stdout.strip()[:100000]
                    if ocr_text:
                        blocks.append(
                            {
                                "text": ocr_text,
                                "locator": {"page": page_num, "type": "ocr"},
                                "origin": "ocr",
                            }
                        )
                except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
                    warnings.append(f"page {page_num}: OCR failed — {exc}")
            else:
                for chunk in _chunk_text(stripped_text, _TEXT_CHUNK_MAX):
                    text_start = stripped_text.index(chunk) if chunk in stripped_text else 0
                    text_end = text_start + len(chunk)
                    blocks.append(
                        {
                            "text": chunk,
                            "locator": {
                                "page": page_num,
                                "text_start": text_start,
                                "text_end": text_end,
                            },
                            "origin": "extracted",
                        }
                    )
    return {"title": title, "blocks": blocks, "assets": assets, "warnings": warnings}


def _chunk_text(text: str, max_len: int) -> list[str]:
    """Split *text* into ~*max_len*-character chunks on word boundaries."""
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + max_len, len(text))
        if end < len(text):
            # Break on last space before limit
            space = text.rfind(" ", start, end)
            if space > start:
                end = space
        chunks.append(text[start:end].strip())
        start = end
    return [c for c in chunks if c]


# --- Markdown import --------------------------------------------------------


def _process_markdown(source: pathlib.Path, destination: pathlib.Path) -> dict:
    """Return blocks with line-range locators for the source file."""
    content = source.read_text(encoding="utf-8")
    title = source.stem
    lines = content.splitlines()

    blocks: list[dict] = []
    for i, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped:
            continue
        blocks.append(
            {
                "text": stripped,
                "locator": {
                    "file": source.name,
                    "line": i,
                    "text_start": 0,
                    "text_end": len(stripped),
                },
                "origin": "extracted",
            }
        )
    return {"title": title, "blocks": blocks, "assets": [], "warnings": []}


# --- HTML import ------------------------------------------------------------


def _process_html(source: pathlib.Path, destination: pathlib.Path) -> dict:
    """Strip scripts, styles, and tags — return plain-text blocks."""
    from bs4 import BeautifulSoup

    html = source.read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "html.parser")

    # Remove dangerous / non-content tags
    for tag in soup(["script", "style", "noscript", "iframe", "svg", "form"]):
        tag.decompose()

    title = soup.title.string.strip() if soup.title and soup.title.string else source.stem
    raw_text = soup.get_text(separator="\n", strip=True)

    blocks: list[dict] = []
    for i, line in enumerate(raw_text.splitlines(), start=1):
        if line.strip():
            blocks.append(
                {
                    "text": line.strip(),
                    "locator": {"file": source.name, "line": i, "text_start": 0, "text_end": len(line)},
                    "origin": "extracted",
                }
            )
    return {"title": title, "blocks": blocks, "assets": [], "warnings": []}


# --- Typst import -----------------------------------------------------------


def _process_typst(
    source: pathlib.Path,
    destination: pathlib.Path,
    entrypoint: str | None,
) -> dict:
    """Compile Typst → PDF, then extract via the PDF pipeline."""
    entrypoint = entrypoint or "main.typ"
    pdf_path = destination / f"{source.stem}.pdf"
    try:
        compile_typst(source, pdf_path, entrypoint=entrypoint)
    except FileNotFoundError as exc:
        return {
            "title": source.stem,
            "blocks": [],
            "assets": [],
            "warnings": [str(exc)],
        }
    except ValueError as exc:
        return {
            "title": source.stem,
            "blocks": [],
            "assets": [],
            "warnings": [f"Typst compilation failed: {exc}"],
        }

    assets = [{"path": str(pdf_path), "title": "compiled PDF"}]
    pdf_result = _process_pdf(pdf_path, destination)
    pdf_result["assets"] = assets  # use compiled PDF, not originals
    return pdf_result


# --- Folder (multi-file) import ---------------------------------------------


def _process_folder(
    source: pathlib.Path,
    destination: pathlib.Path,
    entrypoint: str | None,
) -> dict:
    """Import a single file selected by *entrypoint* (or first file by glob)."""
    source = source.resolve()
    if entrypoint:
        # Validate entrypoint is safe (no path traversal)
        ep_path = source / entrypoint
        resolved = ep_path.resolve()
        if not resolved.is_relative_to(source):
            return {
                "title": source.name,
                "blocks": [],
                "assets": [],
                "warnings": [f"entrypoint escapes source directory: {entrypoint!r}"],
            }
        if not resolved.is_file():
            return {
                "title": source.name,
                "blocks": [],
                "assets": [],
                "warnings": [f"entrypoint not found: {entrypoint!r}"],
            }
        target = resolved
    else:
        candidates = sorted(source.iterdir())
        if not candidates:
            return {
                "title": source.name,
                "blocks": [],
                "assets": [],
                "warnings": ["folder is empty"],
            }
        # Skip directories — pick first file
        target = None
        for c in candidates:
            if c.is_file():
                target = c
                break
        if target is None:
            return {
                "title": source.name,
                "blocks": [],
                "assets": [],
                "warnings": ["folder contains no regular files"],
            }

    kind = _detect_kind(target)
    dispatch = {
        "pdf": _process_pdf,
        "markdown": _process_markdown,
        "html": _process_html,
        "typst": _process_typst,
    }
    handler = dispatch.get(kind)
    if handler is None:
        return {
            "title": source.name,
            "blocks": [],
            "assets": [],
            "warnings": [f"unknown file type: {target.suffix}"],
        }
    if kind == "typst":
        return handler(source, destination, target.relative_to(source).as_posix())
    return handler(target, destination)


# --- Dispatch ---------------------------------------------------------------

_KIND_MAP: dict[str, str] = {
    ".pdf": "pdf",
    ".md": "markdown",
    ".markdown": "markdown",
    ".html": "html",
    ".htm": "html",
    ".typ": "typst",
    ".typst": "typst",
}


def _detect_kind(path: pathlib.Path) -> str:
    """Map a file extension to a known import kind."""
    return _KIND_MAP.get(path.suffix.lower(), "unknown")


# --- Public API -------------------------------------------------------------


def process_import(
    source: pathlib.Path,
    destination: pathlib.Path,
    kind: str,
    entrypoint: str | None = None,
) -> dict:
    """Process a document import and return structured extracted content.

    Parameters
    ----------
    source : Path
        Path to the file or folder to import.  Preserved unchanged under
        ``destination/originals``.
    destination : Path
        Directory where processed artifacts are written.
    kind : str
        One of ``"pdf"``, ``"markdown"``, ``"html"``, ``"typst"``,
        ``"folder"``.
    entrypoint : str, optional
        For ``"typst"`` or ``"folder"`` imports: the entry-point file
        name (e.g. ``"main.typ"``).

    Returns
    -------
    dict
        ``title`` · ``blocks`` · ``assets`` · ``warnings`` · ``original_path``

    Raises
    ------
    ValueError
        When *source* does not exist or *kind* is not supported.
    """
    source = pathlib.Path(source).resolve()
    destination = pathlib.Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)

    if kind not in {"pdf", "markdown", "html", "typst", "folder", "pptx"}:
        raise ValueError(f"unsupported kind: {kind!r}")
    if kind == "folder" or (kind == "typst" and source.is_dir()):
        if not source.is_dir():
            raise ValueError(f"source must be a directory for kind 'folder': {source}")
    else:
        if not source.is_file():
            raise ValueError(f"source must be a regular file: {source}")

    original_path = _save_original(source, destination)
    if kind == "pptx":
        from mynotes.pptx_importer import process_pptx
        return {**process_pptx(original_path, destination), "original_path": str(original_path)}

    handlers: dict[str, callable] = {
        "pdf": lambda: _process_pdf(source, destination),
        "markdown": lambda: _process_markdown(source, destination),
        "html": lambda: _process_html(source, destination),
        "typst": lambda: _process_typst(source, destination, entrypoint),
        "folder": lambda: _process_folder(source, destination, entrypoint),
    }

    handler = handlers.get(kind)
    if handler is None:
        # Try to auto-detect from file extension
        if source.is_file():
            detected = _detect_kind(source)
            handler = handlers.get(detected)
            if handler is None:
                raise ValueError(f"unsupported kind: {kind!r}")
            return {**handler(), "original_path": str(original_path)}
        raise ValueError(f"unsupported kind: {kind!r}")

    result = handler()
    result["original_path"] = str(original_path)
    return result
