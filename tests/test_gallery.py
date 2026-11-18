"""Gallery image integrity, PDF/ZIP exports, storage API, and GeoTIFF bounds."""
from __future__ import annotations

import base64
import io
import secrets
from unittest.mock import Mock, patch
from zipfile import ZipFile

import pytest
from PIL import Image as PILImage

from gallery import (
    GalleryStorage, build_gallery_pdf, build_gallery_zip, new_gallery_item,
    normalize_gallery, read_snapshot, validate_bounds,
)
from geo_export import estimate_grid, suggested_scale
from project_archive import build_project_zip, import_project_zip
from session_store import SessionStorageError, make_token, normalize_payload


def example():
    image = PILImage.new("RGB", (240, 130), color=(200, 220, 245))
    f = io.BytesIO()
    image.save(f, format="PNG")
    photo = f.getvalue()
    item = new_gallery_item([46.1, 42.1, 46.2, 42.2])
    item["title"] = "Снежная обстановка"
    item["note"] = "Граница снега на северном склоне."
    return item, photo


def test_validate_bounds_and_normalize_gallery():
    item, _ = example()
    assert validate_bounds(item["bounds"]) == [46.1, 42.1, 46.2, 42.2]
    assert normalize_gallery([item])[0]["title"] == item["title"]
    with pytest.raises(SessionStorageError):
        validate_bounds([47, 42, 46, 43])
    with pytest.raises(SessionStorageError):
        normalize_gallery([item, item])


def test_reencode_browser_screenshot():
    _, photo = example()
    url = "data:image/png;base64," + base64.b64encode(photo).decode()
    out, fmt = read_snapshot(url, "jpg")
    assert fmt == "jpg"
    with PILImage.open(io.BytesIO(out)) as im:
        assert im.size == (240, 130) and im.format == "JPEG"
    with pytest.raises(SessionStorageError):
        read_snapshot("data:text/html;base64,PGgxPg==", "png")


def test_gallery_zip_only_contains_photos():
    item, photo = example()
    data = build_gallery_zip([item], {item["id"]: photo})
    with ZipFile(io.BytesIO(data)) as z:
        assert z.namelist() == [item["title"] + ".png"]
        assert z.read(item["title"] + ".png") == photo


def test_gallery_pdf_russian_caption():
    item, photo = example()
    data = build_gallery_pdf([item], {item["id"]: photo}, "Дагестан — июнь")
    assert data.startswith(b"%PDF")
    assert len(data) > 1000


def test_backup_zip_preserves_gallery_images_without_external_links():
    item, photo = example()
    project = {
        "name": "Горы", "routes": [], "points": [], "notes": [],
        "layers": [], "gallery": [item], "map_state": {"analysis_visible": True}
    }
    data = build_project_zip(project, {item["id"]: photo})
    recovered = import_project_zip(data)
    assert recovered["gallery"][0]["title"] == item["title"]
    assert recovered["gallery"][0]["storage_key"] is None
    assert recovered["_gallery_media"][item["id"]] == photo
    assert recovered["map_state"]["analysis_visible"] is True


def test_gallery_cloud_api_paths_and_secret_header():
    item, photo = example()
    token = make_token()
    response = Mock()
    response.content = b'{"Key":"ok"}'
    response.raise_for_status.return_value = None
    with patch("gallery.requests.request", return_value=response) as request:
        store = GalleryStorage("https://example.supabase.co", "sb_secret_test")
        key = store.put(token, item, photo)
        assert key.startswith(token + "/gal_")
        args, kwargs = request.call_args
        assert args[0] == "POST"
        assert kwargs["headers"]["apikey"] == "sb_secret_test"
        assert "Authorization" not in kwargs["headers"]
        assert kwargs["headers"]["Content-Type"] == "image/png"
        store.get(key)
        assert request.call_args.args[0] == "GET"
        assert "/object/authenticated/" in request.call_args.args[1]


def test_geotiff_grid_is_bounded():
    assert suggested_scale("Sentinel-2") == 20
    assert suggested_scale("Landsat 8/9") == 30
    w, h = estimate_grid([46, 42, 46.02, 42.01], 20)
    assert 70 < w < 100 and 50 < h < 65


def test_streamlit_recovers_stale_gallery_selection_after_deletion_or_restore():
    """Opening a different gallery must not break the active photo."""
    from pathlib import Path
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(
        Path(__file__).resolve().parents[1] / "app.py", default_timeout=30
    ).run()
    assert not at.exception

    first, first_bytes = example()
    at.session_state["gallery"] = [first]
    at.session_state["gallery_blobs"] = {first["id"]: first_bytes}
    at.session_state["gallery_active_id"] = "gal_00000000000000000000"
    at.run()
    assert not at.exception
    assert at.session_state["gallery_active_id"] == first["id"]

    # Gallery view becomes empty, then a completely different photo arrives.
    at.session_state["gallery"] = []
    at.run()
    assert not at.exception

    another, another_bytes = example()
    at.session_state["gallery"] = [another]
    at.session_state["gallery_blobs"] = {another["id"]: another_bytes}
    at.session_state["gallery_active_id"] = first["id"]
    at.run()
    assert not at.exception
    assert at.session_state["gallery_active_id"] == another["id"]


def test_two_photo_captures_can_be_confirmed_and_preserved():
    """Regression: adding a second screenshot must not replace the first."""
    from pathlib import Path
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(
        Path(__file__).resolve().parents[1] / "app.py", default_timeout=30
    ).run()
    assert not at.exception
    made = []
    for _ in range(2):
        photo, blob = example()
        photo["title"] = "S2 · 15.06.25 · день"
        at.session_state["pending_photo"] = {"item": photo, "data": blob}
        at.run()
        assert not at.exception
        matching = [b for b in at.button if b.label == "Сохранить изображение в галерею"]
        assert len(matching) == 1
        matching[0].click()
        at.run()
        assert not at.exception
        made.append(photo["id"])
        # The gallery is now sorted newest first, not in click order.
        assert set(entry["id"] for entry in at.session_state["gallery"]) == set(made)
        assert len(at.session_state["gallery"]) == len(made)
        assert at.session_state["pending_photo"] is None

    assert len(at.session_state["gallery_blobs"]) == 2


def test_gallery_titles_follow_visible_satellite_images():
    from gallery import suggested_gallery_title
    layers = [
        {"id": "img_a", "name": "L8/9 · 01–05.06.25 · мед."},
        {"id": "img_b", "name": "S2 · 15.06.25 08:31"},
    ]
    assert suggested_gallery_title(layers, {"overlays": {
        "img_a": {"visible": False}, "img_b": {"visible": True},
    }}) == "S2 · 15.06.25 08:31"
    assert suggested_gallery_title(layers, {"overlays": {
        "img_a": {"visible": True}, "img_b": {"visible": True},
    }}) == "S2 · 15.06.25 08:31 + L8/9 · 01–05.06.25 · мед."
