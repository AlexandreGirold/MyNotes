"""Extract text, images, and tables from a PPTX file.

Traverse slides, shapes, grouped shapes, and tables.  Save embedded
pictures into *destination* and return a structured summary with
provenance.  Text is extracted via ``python-pptx``; a separate Tesseract
OCR step is out of scope for now (marked ``origin="ocr"`` but never
populated until added).

Limitations
-----------
* SmartArt is treated as an ordinary grouped shape; no special handling.
* Charts, video, audio, and audio-video clips are not extracted.
* Table formatting (merged cells, styles) is lost — plain text only.
* A complete visual rendering of a slide requires the original author's
  PDF export or explicit external headless LibreOffice conversion.
"""

from __future__ import annotations

import pathlib
import uuid
from typing import Any

from lxml import etree
from pptx import Presentation

__all__: list[str] = ["process_pptx"]

NS_DRAWING = "http://schemas.openxmlformats.org/drawingml/2006/main"
NS_PRESENTATION = "http://schemas.openxmlformats.org/presentationml/2006/main"


# ── public API ──────────────────────────────────────────────────────────


def process_pptx(source: pathlib.Path, destination: pathlib.Path) -> dict[str, Any]:
    """Process a PPTX file and extract text blocks, images, and warnings.

    Parameters
    ----------
    source:
        Path to the PowerPoint file (``.pptx``).
    destination:
        Directory where extracted pictures are saved.  Created if missing.

    Returns
    -------
    dict
        ``{"title": str, "blocks": list[dict], "assets": list[dict],
        "warnings": list[str]}``

    Raises
    ------
    FileNotFoundError
        If *source* does not exist or is not a readable PPTX file.
    ValueError
        If the file is not a valid Office Open XML presentation.
    """
    source = pathlib.Path(source).resolve()
    destination = pathlib.Path(destination).resolve()

    if not source.is_file():
        raise FileNotFoundError(f"PPTX file not found: {source}")

    destination.mkdir(parents=True, exist_ok=True)

    prs = Presentation(str(source))
    blocks: list[dict[str, Any]] = []
    assets: list[dict[str, Any]] = []
    warnings: list[str] = []

    # Title: layout name of first slide, or first visible text, or generic
    title = "Untitled Presentation"
    if prs.slides:
        first = prs.slides[0]
        if first.shapes:
            for s in first.shapes:
                try:
                    txt = s.text_frame.text
                    if txt and txt.strip():
                        title = txt.strip()
                        break
                except (AttributeError, IndexError):
                    pass
        else:
            title = first.name or title

    _traverse_shapes(
        prs.slides,
        destination,
        blocks,
        assets,
        warnings,
        group_path="",
    )

    return {
        "title": title,
        "blocks": blocks,
        "assets": assets,
        "warnings": warnings,
    }


# ── internal helpers ────────────────────────────────────────────────────


def _has_grpsp_pr(el: etree._Element) -> bool:
    """Return True if *el* is a group shape (contains grpSpPr or is agrpSp)."""
    return (
        el.find(f".//{{{NS_PRESENTATION}}}grpSpPr") is not None
        or el.tag.endswith("grpSp")
    )


def _traverse_shapes(
    slides: Any,
    destination: pathlib.Path,
    blocks: list[dict[str, Any]],
    assets: list[dict[str, Any]],
    warnings: list[str],
    group_path: str,
) -> None:
    """Walk every slide and its shapes."""

    for slide_idx, slide in enumerate(slides, start=1):
        _walk_shapes(
            slide.shapes,
            slide_idx,
            destination,
            blocks,
            assets,
            warnings,
            "",
        )


def _walk_shapes(
    shape_collection: Any,
    slide_idx: int,
    destination: pathlib.Path,
    blocks: list[dict[str, Any]],
    assets: list[dict[str, Any]],
    warnings: list[str],
    group_path: str,
) -> None:
    """Recursively walk shapes (and nested group children)."""
    for shape in shape_collection:
        el = shape._element
        shape_name = shape.name or "shape"

        # ── group shape ──
        if _has_grpsp_pr(el):
            child_path = f"{group_path}.{shape_name}" if group_path else shape_name
            # group-level text (rare but possible)
            _extract_shape_text(shape, slide_idx, shape_name, blocks)
            # recurse into children
            if hasattr(shape, "shapes"):
                _walk_shapes(
                    shape.shapes,
                    slide_idx,
                    destination,
                    blocks,
                    assets,
                    warnings,
                    child_path,
                )
            continue

        # ── picture ──
        _maybe_extract_picture(shape, slide_idx, shape_name, destination, assets)

        # ── text ──
        _extract_shape_text(shape, slide_idx, shape_name, blocks)

        # ── table ──
        _maybe_extract_table(shape, slide_idx, shape_name, blocks)


def _extract_shape_text(
    shape: Any,
    slide_idx: int,
    shape_name: str,
    blocks: list[dict[str, Any]],
) -> None:
    """Extract text from a shape's text_frame (if present and non-empty)."""
    try:
        text = shape.text_frame.text
    except (AttributeError, IndexError):
        return

    if text.strip():
        blocks.append({
            "text": text,
            "locator": {
                "slide": slide_idx,
                "shape": shape_name,
            },
            "origin": "extracted",
        })


def _maybe_extract_picture(
    shape: Any,
    slide_idx: int,
    shape_name: str,
    destination: pathlib.Path,
    assets: list[dict[str, Any]],
) -> None:
    """Save an embedded picture and record its asset info."""
    try:
        image = shape.image
    except AttributeError:
        return
    fmt = image.ext or "bin"
    blob = image.blob
    if not blob:
        return

    asset_id = str(uuid.uuid4())[:8]
    filename = f"slide{slide_idx}_{asset_id}.{fmt}"
    target = destination / filename
    target.write_bytes(blob)

    locator_shape = shape_name if shape_name and shape_name != "shape" else f"pic_{asset_id}"
    assets.append({
        "path": str(target),
        "slide": slide_idx,
        "shape": locator_shape,
        "x": shape.left or 0,
        "y": shape.top or 0,
        "width": shape.width or 0,
        "height": shape.height or 0,
        "kind": "image",
    })


def _maybe_extract_table(
    shape: Any,
    slide_idx: int,
    shape_name: str,
    blocks: list[dict[str, Any]],
) -> None:
    """Extract table cells as text blocks if present."""
    try:
        tbl = shape.table
    except (AttributeError, IndexError, ValueError):
        return
    if tbl is None:
        return

    cell_texts: list[str] = []
    for row in tbl.rows:
        row_cells: list[str] = []
        for cell in row.cells:
            row_cells.append(cell.text)
        cell_texts.append(" | ".join(row_cells))
    text = "\n".join(cell_texts)
    if text.strip():
        blocks.append({
            "text": text,
            "locator": {
                "slide": slide_idx,
                "shape": shape_name,
            },
            "origin": "extracted",
        })
