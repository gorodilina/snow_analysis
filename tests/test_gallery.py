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
        assert z.namelist() == ["001.png"]
        assert z.read("001.png") == photo


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
    """Reproduce Streamlit StopIteration when selectbox ID outlives the photo."""
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file("app.py", default_timeout=30).run()
    assert not at.exception

    first, first_bytes = example()
    at.session_state["gallery"] = [first]
    at.session_state["gallery_blobs"] = {first["id"]: first_bytes}
    at.session_state["gallery_selected_id"] = "gal_00000000000000000000"  # deleted ID
    at.run()
    assert not at.exception
    assert at.session_state["gallery_selected_id"] == first["id"]

    # Delete all pictures, then restore a different collection.
    at.session_state["gallery"] = []
    at.run()
    assert not at.exception
    assert "gallery_selected_id" not in at.session_state

    another, another_bytes = example()
    at.session_state["gallery"] = [another]
    at.session_state["gallery_blobs"] = {another["id"]: another_bytes}
    at.session_state["gallery_selected_id"] = first["id"]
    at.run()
    assert not at.exception
    assert at.session_state["gallery_selected_id"] == another["id"]
