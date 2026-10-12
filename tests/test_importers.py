"""Tests for src/mynotes/importers.py"""

from __future__ import annotations

import io
import pathlib
import subprocess
import zlib
from subprocess import CompletedProcess
from unittest import mock

import pytest

from mynotes.importers import (
    _chunk_text,
    _detect_kind,
    _low_text_ratio,
    _save_original,
    process_import,
    render_page,
)


# =============================================================================
# PDF fixture generator — real minimal valid PDF with correct object numbering
# =============================================================================


def _create_pdf_simple(
    tmp_path: pathlib.Path,
    name: str,
    pages: list[list[str]],
) -> pathlib.Path:
    """Create a minimal PDF with embedded text using only stdlib + zlib.

    Generates correct sequential object numbers, correct /Kids list in the
    Pages object, and a proper xref table built by scanning the final buffer.
    pypdfium2 can read the result.
    """
    buf = io.BytesIO()
    buf.write(b"%PDF-1.4\n")

    # Pre-compute sequential object numbers:
    #   Catalog = 1, Pages = 2
    #   Per page: content=N, page=N+1, font=N+2  (N = 3, 6, 9, ...)
    #   Page object numbers: 4, 7, 10, ...
    base = 3
    page_obj_nums = [base + i * 3 + 1 for i in range(len(pages))]
    kids_str = " ".join(f"{n} 0 R" for n in page_obj_nums)

    obj_counter = 1

    def _write_obj(content) -> int:
        nonlocal obj_counter
        num = obj_counter
        obj_counter += 1
        buf.write(f"{num} 0 obj\n".encode())
        buf.write(content if isinstance(content, bytes) else content.encode())
        buf.write(b"\nendobj\n")
        return num

    # Object 1: Catalog
    _write_obj(b"<< /Type /Catalog /Pages 2 0 R >>")

    # Object 2: Pages — already has the correct kids from pre-computation
    _write_obj(f"<< /Type /Pages /Kids [{kids_str}] /Count {len(pages)} >>".encode())

    # Page-level objects
    for i, page_texts in enumerate(pages):
        content_num = base + i * 3
        font_num = base + i * 3 + 2

        # Content stream
        ops: list[str] = []
        for j, text in enumerate(page_texts):
            escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            y = 700 - j * 20
            ops.append(f"BT /F1 12 Tf 1 0 0 1 50 {y} Tm ({escaped}) Tj ET")
        raw = b"\n".join(op.encode("latin-1") for op in ops)
        compressed = zlib.compress(raw)

        _write_obj(
            b"<< /Length "
            + str(len(compressed)).encode()
            + b" /Filter /FlateDecode"
            + b" >>\nstream\n"
            + compressed
            + b"\nendstream"
        )

        # Page object
        _write_obj(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Contents "
            + str(content_num).encode()
            + b" 0 R "
            b"/Resources << /Font << /F1 "
            + str(font_num).encode()
            + b" 0 R >> >> >>"
        )

        # Font object
        _write_obj(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    # --- xref table ---
    data = buf.getvalue()
    xref_start = len(data)
    buf.write(b"xref\n")

    total_objects = obj_counter
    buf.write(f"0 {total_objects}\n".encode())
    buf.write(b"0000000000 65535 f \n")

    for i in range(1, total_objects):
        header = f"{i} 0 obj\n".encode()
        pos = data.find(header)
        if pos >= 0:
            buf.write(f"{pos:010d} 00000 n \n".encode())
        else:
            buf.write(b"0000000000 00000 n \n")

    buf.write(b"trailer\n<< /Size " + str(total_objects).encode() + b" /Root 1 0 R >>\n")
    buf.write(b"startxref\n")
    buf.write(str(xref_start).encode() + b"\n")
    buf.write(b"%%EOF\n")

    pdf_path = tmp_path / name
    pdf_path.write_bytes(buf.getvalue())
    return pdf_path


# =============================================================================
# _save_original
# =============================================================================


class Test_save_original:
    def test_preserves_content(self, tmp_path: pathlib.Path) -> None:
        src = tmp_path / "doc.pdf"
        src.write_bytes(b"original content")
        dst = _save_original(src, tmp_path / "dest")
        assert dst == tmp_path / "dest" / "originals" / "doc.pdf"
        assert dst.read_bytes() == b"original content"

    def test_dest_dir_created(self, tmp_path: pathlib.Path) -> None:
        src = tmp_path / "a.txt"
        src.write_text("hi")
        dest_dir = tmp_path / "new" / "deep"
        dst = _save_original(src, dest_dir)
        assert dst.exists()


# =============================================================================
# _low_text_ratio
# =============================================================================


class Test_low_text_ratio:
    def test_empty(self) -> None:
        assert _low_text_ratio("") == 1.0

    def test_all_spaces(self) -> None:
        assert _low_text_ratio("   ") == 1.0

    def test_all_text(self) -> None:
        assert _low_text_ratio("hello") == 0.0

    def test_mixed(self) -> None:
        r = _low_text_ratio("hello world")
        assert 0 < r < 1


# =============================================================================
# _chunk_text
# =============================================================================


class Test_chunk_text:
    def test_short(self) -> None:
        assert _chunk_text("hello", 100) == ["hello"]

    def test_split_at_word_boundary(self) -> None:
        text = "one two three four five six seven eight nine ten"
        chunks = _chunk_text(text, 20)
        for c in chunks:
            assert len(c) <= 20
        assert len(chunks) >= 2
        assert " " in "".join(chunks)

    def test_empty(self) -> None:
        assert _chunk_text("", 100) == []

    def test_only_spaces(self) -> None:
        assert _chunk_text("   ", 100) == []


# =============================================================================
# render_page
# =============================================================================


class Test_render_page:
    def test_page_out_of_range(self, tmp_path: pathlib.Path) -> None:
        pdf = _create_pdf_simple(tmp_path, "doc.pdf", [["Hello"]])
        with pytest.raises(ValueError, match="page 5 out of range"):
            render_page(pdf, 5, tmp_path / "out")

    def test_page_zero(self, tmp_path: pathlib.Path) -> None:
        pdf = _create_pdf_simple(tmp_path, "doc.pdf", [["Hello"]])
        with pytest.raises(ValueError, match="page must be >= 1"):
            render_page(pdf, 0, tmp_path / "out")

    def test_pdf_not_found(self, tmp_path: pathlib.Path) -> None:
        with pytest.raises(FileNotFoundError, match="PDF not found"):
            render_page(tmp_path / "nope.pdf", 1, tmp_path / "out")

    def test_render_creates_png(self, tmp_path: pathlib.Path) -> None:
        pdf = _create_pdf_simple(tmp_path, "doc.pdf", [["Test page"]])
        png = render_page(pdf, 1, tmp_path / "out")
        assert png.exists()
        assert png.suffix == ".png"
        assert png.name == "page_1.png"


# =============================================================================
# _detect_kind
# =============================================================================


class Test_detect_kind:
    @pytest.mark.parametrize(
        "name, expected",
        [
            ("a.pdf", "pdf"),
            ("b.md", "markdown"),
            ("c.markdown", "markdown"),
            ("d.html", "html"),
            ("e.htm", "html"),
            ("f.typ", "typst"),
            ("typst.typst", "typst"),
            ("x.xyz", "unknown"),
        ],
    )
    def test_extension_map(self, name: str, expected: str) -> None:
        assert _detect_kind(pathlib.Path(name)) == expected


# =============================================================================
# process_import — Markdown
# =============================================================================


class Test_process_import_markdown:
    def test_basic(self, tmp_path: pathlib.Path) -> None:
        src = tmp_path / "readme.md"
        src.write_text("# Title\n\nLine one\n\nLine two\n", encoding="utf-8")
        dst = tmp_path / "out"
        result = process_import(src, dst, "markdown")

        assert result["title"] == "readme"
        assert len(result["blocks"]) == 3
        assert result["blocks"][0]["text"] == "# Title"
        assert result["blocks"][0]["origin"] == "extracted"
        assert result["blocks"][0]["locator"]["file"] == "readme.md"
        assert result["blocks"][0]["locator"]["line"] == 1
        assert result["original_path"] is not None

    def test_empty_file(self, tmp_path: pathlib.Path) -> None:
        src = tmp_path / "empty.md"
        src.write_text("", encoding="utf-8")
        result = process_import(src, tmp_path / "out", "markdown")
        assert result["blocks"] == []


# =============================================================================
# process_import — HTML (script removal is the key requirement)
# =============================================================================


class Test_process_import_html:
    def test_script_removed(self, tmp_path: pathlib.Path) -> None:
        html = (
            "<html><head>"
            "<script>alert('xss');</script>"
            "<style>.x{color:red}</style>"
            "</head><body>"
            "<h1>Hello</h1>"
            "<p>Normal paragraph.</p>"
            "<noscript>JS disabled</noscript>"
            "<iframe src='evil'></iframe>"
            "</body></html>"
        )
        src = tmp_path / "page.html"
        src.write_text(html, encoding="utf-8")
        result = process_import(src, tmp_path / "out", "html")

        assert result["title"] == "page"

        texts = [b["text"] for b in result["blocks"]]
        all_text = " ".join(texts)
        assert "alert" not in all_text
        assert "color:red" not in all_text
        assert "JS disabled" not in all_text
        assert "evil" not in all_text

        assert "Hello" in all_text
        assert "Normal paragraph" in all_text

    def test_title_extracted(self, tmp_path: pathlib.Path) -> None:
        html = "<html><head><title>My Cool Page</title></head><body><p>Hi</p></body></html>"
        src = tmp_path / "cool.html"
        src.write_text(html, encoding="utf-8")
        result = process_import(src, tmp_path / "out", "html")
        assert result["title"] == "My Cool Page"

    def test_multiple_scripts(self, tmp_path: pathlib.Path) -> None:
        html = "<body><script>var a=1;</script><p>safe</p><script>alert(2);</script>"
        src = tmp_path / "scripts.html"
        src.write_text(html, encoding="utf-8")
        result = process_import(src, tmp_path / "out", "html")
        texts = " ".join(b["text"] for b in result["blocks"])
        assert "alert" not in texts
        assert "var a=1" not in texts
        assert "safe" in texts

    def test_svg_tag_removed(self, tmp_path: pathlib.Path) -> None:
        html = "<body><svg><script>bad</script></svg><p>ok</p></body>"
        src = tmp_path / "svg.html"
        src.write_text(html, encoding="utf-8")
        result = process_import(src, tmp_path / "out", "html")
        texts = " ".join(b["text"] for b in result["blocks"])
        assert "bad" not in texts
        assert "ok" in texts


# =============================================================================
# process_import — PDF
# =============================================================================


class Test_process_import_pdf:
    def _make_pdf(self, tmp_path: pathlib.Path, pages: list[list[str]]) -> pathlib.Path:
        return _create_pdf_simple(tmp_path, "test.pdf", pages)

    def test_text_extraction(self, tmp_path: pathlib.Path) -> None:
        pdf = self._make_pdf(tmp_path, [["Hello world"]])
        result = process_import(pdf, tmp_path / "out", "pdf")
        assert result["title"] == "test"
        assert len(result["blocks"]) >= 1
        assert result["original_path"] is not None

    def test_phrase_in_page(self, tmp_path: pathlib.Path) -> None:
        """Specific phrases in a PDF should appear in extracted blocks."""
        pdf = self._make_pdf(tmp_path, [["The quick brown fox jumps over the lazy dog"]])
        result = process_import(pdf, tmp_path / "out", "pdf")
        all_text = " ".join(b["text"] for b in result["blocks"])
        assert "The quick brown fox" in all_text

    def test_multi_page(self, tmp_path: pathlib.Path) -> None:
        pdf = self._make_pdf(tmp_path, [["Page 1 text"], ["Page 2 text"]])
        result = process_import(pdf, tmp_path / "out", "pdf")
        assert len(result["blocks"]) >= 2

    def test_low_text_warns_no_tesseract(self, tmp_path: pathlib.Path) -> None:
        """A page with almost no real text -> triggers the low-text path."""
        pdf = self._make_pdf(tmp_path, [[" "]])
        with mock.patch("mynotes.importers.shutil.which", return_value=None):
            result = process_import(pdf, tmp_path / "out", "pdf")
        assert len(result["warnings"]) >= 1
        assert "tesseract" in result["warnings"][0].lower()


# =============================================================================
# process_import — PDF with Tesseract OCR (subprocess mock, not Path.read_text)
# =============================================================================


class Test_process_import_pdf_ocr:
    def test_ocr_on_low_text_page(self, tmp_path: pathlib.Path) -> None:
        """Low-text page should trigger OCR when tesseract is available."""
        pdf = _create_pdf_simple(tmp_path, "scan.pdf", [[" "]])
        mock_png = tmp_path / "page_1.png"
        mock_png.touch()

        with mock.patch("mynotes.importers.shutil.which", return_value="/usr/bin/tesseract"):
            with mock.patch(
                "mynotes.importers._safe_subprocess",
                return_value=CompletedProcess(
                    args=["tesseract", "page_1.png", "page_1", "-l", "eng"],
                    returncode=0,
                    stdout="OCR result text\n",
                    stderr="",
                ),
            ):
                with mock.patch(
                    "mynotes.importers.render_page", return_value=mock_png
                ):
                    result = process_import(pdf, tmp_path / "out", "pdf")
                    texts = [b["text"] for b in result["blocks"]]
                    assert any("OCR result" in t for t in texts), texts
                    assert any(
                        b["origin"] == "ocr" for b in result["blocks"]
                    ), [b["origin"] for b in result["blocks"]]
                    assert len(result["assets"]) >= 1

    def test_ocr_timeout_warns(self, tmp_path: pathlib.Path) -> None:
        """Tesseract timeout should produce a warning, not crash."""
        pdf = _create_pdf_simple(tmp_path, "scan.pdf", [[" "]])

        with mock.patch("mynotes.importers.shutil.which", return_value="/usr/bin/tesseract"):
            with mock.patch(
                "mynotes.importers._safe_subprocess",
                side_effect=subprocess.TimeoutExpired(cmd=["tesseract"], timeout=60),
            ):
                with mock.patch(
                    "mynotes.importers.render_page",
                    return_value=tmp_path / "page_1.png",
                ):
                    result = process_import(pdf, tmp_path / "out", "pdf")
                    assert any("OCR failed" in w for w in result["warnings"])


# =============================================================================
# process_import — Typst (patch correct modules)
# =============================================================================


class Test_process_import_typst:
    def test_missing_typst_binary(self, tmp_path: pathlib.Path) -> None:
        src = tmp_path / "project"
        src.mkdir()
        (src / "main.typ").write_text("// Typst", encoding="utf-8")
        with mock.patch(
            "mynotes.importers.compile_typst",
            side_effect=FileNotFoundError("typst binary not found"),
        ):
            result = process_import(src, tmp_path / "out", "typst")
        assert len(result["warnings"]) >= 1
        assert "typst" in result["warnings"][0].lower() or "binary" in result["warnings"][0].lower()

    def test_compilation_failure(self, tmp_path: pathlib.Path) -> None:
        src = tmp_path / "project"
        src.mkdir()
        (src / "main.typ").write_text("// Typst", encoding="utf-8")
        dst = tmp_path / "out"

        with mock.patch(
            "mynotes.importers.compile_typst",
            side_effect=ValueError("typst: expected token"),
        ):
            result = process_import(src, dst, "typst")
            assert len(result["warnings"]) >= 1
            assert "compilation failed" in result["warnings"][0].lower()

    def test_successful_compilation(self, tmp_path: pathlib.Path) -> None:
        """Typst compiles -> PDF -> extract. Mock the compile_typst call."""
        src = tmp_path / "project"
        src.mkdir()
        (src / "main.typ").write_text("// Typst", encoding="utf-8")
        dst = tmp_path / "out"

        with mock.patch("mynotes.importers.compile_typst", return_value=None):
            with mock.patch(
                "mynotes.importers._process_pdf",
                return_value={"title": "project", "blocks": [], "assets": [], "warnings": []},
            ):
                result = process_import(src, dst, "typst")
                assert result["title"] == "project"
                assert len(result["assets"]) >= 1


# =============================================================================
# process_import — folder
# =============================================================================


class Test_process_import_folder:
    def test_empty_folder(self, tmp_path: pathlib.Path) -> None:
        src = tmp_path / "empty"
        src.mkdir()
        result = process_import(src, tmp_path / "out", "folder")
        assert len(result["warnings"]) >= 1
        assert "empty" in result["warnings"][0].lower()

    def test_uses_entrypoint(self, tmp_path: pathlib.Path) -> None:
        src = tmp_path / "project"
        src.mkdir()
        (src / "main.md").write_text("Hello from entrypoint", encoding="utf-8")
        (src / "other.md").write_text("Ignored", encoding="utf-8")
        result = process_import(
            src, tmp_path / "out", "folder", entrypoint="main.md"
        )
        texts = " ".join(b["text"] for b in result["blocks"])
        assert "Hello from entrypoint" in texts
        assert "Ignored" not in texts

    def test_fallback_to_first_file(self, tmp_path: pathlib.Path) -> None:
        src = tmp_path / "project"
        src.mkdir()
        (src / "a.md").write_text("First file", encoding="utf-8")
        (src / "b.md").write_text("Second file", encoding="utf-8")
        result = process_import(src, tmp_path / "out", "folder")
        texts = " ".join(b["text"] for b in result["blocks"])
        assert "First file" in texts

    def test_unknown_extension(self, tmp_path: pathlib.Path) -> None:
        src = tmp_path / "project"
        src.mkdir()
        (src / "data.xyz").write_text("blob", encoding="utf-8")
        result = process_import(src, tmp_path / "out", "folder")
        assert len(result["warnings"]) >= 1

    def test_entrypoint_traversal_rejected(self, tmp_path: pathlib.Path) -> None:
        src = tmp_path / "project"
        src.mkdir()
        (src / "safe.md").write_text("safe", encoding="utf-8")
        result = process_import(src, tmp_path / "out", "folder", entrypoint="../etc/passwd")
        assert len(result["warnings"]) >= 1
        assert "escape" in result["warnings"][0].lower()


# =============================================================================
# process_import — general / validation
# =============================================================================


class Test_process_import_general:
    def test_unsupported_kind(self, tmp_path: pathlib.Path) -> None:
        with pytest.raises(ValueError, match="unsupported kind"):
            process_import(tmp_path / "a.pdf", tmp_path / "out", "epub")

    def test_original_saved(self, tmp_path: pathlib.Path) -> None:
        src = tmp_path / "doc.md"
        src.write_text("content", encoding="utf-8")
        result = process_import(src, tmp_path / "out", "markdown")
        originals = tmp_path / "out" / "originals"
        assert originals.exists()
        assert (originals / "doc.md").read_bytes() == b"content"

    def test_destination_created(self, tmp_path: pathlib.Path) -> None:
        src = tmp_path / "doc.md"
        src.write_text("x", encoding="utf-8")
        new_dst = tmp_path / "new" / "nested" / "dir"
        result = process_import(src, new_dst, "markdown")
        assert new_dst.exists()

    def test_original_path_absolute(self, tmp_path: pathlib.Path) -> None:
        src = tmp_path / "doc.md"
        src.write_text("x", encoding="utf-8")
        result = process_import(src, tmp_path / "out", "markdown")
        assert pathlib.Path(result["original_path"]).is_absolute()

    def test_folder_kind_requires_directory(self, tmp_path: pathlib.Path) -> None:
        src = tmp_path / "not_a_dir.md"
        src.write_text("content", encoding="utf-8")
        with pytest.raises(ValueError, match="must be a directory"):
            process_import(src, tmp_path / "out", "folder")

    def test_file_kind_requires_file(self, tmp_path: pathlib.Path) -> None:
        src = tmp_path / "not_a_file"
        src.mkdir()
        with pytest.raises(ValueError, match="must be a regular file"):
            process_import(src, tmp_path / "out", "pdf")


# =============================================================================
# process_import — return shape
# =============================================================================


class Test_process_import_return_shape:
    def test_all_keys_present(self, tmp_path: pathlib.Path) -> None:
        src = tmp_path / "doc.md"
        src.write_text("hello", encoding="utf-8")
        result = process_import(src, tmp_path / "out", "markdown")
        assert "title" in result
        assert "blocks" in result
        assert "assets" in result
        assert "warnings" in result
        assert "original_path" in result

    def test_block_shape(self, tmp_path: pathlib.Path) -> None:
        src = tmp_path / "doc.md"
        src.write_text("line one\nline two\n", encoding="utf-8")
        result = process_import(src, tmp_path / "out", "markdown")
        block = result["blocks"][0]
        assert "text" in block
        assert "locator" in block
        assert "origin" in block
        assert block["origin"] in ("extracted", "ocr")

    def test_locator_shape(self, tmp_path: pathlib.Path) -> None:
        src = tmp_path / "doc.md"
        src.write_text("line one", encoding="utf-8")
        result = process_import(src, tmp_path / "out", "markdown")
        loc = result["blocks"][0]["locator"]
        assert "line" in loc
        assert "file" in loc
        assert "text_start" in loc
        assert "text_end" in loc
