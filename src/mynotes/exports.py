"""MyNotes document exports — Typst compilation and Marp slide-deck generation."""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import pathlib
import re
import shlex
import shutil
import subprocess
from collections.abc import Sequence
from typing import Any

__all__ = ["compile_typst", "export_deck", "prepare_markdown"]

# --------------------------------------------------------------------------- #
# Typst compilation
# --------------------------------------------------------------------------- #


def compile_typst(
    source: pathlib.Path,
    destination: pathlib.Path,
    entrypoint: str | None = None,
) -> pathlib.Path:
    source = pathlib.Path(source).resolve()
    destination = pathlib.Path(destination).resolve()

    if source.is_file():
        if source.suffix != ".typ":
            raise ValueError(f"source must be a directory or .typ file: {source}")
        entrypoint = source.name
        source = source.parent
    if not source.is_dir():
        raise ValueError(f"source must be a directory or file: {source}")

    if entrypoint is None:
        entrypoint = "main.typ"

    _enforce_safe_entrypoint(entrypoint, source)
    entry_path = source / entrypoint
    if not entry_path.is_file():
        raise ValueError(f"entrypoint file not found: {entry_path}")

    typst_bin = shutil.which("typst")
    if typst_bin is None:
        raise FileNotFoundError(
            "The 'typst' CLI binary was not found on $PATH. "
            "Install Typst (https://typst.app) or provide a full path."
        )

    # Destination may be a .pdf file or a directory.
    if str(destination).endswith(".pdf"):
        destination.parent.mkdir(parents=True, exist_ok=True)
        out = destination
    else:
        destination.mkdir(parents=True, exist_ok=True)
        out = destination / "compiled.pdf"

    cmd = [
        typst_bin, "compile",
        "--root", str(source),
        "--format", "pdf",
        str(entry_path), str(out),
    ]

    try:
        result = subprocess.run(
            cmd, timeout=120, capture_output=True, text=True, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise subprocess.TimeoutExpired(
            cmd=exc.cmd, timeout=exc.timeout,
            output=exc.output,
            stderr="Typst compilation exceeded the 120 s time limit.",
        ) from exc

    if result.returncode != 0:
        msg = f"Typst compilation failed (exit {result.returncode})\n"
        msg += result.stderr.strip() if result.stderr else "(no stderr)"
        raise ValueError(msg)

    return out


# --------------------------------------------------------------------------- #
# Safe helpers
# --------------------------------------------------------------------------- #


def _enforce_safe_entrypoint(entrypoint: str, base: pathlib.Path) -> None:
    if pathlib.PurePath(entrypoint).is_absolute():
        raise ValueError(f"entrypoint must be relative: {entrypoint!r}")
    parts = pathlib.PurePath(entrypoint).parts
    if ".." in parts:
        raise ValueError(f"entrypoint contains '..': {entrypoint!r}")
    resolved = (base / entrypoint).resolve()
    if not resolved.is_relative_to(base.resolve()):
        raise ValueError(f"entrypoint escapes source directory: {entrypoint!r}")


# --------------------------------------------------------------------------- #
# Slide-deck export
# --------------------------------------------------------------------------- #


def export_deck(
    markdown: str,
    destination: pathlib.Path,
    assets: Sequence[dict[str, Any]] | None = None,
    citations: Sequence[dict[str, str]] | None = None,
) -> dict[str, str]:
    """Export a Marp Markdown deck to HTML, PDF, and a citation manifest."""
    dest = pathlib.Path(destination).resolve()
    dest.mkdir(parents=True, exist_ok=True)

    # 1. Copy assets first so image refs in markdown can be resolved.
    manifest_assets: list[dict[str, Any]] = []
    for asset in (assets or []):
        raw_path = asset["path"]
        if not pathlib.Path(raw_path).is_absolute():
            raise ValueError(f"asset path must be absolute: {raw_path!r}")
        src = pathlib.Path(raw_path).resolve()
        _enforce_local_file(src)
        if not src.is_file():
            raise FileNotFoundError(f"Asset file not found: {src}")
        _validate_svg_asset(src)
        _copy_and_embed(src, dest)
        manifest_assets.append({
            "path": str(src),
            "title": asset.get("title"),
            "locator": asset.get("locator"),
            "excerpt": asset.get("excerpt"),
            "local_path": f"../exports/{src.name}",
        })

    # 2. Now validate markdown (after assets are in place for ref check).
    _reject_unsafe_html(markdown)
    _validate_image_refs(markdown, dest)
    def embed_image(match):
        ref = match.group(2).strip()
        if ref.startswith("data:"):
            return match.group(0)
        if ":" in ref or ref.startswith("//"):
            raise ValueError("Remote or unsafe images are not supported in offline decks")
        file = dest / pathlib.PurePath(ref).name
        mime = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        return f"![{match.group(1)}](data:{mime};base64,{base64.b64encode(file.read_bytes()).decode()})"
    markdown = re.sub(r"!\[([^]]*)\]\(([^)]+)\)", embed_image, markdown)

    # 3. Append citation appendix so the deck embeds source traceability.
    citation_md = _citations_appendix(citations) if citations else None
    if citation_md:
        markdown += "\n\n---\n\n" + citation_md
        (dest / "citations.md").write_text(citation_md, encoding="utf-8")

    md_path = dest / "deck.md"
    md_path.write_text(markdown, encoding="utf-8")

    # 4. Separate Marp calls — HTML and PDF are mutually exclusive via -o.
    html_path = dest / "deck.html"
    pdf_path = dest / "deck.pdf"

    marp = _find_marp()
    result = subprocess.run(
        marp + [str(md_path), "--no-config", "--no-html", "-o", str(html_path)],
        timeout=120, capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise ValueError(f"Marp HTML export failed: {result.stderr.strip()}")

    pdf_cmd = marp + [str(md_path), "--no-config", "--no-html", "--pdf", "-o", str(pdf_path)]
    chromium = os.environ.get("MYNOTES_CHROMIUM")
    if chromium:
        pdf_cmd += ["--browser-path", chromium]

    result = subprocess.run(
        pdf_cmd, timeout=120, capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise ValueError(
            f"Marp PDF export failed (exit {result.returncode})\n"
            f"{result.stderr.strip() or '(no stderr)'}"
        )

    # 5. Verify files were produced.
    for p in (html_path, pdf_path):
        if not p.is_file():
            raise FileNotFoundError(
                f"Marp did not produce expected output: {p}"
            )

    # 6. Manifest.
    manifest_citations = (
        [{"title": c.get("title"), "source": c.get("source"),
           "excerpt": c.get("excerpt"), "version_id": c.get("version_id"),
           "source_id": c.get("source_id"), "locator": c.get("locator")}
         for c in citations]
        if citations else []
    )
    manifest_path = dest / "deck-manifest.json"
    manifest_path.write_text(
        json.dumps({
            "markdown_path": str(md_path),
            "html_path": str(html_path),
            "pdf_path": str(pdf_path),
            "assets": manifest_assets,
            "citations": manifest_citations,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return {
        "markdown_path": str(md_path),
        "html_path": str(html_path),
        "pdf_path": str(pdf_path),
        "manifest_path": str(manifest_path),
    }


# --------------------------------------------------------------------------- #
# Asset & HTML helpers
# --------------------------------------------------------------------------- #


def _reject_unsafe_html(content: str) -> None:
    for pattern in (
        r"(?i)<\s*script\b",
        r"(?i)javascript\s*:",
        r"(?i)\bon\w+\s*=",
        r"(?i)<\s*(object|embed|iframe)\b",
        r"(?i)<\s*(foreignObject|font-face|svg)\b[^>]*\b(action)\s*=",
        r"(?i)<\s*!?\s*DOCTYPE",
        r"(?i)@import\s",
        r"(?i)(?:background|background-image|style)\s*:\s*(?:url\()?\s*['\"]?(?:https?://)",
        r"(?i)\btheme\s*[=:]\s*['\"]?https?://",
        r"(?i)<\s*(img|a|link|area)\b[^>]*?\bsrc\s*=\s*['\"]?(?:https?://|//)",
        r"(?i)<\s*(img|a|link|area)\b[^>]*?\bhref\s*=\s*['\"]?(?:https?://|//)",
    ):
        if re.search(pattern, content):
            raise ValueError(
                "Deck content contains unsafe HTML or JavaScript and was rejected."
            )


def _validate_image_refs(markdown: str, dest: pathlib.Path) -> None:
    """Ensure local file-based image references exist on disk."""
    for match in re.finditer(r"!\[.*?\]\((.*?)\)", markdown):
        src = match.group(1)
        if not src.startswith(("http://", "https://", "data:", "mailto:")):
            candidate = dest / pathlib.PurePath(src).name
            if not candidate.is_file():
                raise FileNotFoundError(
                    f"Image reference not found on disk: {src!r}"
                )


def _validate_svg_asset(path: pathlib.Path) -> None:
    if path.suffix.lower() != ".svg":
        return
    content = path.read_text(encoding="utf-8")
    for bad in (r"<\s*script\b", r"javascript\s*:",
                 r"href\s*=\s*['\"]?https?://",
                 r"src\s*=\s*['\"]?https?://"):
        if re.search(bad, content, re.IGNORECASE):
            raise ValueError(f"SVG asset contains unsafe content: {path}")


def _copy_and_embed(src: pathlib.Path, dest: pathlib.Path) -> None:
    """Copy asset into dest so Marp can resolve local image refs."""
    dest_asset = dest / src.name
    dest_asset.write_bytes(src.read_bytes())


def _enforce_local_file(path: pathlib.Path) -> None:
    if not path.is_absolute():
        raise ValueError(f"asset path must be absolute: {path}")


# --------------------------------------------------------------------------- #
# Citations
# --------------------------------------------------------------------------- #


def _citations_appendix(citations: Sequence[dict[str, str]]) -> str:
    """Build a Markdown appendix with human-readable citation entries."""
    lines = ["## Citations and Source References", ""]
    for i, c in enumerate(citations, 1):
        lines.append(f"**{i}. {c.get('title', 'Untitled')}**")
        src = c.get("source")
        vid = c.get("version_id")
        excerpt = c.get("excerpt")
        if src:
            lines.append(f"Source: {src}")
        if vid:
            lines.append(f"Version ID: {vid}")
        if excerpt:
            lines.append(f"> {excerpt}")
        lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Marp discovery
# --------------------------------------------------------------------------- #


def _find_marp() -> list[str]:
    env_marp = os.environ.get("MYNOTES_MARP")
    if env_marp:
        if os.environ.get("MYNOTES_NODE"):
            return [os.environ["MYNOTES_NODE"], env_marp]
        return shlex.split(env_marp)

    # Resolve local node_modules/.bin/marp from project root (parents[2]).
    script_dir = pathlib.Path(__file__).resolve().parent
    root = script_dir.parent.parent
    local_marp = root / "node_modules" / ".bin" / "marp"
    if local_marp.is_file():
        return [str(local_marp)]

    return ["marp"]


# --------------------------------------------------------------------------- #
# prepare_markdown (placeholder)
# --------------------------------------------------------------------------- #


def prepare_markdown(markdown: str) -> str:
    """Prepare or normalise Marp Markdown for deck export."""
    return markdown
