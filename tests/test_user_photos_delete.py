"""Custom gallery photo import, project removal, image click and TIFF naming."""
from __future__ import annotations

from io import BytesIO
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from PIL import Image

from gallery import GalleryStorage, new_gallery_item, normalize_gallery, read_user_image
from geo_export import geotiff_filename
from project_delete import delete_project
from session_store import SessionStorageError, SessionStore, make_token


def png_bytes():
    stream = BytesIO()
    Image.new("RGB", (200, 120), (30, 120, 180)).save(stream, format="PNG")
    return stream.getvalue()


def test_custom_png_and_jpeg_without_geographic_rectangle():
    photo = png_bytes()
    raw, fmt = read_user_image(photo, "Мой снимок.png")
    assert raw == photo
    assert fmt == "png"
    item = new_gallery_item(None, fmt)
    item["title"] = "Моя фотография"
    assert normalize_gallery([item])[0]["bounds"] is None
    jpeg = BytesIO()
    Image.new("RGB", (80, 40)).save(jpeg, format="JPEG")
    assert read_user_image(jpeg.getvalue(), "photo.jpeg")[1] == "jpg"
    with pytest.raises(SessionStorageError):
        read_user_image(b"not-an-image", "wrong.png")


def test_storage_project_prefix_list_and_bulk_delete():
    token = make_token()
    gallery = GalleryStorage("https://demo.supabase.co", "sb_secret_test")
    names = ["gal_" + "a" * 20 + ".png", "gal_" + "b" * 20 + ".jpg"]
    with patch.object(gallery, "_call", side_effect=[
        ('[{"name":"' + names[0] + '"},{"name":"' + names[1] + '"}]').encode(),
        b'[]',  # bulk remove response
        b'[]',  # verify folder is now empty
    ]) as call:
        count = gallery.remove_project_files(token)
        assert count == 2
        args = call.call_args_list
        assert args[0].args[0] == "POST"
        assert args[0].kwargs["json"]["prefix"] == token + "/"
        assert args[1].args[0] == "DELETE"
        assert args[1].kwargs["json"]["prefixes"] == [
            token + "/" + name for name in names
        ]


def test_refuse_to_delete_other_project_objects():
    token = make_token()
    gallery = GalleryStorage("https://demo.supabase.co", "sb_secret_test")
    with patch.object(gallery, "_call", return_value=b'[{"name":"../not-ours.png"}]'):
        with pytest.raises(SessionStorageError):
            gallery.remove_project_files(token)


def test_delete_project_removes_storage_first_and_database_last():
    token = make_token()
    actions = []
    store = Mock()
    gallery = Mock()
    store.get.side_effect = lambda _: actions.append("read") or {"name": "Test"}
    gallery.remove_project_files.side_effect = lambda _: actions.append("images") or 3
    store.delete.side_effect = lambda _: actions.append("row")
    assert delete_project(store, gallery, token) == 3
    assert actions == ["read", "images", "row"]


def test_delete_failure_keeps_database_row():
    token = make_token()
    store = Mock()
    gallery = Mock()
    store.get.return_value = {"name": "Test"}
    gallery.remove_project_files.side_effect = SessionStorageError("Storage failed")
    with pytest.raises(SessionStorageError):
        delete_project(store, gallery, token)
    store.delete.assert_not_called()


def test_session_delete_exact_token_and_affected_row():
    token = make_token()
    session = SessionStore("https://demo.supabase.co", "sb_secret_test")
    response = Mock()
    response.json.return_value = [{"token": token}]
    with patch.object(session, "_request", return_value=response) as call:
        session.delete(token)
        assert call.call_args.args == ("DELETE",)
        assert call.call_args.kwargs["params"] == {
            "token": "eq." + token, "select": "token"
        }
        assert call.call_args.kwargs["headers"]["Prefer"] == "return=representation"
    response.json.return_value = []
    with patch.object(session, "_request", return_value=response):
        with pytest.raises(SessionStorageError):
            session.delete(token)


def test_geotiff_names_human_readable_and_distinct_areas():
    scene = {
        "kind": "scene", "satellite": "Sentinel-2",
        "day": "2025-06-15", "time": "08:32",
        "id": "img_not_in_export_name",
    }
    a = geotiff_filename(scene, [46, 42, 46.2, 42.2], 20)
    b = geotiff_filename(scene, [47, 42, 47.2, 42.2], 20)
    assert a == "S2_15.06.2025_08-32_20m_42.10N_46.10E.tif"
    assert a != b
    assert "img_not" not in a
    median = {
        "kind": "median", "satellite": "Landsat 8/9",
        "start": "2025-06-01", "length": 5,
    }
    assert geotiff_filename(median, [46, 42, 46.2, 42.2], 30).startswith(
        "L8-9_median_01.06.2025_to_05.06.2025_30m_"
    )


def test_photo_click_opens_dialog_and_paste_zone_exists():
    root = Path(__file__).resolve().parents[1]
    app = (root / "app.py").read_text(encoding="utf-8")
    mosaic = (root / "gallery_mosaic/index.html").read_text(encoding="utf-8")
    assert '@st.dialog("Просмотр и редактирование изображения", width="large")' in app
    assert 'gallery_edit_dialog(mosaic_event["image_id"])' in app
    assert "paste-zone" in mosaic
    assert "document.addEventListener('paste'" in mosaic
    assert "Ctrl+V" in mosaic
    assert "Загрузить проект из ZIP" not in app
