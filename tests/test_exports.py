"""Tests for src/mynotes/exports.py"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import tempfile
from unittest import mock

import pytest

from mynotes.exports import (
    _citations_appendix,
    _enforce_safe_entrypoint,
    _enforce_local_file,
    _find_marp,
    _reject_unsafe_html,
    compile_typst,
    export_deck,
    prepare_markdown,
)


# ── _enforce_safe_entrypoint ──────────────────────────────────────────────────


class Test_enforce_safe_entrypoint:
    def test_accepts_simple_and_subdir(self, tmp_path: pathlib.Path) -> None:
        (tmp_path / "main.typ").touch()
        (tmp_path / "chapters").mkdir()
        (tmp_path / "chapters" / "intro.typ").touch()
        _enforce_safe_entrypoint("main.typ", tmp_path)
        _enforce_safe_entrypoint("chapters/intro.typ", tmp_path)

    @pytest.mark.parametrize(
        "path", ["/etc/passwd", "../etc/passwd", "a/../../etc/passwd"]
    )
    def test_rejects_traversal(self, tmp_path: pathlib.Path, path: str) -> None:
        with pytest.raises(ValueError, match="must be relative|\\.\\.|escapes"):
            _enforce_safe_entrypoint(path, tmp_path)


# ── _reject_unsafe_html ───────────────────────────────────────────────────────


class Test_reject_unsafe_html:
    @pytest.mark.parametrize(
        "content",
        [
            "<script>alert(1)</script>",
            'src="javascript:alert(1)"',
            'onclick="alert(1)"',
            "<object data='x'></object>",
            "<embed src='x' />",
            "<iframe src='x'></iframe>",
            "@import url('https://evil.com/style.css');",
            'background-image:url(https://evil.com/bg.png)',
            'theme="https://evil.com/custom.css"',
        ],
    )
    def test_rejects_unsafe(self, content: str) -> None:
        with pytest.raises(ValueError, match="unsafe"):
            _reject_unsafe_html(content)

    def test_accepts_clean(self) -> None:
        _reject_unsafe_html("---\nmarp: true\n---\n\n# Hello\n")
        _reject_unsafe_html("![diagram](diagram.svg)")


# ── _enforce_local_file ───────────────────────────────────────────────────────


class Test_enforce_local_file:
    def test_accepts_absolute(self, tmp_path: pathlib.Path) -> None:
        _enforce_local_file(tmp_path / "img.png")

    def test_rejects_relative(self) -> None:
        with pytest.raises(ValueError, match="must be absolute"):
            _enforce_local_file(pathlib.PurePath("relative/path.png"))


# ── compile_typst ─────────────────────────────────────────────────────────────


class Test_compile_typst:
    def test_missing_binary(self, tmp_path: pathlib.Path) -> None:
        src = tmp_path / "project"; src.mkdir()
        (src / "main.typ").touch()
        with mock.patch("mynotes.exports.shutil.which", return_value=None):
            with pytest.raises(FileNotFoundError, match="'typst' CLI"):
                compile_typst(src, tmp_path / "out")

    def test_source_not_directory(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            src = pathlib.Path(d) / "notadir"
            src.write_text("foo")
            with mock.patch("mynotes.exports.shutil.which", return_value="/bin/typst"):
                with pytest.raises(ValueError, match="must be a directory"):
                    compile_typst(src, pathlib.Path(d) / "out")

    def test_unsafe_entrypoint(self, tmp_path: pathlib.Path) -> None:
        with mock.patch("mynotes.exports.shutil.which", return_value="/bin/typst"):
            with pytest.raises(ValueError, match="escapes|'\\.\\.'|must be relative"):
                compile_typst(tmp_path, tmp_path / "out", entrypoint="../../../etc/passwd")

    def test_entrypoint_not_found(self, tmp_path: pathlib.Path) -> None:
        with mock.patch("mynotes.exports.shutil.which", return_value="/bin/typst"):
            with pytest.raises(ValueError, match="not found"):
                compile_typst(tmp_path, tmp_path / "out", entrypoint="missing.typ")

    def test_success_directory_dest(self, tmp_path: pathlib.Path) -> None:
        src = tmp_path / "project"; src.mkdir()
        (src / "main.typ").write_text("// Typst", encoding="utf-8")
        r = mock.Mock(returncode=0, stderr="")
        with mock.patch("mynotes.exports.shutil.which", return_value="/bin/typst"):
            with mock.patch("mynotes.exports.subprocess.run", return_value=r) as spy:
                compile_typst(src, tmp_path / "out")
                assert "--root" in spy.call_args.args[0]
                assert "pdf" in spy.call_args.args[0]

    def test_success_file_dest(self, tmp_path: pathlib.Path) -> None:
        src = tmp_path / "project"; src.mkdir()
        (src / "main.typ").write_text("// Typst", encoding="utf-8")
        dst = tmp_path / "out.pdf"
        with mock.patch("mynotes.exports.shutil.which", return_value="/bin/typst"):
            with mock.patch("mynotes.exports.subprocess.run", return_value=mock.Mock(returncode=0, stderr="")) as spy:
                compile_typst(src, dst)
                assert str(dst) in spy.call_args.args[0]

    def test_failure_surfaces_stderr(self, tmp_path: pathlib.Path) -> None:
        src = tmp_path / "project"; src.mkdir()
        (src / "main.typ").write_text("// Typst", encoding="utf-8")
        with mock.patch("mynotes.exports.shutil.which", return_value="/bin/typst"):
            with mock.patch("mynotes.exports.subprocess.run", return_value=mock.Mock(returncode=1, stderr="Error: unexpected")):
                with pytest.raises(ValueError, match="unexpected"):
                    compile_typst(src, tmp_path / "out")

    def test_timeout(self, tmp_path: pathlib.Path) -> None:
        src = tmp_path / "project"; src.mkdir()
        (src / "main.typ").write_text("// Typst", encoding="utf-8")
        with mock.patch("mynotes.exports.shutil.which", return_value="/bin/typst"):
            with mock.patch("mynotes.exports.subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=["typst"], timeout=120)):
                with pytest.raises(subprocess.TimeoutExpired) as exc:
                    compile_typst(src, tmp_path / "out")
                assert "120 s" in str(exc.value.stderr or "")


# ── _find_marp ────────────────────────────────────────────────────────────────


class Test_find_marp:
    def test_env_var_takes_precedence(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MYNOTES_MARP", "/usr/local/bin/marp --no-verify")
        assert _find_marp() == ["/usr/local/bin/marp", "--no-verify"]

    def test_env_quoted_args(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MYNOTES_MARP", '"/usr/bin/node" /opt/marp/marp.js --pdf')
        assert _find_marp() == ["/usr/bin/node", "/opt/marp/marp.js", "--pdf"]


# ── export_deck ────────────────────────────────────────────────────────────────


class Test_export_deck:
    def _png(self, tmp_path: pathlib.Path, name: str) -> pathlib.Path:
        """Write a valid 1x1 red PNG via Pillow."""
        from PIL import Image
        p = tmp_path / name
        Image.new("RGB", (1, 1), (255, 0, 0)).save(p, format="PNG")
        return p

    def _marp(self, html: pathlib.Path, pdf: pathlib.Path) -> mock.MagicMock:
        def _r(*args, **kw):
            c = args[0] if args else []
            if "--pdf" in c:
                pdf.write_bytes(b"%PDF-placeholder")
            else:
                html.write_text("<html>", encoding="utf-8")
            return mock.Mock(returncode=0, stderr="")
        return mock.patch("mynotes.exports.subprocess.run", side_effect=_r)

    def test_basic_export(self, tmp_path: pathlib.Path) -> None:
        md = "# Slide 1\n\n---\n\n# Slide 2\n"
        dst = tmp_path / "export"
        with self._marp(dst / "deck.html", dst / "deck.pdf"):
            result = export_deck(md, dst)
        assert result["html_path"] == str(dst / "deck.html")
        assert result["pdf_path"] == str(dst / "deck.pdf")
        assert result["manifest_path"] == str(dst / "deck-manifest.json")
        assert (dst / "deck.md").read_text(encoding="utf-8") == md
        m = json.loads((dst / "deck-manifest.json").read_text(encoding="utf-8"))
        assert m["assets"] == []
        assert m["citations"] == []

    def test_with_assets(self, tmp_path: pathlib.Path) -> None:
        img = self._png(tmp_path, "chart.png")
        assets = [{"path": str(img), "title": "Chart", "locator": "fig:1", "excerpt": "Rev"}]
        dst = tmp_path / "export"
        with self._marp(dst / "deck.html", dst / "deck.pdf"):
            export_deck("# Slide\n![chart](chart.png)\n", dst, assets=assets)
        m = json.loads((dst / "deck-manifest.json").read_text(encoding="utf-8"))
        assert len(m["assets"]) == 1
        assert m["assets"][0]["title"] == "Chart"
        assert m["assets"][0]["local_path"] == "../exports/chart.png"
        assert (dst / img.name).exists()

    def test_with_citations(self, tmp_path: pathlib.Path) -> None:
        citations = [{"title": "Doe", "source": "https://example.com", "excerpt": "Key", "version_id": "v1"}]
        dst = tmp_path / "export"
        with self._marp(dst / "deck.html", dst / "deck.pdf"):
            export_deck("# Slide\n", dst, citations=citations)
        m = json.loads((dst / "deck-manifest.json").read_text(encoding="utf-8"))
        assert m["citations"][0]["title"] == "Doe"
        assert (dst / "citations.md").exists()
        assert "Doe" in (dst / "citations.md").read_text(encoding="utf-8")

    @pytest.mark.parametrize(
        "content", ["![x](javascript:alert(1))", "<script>x</script>", "<embed src='x' />"]
    )
    def test_rejects_unsafe(self, content: str, tmp_path: pathlib.Path) -> None:
        with pytest.raises(ValueError, match="unsafe"):
            export_deck(content, tmp_path / "out")

    def test_rejects_remote_background(self, tmp_path: pathlib.Path) -> None:
        with pytest.raises(ValueError, match="unsafe"):
            export_deck("# Slide\n<div style='background-image:url(https://evil.com/x.png)'>", tmp_path / "out")

    def test_rejects_missing_asset(self, tmp_path: pathlib.Path) -> None:
        with pytest.raises(FileNotFoundError, match="not found"):
            export_deck("# Slide\n", tmp_path / "exp", assets=[{"path": str(tmp_path / "no.png")}])

    def test_rejects_nonabsolute_asset(self, tmp_path: pathlib.Path) -> None:
        with pytest.raises(ValueError, match="must be absolute"):
            export_deck("# Slide\n", tmp_path / "exp", assets=[{"path": "relative.png"}])

    def test_pdf_failure(self, tmp_path: pathlib.Path) -> None:
        dst = tmp_path / "export"
        def _fail(*args, **kw):
            c = args[0] if args else []
            if "--pdf" in c: raise ValueError("chromium error")
            return mock.Mock(returncode=0, stderr="")
        with mock.patch("mynotes.exports.subprocess.run", side_effect=_fail):
            with pytest.raises(ValueError, match="chromium error"):
                export_deck("# Slide\n", dst)

    def test_pdf_no_output(self, tmp_path: pathlib.Path) -> None:
        dst = tmp_path / "export"
        html = dst / "deck.html"
        def _html_only(*args, **kw):
            c = args[0] if args else []
            if "--pdf" not in c: html.write_text("<html>", encoding="utf-8")
            return mock.Mock(returncode=0, stderr="")
        with mock.patch("mynotes.exports.subprocess.run", side_effect=_html_only):
            with pytest.raises(FileNotFoundError, match="did not produce"):
                export_deck("# Slide\n", dst)

    def test_chromium_flag(self, tmp_path: pathlib.Path) -> None:
        dst = tmp_path / "export"
        html = dst / "deck.html"
        pdf = dst / "deck.pdf"
        calls: list[list[str]] = []
        def _track(*args, **kw):
            c = args[0] if args else []
            if "--pdf" in c: pdf.write_bytes(b"%PDF")
            else: html.write_text("<html>", encoding="utf-8")
            calls.append(c)
            return mock.Mock(returncode=0, stderr="")
        with mock.patch.dict(os.environ, {"MYNOTES_CHROMIUM": "/opt/chrome/chrome"}):
            with mock.patch("mynotes.exports.subprocess.run", side_effect=_track):
                export_deck("# Slide\n", dst)
        assert any("--browser-path" in c and "/opt/chrome/chrome" in c for c in calls)


# ── _citations_appendix ───────────────────────────────────────────────────────


class Test_citations_appendix:
    def test_single(self) -> None:
        out = _citations_appendix([{"title": "Foo", "source": "https://x.com", "version_id": "v1", "excerpt": "Bar"}])
        assert "**1. Foo**" in out
        assert "Source: https://x.com" in out
        assert "Version ID: v1" in out
        assert "> Bar" in out

    def test_empty(self) -> None:
        assert _citations_appendix([]) == "## Citations and Source References\n"


# ── prepare_markdown ──────────────────────────────────────────────────────────


class Test_prepare_markdown:
    def test_returns_input(self) -> None:
        assert prepare_markdown("---\nmarp: true\n---\n\n# Test") == "---\nmarp: true\n---\n\n# Test"
    def test_empty(self) -> None:
        assert prepare_markdown("") == ""


# ── Smoke (requires Marp + node) ──────────────────────────────────────────────


class TestSmokeIntegration:
    @pytest.fixture(autouse=True)
    def _check_marp(self) -> None:
        import shutil
        marp = _find_marp()
        if not (shutil.which(marp[0]) or pathlib.Path(marp[0]).is_file()):
            pytest.skip("Marp CLI not installed")
        pytest.importorskip("node", reason="node required")

    def test_marp_html_generation(self, tmp_path: pathlib.Path) -> None:
        dst = tmp_path / "smoke"
        html = dst / "deck.html"
        pdf = dst / "deck.pdf"
        def _real(*args, **kw):
            c = args[0] if args else []
            if "--pdf" in c: pdf.write_bytes(b"%PDF-smoke")
            else: subprocess.run(c, timeout=120, capture_output=True)
            return mock.Mock(returncode=0, stderr="")
        with mock.patch("mynotes.exports.subprocess.run", side_effect=_real):
            export_deck("# Slide 1\n\n---\n\n# Slide 2\n", dst)
        assert html.is_file()
        content = html.read_text(encoding="utf-8")
        assert "Slide 1" in content and "Slide 2" in content
