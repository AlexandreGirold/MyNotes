"""Tests for pptx_importer.process_pptx."""
from __future__ import annotations
import io, pathlib
import pytest
from PIL import Image
from pptx import Presentation
from pptx.util import Emu
import mynotes.pptx_importer as p


def _pillow(r=255, g=0, b=0) -> bytes:
    buf = io.BytesIO(); Image.new("RGB", (2, 2), (r, g, b)).save(buf, "PNG"); return buf.getvalue()


def _save(dest, slide_fn) -> None:
    prs = Presentation(); slide_fn(prs.slides.add_slide(prs.slide_layouts[0])); prs.save(str(dest))


@pytest.fixture()
def _empty(tmp_path):
    d = tmp_path / "e.pptx"; _save(d, lambda s: None); return d


@pytest.fixture()
def _texts(tmp_path):
    d = tmp_path / "t.pptx"
    def _cb(s):
        for i, t in enumerate(["T1", "T2"]):
            s.shapes.add_textbox(Emu(100_000), Emu(100_000+i*500_000), Emu(5_000_000), Emu(1_000_000)).text_frame.text = t
    _save(d, _cb); return d


@pytest.fixture()
def _img(tmp_path):
    d = tmp_path / "i.pptx"
    _save(d, lambda s: s.shapes.add_picture(io.BytesIO(_pillow(0, 100, 200)), Emu(100_000), Emu(300_000), Emu(1_000_000), Emu(1_000_000))); return d


@pytest.fixture()
def _tbl(tmp_path):
    d = tmp_path / "tb.pptx"
    def _cb(s):
        t = s.shapes.add_table(2, 3, Emu(100_000), Emu(400_000), Emu(5_000_000), Emu(2_000_000)).table
        t.cell(0, 0).text = "r0c0"; t.cell(1, 2).text = "r1c2"
    _save(d, _cb); return d


@pytest.fixture()
def _grp(tmp_path):
    d = tmp_path / "g.pptx"
    def _cb(s):
        a = s.shapes.add_textbox(Emu(100_000), Emu(600_000), Emu(2_000_000), Emu(1_000_000))
        a.text_frame.text = "inner A"
        b = s.shapes.add_textbox(Emu(3_000_000), Emu(600_000), Emu(2_000_000), Emu(1_000_000))
        b.text_frame.text = "inner B"
        s.shapes.add_group_shape([a, b])
    _save(d, _cb); return d


@pytest.fixture()
def _grp_img(tmp_path):
    d = tmp_path / "gi.pptx"
    def _cb(s):
        s.shapes.add_picture(io.BytesIO(_pillow(0, 128, 255)), Emu(100_000), Emu(900_000), Emu(1_500_000), Emu(1_500_000))
    _save(d, _cb); return d


@pytest.fixture()
def _full(tmp_path):
    d = tmp_path / "f.pptx"
    prs = Presentation()
    s0 = prs.slides.add_slide(prs.slide_layouts[0])
    s0.shapes.add_textbox(Emu(100_000), Emu(100_000), Emu(5_000_000), Emu(1_000_000)).text_frame.text = "My Presentation"
    s0.shapes.add_picture(io.BytesIO(_pillow(0, 128, 255)), Emu(4_000_000), Emu(2_000_000), Emu(1_000_000), Emu(1_000_000))
    s1 = prs.slides.add_slide(prs.slide_layouts[6])
    t = s1.shapes.add_table(2, 2, Emu(500_000), Emu(500_000), Emu(4_000_000), Emu(2_000_000)).table
    t.cell(0, 0).text = "Header A"; t.cell(0, 1).text = "Header B"
    t.cell(1, 0).text = "Val 1"; t.cell(1, 1).text = "Val 2"
    s2 = prs.slides.add_slide(prs.slide_layouts[6])
    a = s2.shapes.add_textbox(Emu(500_000), Emu(500_000), Emu(2_000_000), Emu(1_000_000)); a.text_frame.text = "Inner A"
    b = s2.shapes.add_textbox(Emu(3_000_000), Emu(500_000), Emu(2_000_000), Emu(1_000_000)); b.text_frame.text = "Inner B"
    prs.save(str(d)); return d


