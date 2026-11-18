"""Regression tests for saved drawings, filename safety and independent contour toggles."""
from __future__ import annotations

from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pytest
from PIL import Image as PILImage

from gallery import (
    build_gallery_pdf, build_gallery_zip, gallery_export_filename, new_gallery_item,
    normalize_annotations, normalize_gallery, render_annotations,
)
from project_layers import CONTOUR_COLORS, contour_flags
from session_store import SessionStorageError, normalize_payload


def base_photo(fmt="png"):
    with PILImage.new("RGB", (600, 400), color=(245, 247, 249)) as im:
        buf = BytesIO()
        im.save(buf, "PNG" if fmt == "png" else "JPEG")
    item = new_gallery_item([46, 42, 46.1, 42.1], fmt)
    return item, buf.getvalue()


def test_cyrillic_gallery_filenames_and_collision_avoidance():
    items, images = [], {}
    for name in ["S2 · 15.06.25", "S2 · 15.06.25", "../Северный / склон:*?", "CON"]:
        item, blob = base_photo()
        item["title"] = name
        items.append(item)
        images[item["id"]] = blob
    exported = build_gallery_zip(items, images)
    with ZipFile(BytesIO(exported)) as z:
        names = z.namelist()
    assert names[:2] == ["S2 · 15.06.25.png", "S2 · 15.06.25 (2).png"]
    assert all("/" not in name and "\\" not in name for name in names)
    assert names[2].startswith("Северный") is False  # leading dots sanitized
    assert names[3] == "_CON.png"


def test_annotation_roundtrip_keeps_original_unchanged():
    item, photo = base_photo()
    original_photo = bytes(photo)
    strokes = [
        {"type": "pen", "points": [[.1, .1], [.4, .3], [.8, .3]],
         "color": "#ff0055", "width": .01},
        {"type": "arrow", "points": [[.1, .9], [.8, .6]],
         "color": "#0088ff", "width": .008},
        {"type": "text", "points": [[.15, .5]], "text": "Снег",
         "color": "#111111", "width": .008},
    ]
    item["annotations"] = normalize_annotations(strokes)
    normalized = normalize_gallery([item])[0]
    assert len(normalized["annotations"]) == 3
    saved = render_annotations(photo, normalized)
    assert saved != photo
    assert photo == original_photo
    with PILImage.open(BytesIO(saved)) as im:
        assert im.size == (600, 400)
        assert im.convert("RGB").getpixel((60, 40)) != (245, 247, 249)
    pdf = build_gallery_pdf([normalized], {item["id"]: saved}, "Заметки")
    assert pdf.startswith(b"%PDF")
    cloud = normalize_payload({"routes": [], "points": [], "notes": [],
                              "layers": [], "gallery": [normalized]})
    assert cloud["gallery"][0]["annotations"][2]["text"] == "Снег"


def test_unsafe_annotations_rejected():
    good = {"type": "pen", "points": [[.1, .2]], "color": "#fb0055", "width": .005}
    for bad in [
        {**good, "points": [[-1, .2]]},
        {**good, "color": "javascript:alert(1)"},
        {**good, "width": 2},
        {**good, "points": [[.1, .2]] * 351},
    ]:
        with pytest.raises(SessionStorageError):
            normalize_annotations([bad])


def test_independent_contours_and_legacy_migration():
    keys = list(CONTOUR_COLORS)
    assert keys == ["snow", "iso_-5", "iso_0", "iso_5", "iso_10"]
    assert all(not v for v in contour_flags({}).values())
    assert all(contour_flags({"analysis_visible": True}).values())
    custom = contour_flags({"analysis_toggles": {
        "snow": True, "iso_-5": False, "iso_0": True,
    }})
    assert custom == {
        "snow": True, "iso_-5": False, "iso_0": True,
        "iso_5": False, "iso_10": False,
    }


def test_capture_dom_ignores_blue_rectangle_and_uses_hires():
    html = (Path(__file__).resolve().parents[1] /
            "map_component/index.html").read_text(encoding="utf-8")
    assert "className:'capture-selection-overlay'" in html
    assert "onclone:clone" in html
    assert "querySelectorAll('.capture-selection-overlay')" in html
    assert "scale:2.8" in html
    assert "10000000/(sw*sh)" in html


def test_canvas_editor_exposes_save_undo_clear():
    source = (Path(__file__).resolve().parents[1] /
              "gallery_editor/index.html").read_text(encoding="utf-8")
    assert "save_annotations" in source
    assert "tool-arrow" in source and "tool-text" in source
    assert "document.getElementById('undo')" in source
    assert "document.getElementById('clear')" in source
