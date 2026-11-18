"""Regressions for research notes, two-satellite date mode and mosaic gallery."""
from __future__ import annotations

from datetime import date
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

from PIL import Image as PILImage

from gallery import build_gallery_pdf, new_gallery_item, photo_layer_date, sorted_gallery
from session_store import normalize_payload


def photo(title: str, source_date: str = ""):
    item = new_gallery_item([46, 42, 46.2, 42.1])
    item["title"] = title
    item["source_date"] = source_date
    output = BytesIO()
    PILImage.new("RGB", (240, 130), (210, 221, 234)).save(output, "PNG")
    return item, output.getvalue()


def test_date_sort_newest_first_and_legacy_title_fallback():
    a, _ = photo("S2 · 01.06.25", "2025-06-01")
    b, _ = photo("L8/9 · 05.07.25", "2025-07-05")
    c, _ = photo("S2 · 20.06.25 · день")
    c["captured_at"] = "2026-11-18T10:00:00+00:00"
    assert photo_layer_date(c) == "2025-06-20"
    assert [i["id"] for i in sorted_gallery([a, b, c])] == [
        b["id"], c["id"], a["id"],
    ]


def test_pdf_notes_change_report_and_are_before_images():
    first, image = photo("S2 · 15.06.25", "2025-06-15")
    empty_notes = build_gallery_pdf([first], {first["id"]: image}, "Дагестан")
    with_notes = build_gallery_pdf(
        [first], {first["id"]: image}, "Дагестан",
        project_notes="Заметки по снегу: Северный склон.\n\nУчасток 2.",
    )
    assert empty_notes.startswith(b"%PDF") and with_notes.startswith(b"%PDF")
    assert len(with_notes) > len(empty_notes)
    assert b"/Type /Page" in with_notes
    # The first page of the second document contains a title and report notes,
    # and images follow a PageBreak in the report-generation source.
    pdf_source = (
        Path(__file__).resolve().parents[1] / "gallery.py"
    ).read_text(encoding="utf-8")
    assert pdf_source.index("if project_notes.strip():") < pdf_source.index(
        "for index, item in enumerate(items, 1):", pdf_source.index("if project_notes.strip():")
    )


def test_session_payload_retains_custom_gallery_order():
    a, _ = photo("S2 · 01.06.25", "2025-06-01")
    b, _ = photo("L8/9 · 05.07.25", "2025-07-05")
    payload = normalize_payload({
        "name": "Дагестан", "routes": [], "points": [], "notes": [
            {"id": "research", "text": "Один текст", "created": ""}
        ],
        "gallery": [a, b],
        "gallery_custom_order": True,
    })
    assert payload["gallery_custom_order"] is True
    assert [x["id"] for x in payload["gallery"]] == [a["id"], b["id"]]


def test_frontend_masonry_supports_dragging():
    root = Path(__file__).resolve().parents[1]
    html = (root / "gallery_mosaic/index.html").read_text(encoding="utf-8")
    assert "grid-template-columns" in html
    assert "dragstart" in html and "drop" in html
    assert "emit('reorder'" in html


def test_capture_uses_roi_only_without_redundant_area_rerun():
    root = Path(__file__).resolve().parents[1]
    html = (root / "map_component/index.html").read_text(encoding="utf-8")
    assert "width:cropWidth,height:cropHeight" in html
    assert "finalCanvas.toBlob" in html
    assert "clearTimeout(activeSnapshotTimer)" in html
    assert "action:'area_selected'" not in html


def test_research_note_single_widget():
    from streamlit.testing.v1 import AppTest
    root = Path(__file__).resolve().parents[1]
    at = AppTest.from_file(root / "app.py", default_timeout=30).run()
    assert not at.exception
    matched = [a for a in at.text_area if a.label == "Заметки об исследовании"]
    assert len(matched) == 1
    assert not any(b.label == "Добавить заметку" for b in at.button)
    matched[0].set_value("Текущий снеговой покров — 2300 м").run()
    assert not at.exception
    assert at.session_state["research_note"] == "Текущий снеговой покров — 2300 м"