# ── tests ─────────────────────────────────────────────────────────────────
class TestProcessPptx:
    def test_keys(self, _empty, tmp_path):
        r = p.process_pptx(_empty, tmp_path)
        assert set(r) == {"title", "blocks", "assets", "warnings"}

    def test_nonexistent(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            p.process_pptx(tmp_path / "no.pptx", tmp_path)

    def test_title(self, _texts, tmp_path):
        assert p.process_pptx(_texts, tmp_path)["title"] == "T1"

    def test_block_structure(self, _texts, tmp_path):
        b = p.process_pptx(_texts, tmp_path)["blocks"][0]
        assert set(b) == {"text", "locator", "origin"} and b["origin"] == "extracted"

    def test_texts(self, _texts, tmp_path):
        t = [b["text"] for b in p.process_pptx(_texts, tmp_path)["blocks"]]
        assert "T1" in t and "T2" in t

    def test_image_bytes(self, _img, tmp_path):
        a = p.process_pptx(_img, tmp_path)["assets"][0]
        assert pathlib.Path(a["path"]).read_bytes() == _pillow(0, 100, 200)

    def test_image_coords(self, _img, tmp_path):
        a = p.process_pptx(_img, tmp_path)["assets"][0]
        assert a["x"] == Emu(100_000) and a["y"] == Emu(300_000)

    def test_image_kind(self, _img, tmp_path):
        assert p.process_pptx(_img, tmp_path)["assets"][0]["kind"] == "image"

    def test_image_ext(self, tmp_path):
        d = tmp_path / "j.pptx"
        prs = Presentation(); s = prs.slides.add_slide(prs.slide_layouts[6])
        buf = io.BytesIO(); Image.new("RGB", (2, 2)).save(buf, "JPEG")
        s.shapes.add_picture(buf, Emu(0), Emu(0), Emu(100_000), Emu(100_000)); prs.save(str(d))
        assert p.process_pptx(d, tmp_path)["assets"][0]["path"].endswith(".jpg")

    def test_multi_image(self, tmp_path):
        d = tmp_path / "x.pptx"; _save(d, lambda s: [s.shapes.add_picture(io.BytesIO(_pillow(i*50, 100, 200)), Emu(100_000), Emu(300_000+i*300_000), Emu(1_000_000), Emu(1_000_000)) for i in range(3)])
        r = p.process_pptx(d, tmp_path)
        assert len(r["assets"]) == 3 and len(set(a["y"] for a in r["assets"])) == 3

    def test_multi_coords(self, tmp_path):
        d = tmp_path / "y.pptx"; _save(d, lambda s: [s.shapes.add_picture(io.BytesIO(_pillow(i*50, 100, 200)), Emu(100_000), Emu(300_000+i*300_000), Emu(1_000_000), Emu(1_000_000)) for i in range(2)])
        r = p.process_pptx(d, tmp_path)
        assert r["assets"][0]["y"] == Emu(300_000) and r["assets"][1]["y"] == Emu(600_000)

    def test_table(self, _tbl, tmp_path):
        bl = [b for b in p.process_pptx(_tbl, tmp_path)["blocks"] if "r" in b["text"]]
        assert len(bl) >= 1 and "r0c0" in bl[0]["text"] and "r1c2" in bl[0]["text"]

    def test_group_texts(self, _grp, tmp_path):
        t = [b["text"] for b in p.process_pptx(_grp, tmp_path)["blocks"]]
        assert "inner A" in t and "inner B" in t

    def test_group_img(self, _grp_img, tmp_path):
        imgs = [a for a in p.process_pptx(_grp_img, tmp_path)["assets"] if a["kind"] == "image"]
        assert len(imgs) == 1 and pathlib.Path(imgs[0]["path"]).read_bytes() == _pillow(0, 128, 255)

    def test_group_stable(self, _grp_img, tmp_path):
        imgs = [a for a in p.process_pptx(_grp_img, tmp_path)["assets"] if a["kind"] == "image"]
        assert imgs[0]["x"] == Emu(100_000) and imgs[0]["y"] == Emu(900_000)

    def test_chart(self, tmp_path):
        d = tmp_path / "c.pptx"
        prs = Presentation(); s = prs.slides.add_slide(prs.slide_layouts[0])
        import pptx.chart.data as cd
        from pptx.enum.chart import XL_CHART_TYPE
        data = cd.ChartData(); data.categories = ["A"]; data.add_series("s", [1])
        s.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Emu(0), Emu(0), Emu(100_000), Emu(100_000), data)
        prs.save(str(d))
        assert isinstance(p.process_pptx(d, tmp_path)["warnings"], list)

    def test_multi_slide(self, _full, tmp_path):
        s = {b["locator"]["slide"] for b in p.process_pptx(_full, tmp_path)["blocks"]}
        assert 1 in s and 2 in s and 3 in s

    def test_full_deck(self, _full, tmp_path):
        r = p.process_pptx(_full, tmp_path)
        assert r["title"] == "My Presentation"
        t = [b["text"] for b in r["blocks"]]
        assert "Inner A" in t and "Inner B" in t and any("Header" in b["text"] for b in r["blocks"])

    def test_warnings(self, _texts, tmp_path):
        assert p.process_pptx(_texts, tmp_path)["warnings"] == []

    def test_ocr(self, _texts, tmp_path):
        assert {b["origin"] for b in p.process_pptx(_texts, tmp_path)["blocks"]} == {"extracted"}

    def test_unique(self, tmp_path):
        d = tmp_path / "u.pptx"; _save(d, lambda s: [s.shapes.add_picture(io.BytesIO(_pillow(i, 0, 0)), Emu(100_000), Emu(300_000+i*300_000), Emu(1_000_000), Emu(1_000_000)) for i in range(2)])
        paths = [a["path"] for a in p.process_pptx(d, tmp_path)["assets"]]
        assert len(paths) == len(set(paths))
