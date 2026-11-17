"""Compact label, unlimited catalogue, export/import, and legacy migration tests."""
from __future__ import annotations

from datetime import date
from io import BytesIO
from zipfile import ZipFile
import json

import pytest

from project_layers import (
    layer_label, migrate_legacy_selection, new_median_layer, new_scene_layer,
    normalize_layers,
)
from project_archive import build_project_zip, import_project_zip, safe_export_filename
from session_store import normalize_payload


def make_project():
    a = new_scene_layer(
        "Sentinel-2", date(2025, 6, 15), "COPERNICUS/S2_SR_HARMONIZED/20250615T085609", "08:56"
    )
    b = new_median_layer("Landsat 8/9", date(2025, 6, 1), 5)
    return {
        "name": "Кавказ снежная обстановка",
        "routes": [{"name": "Тестовый трек", "coordinates": [[46.0, 42.0], [46.1, 42.1]]}],
        "points": [{"name": "Перевал", "coordinates": [46.05, 42.05]}],
        "notes": [{"id": "one", "text": "Снег на северной стороне", "created": "2026-11-17"}],
        "ui": {"satellite": "Sentinel-2", "mode": "Медианная мозаика"},
        "layers": [a, b],
        "map_state": {
            "base": "Рельеф (Esri)",
            "track": True, "points": True, "labels": True,
            "center": [42.05, 46.05], "zoom": 11,
            "overlays": {
                a["id"]: {"visible": True, "opacity": 0.4},
                b["id"]: {"visible": False, "opacity": 0.9},
                "snow-outline": {"visible": True, "opacity": 0.95},
            },
        },
    }


def test_short_labels_single_and_median():
    single = new_scene_layer("Sentinel-2", date(2025, 6, 15), None)
    assert single["name"] == "S2 · 15.06.25 · день"
    assert new_scene_layer("Sentinel-2", date(2025, 6, 15), "COPERNICUS/test", "08:31")["name"] == "S2 · 15.06.25 08:31"
    assert new_median_layer("Landsat 8/9", date(2025, 6, 1), 5)["name"] == "L8/9 · 01–05.06.25 · мед."
    assert new_median_layer("Sentinel-2", date(2025, 6, 29), 5)["name"] == "S2 · 29.06–03.07.25 · мед."
    assert new_median_layer("Sentinel-2", date(2025, 12, 30), 5)["name"] == "S2 · 30.12.25–03.01.26 · мед."


def test_unlimited_distinct_image_ids():
    layers = [new_median_layer("Sentinel-2", date(2025, 6, 1), 3) for _ in range(250)]
    assert len(normalize_layers(layers)) == 250
    assert len({l["id"] for l in layers}) == 250


def test_zip_includes_no_raster_data_and_round_trip():
    project = make_project()
    zip_bytes = build_project_zip(project)
    assert len(zip_bytes) < 20000
    with ZipFile(BytesIO(zip_bytes)) as z:
        assert set(z.namelist()) == {
            "project.json", "tracks.geojson", "points.geojson",
            "manifest.json", "README.txt"
        }
        decoded = json.loads(z.read("project.json"))
        assert "tile_url" not in decoded
        assert len(json.loads(z.read("tracks.geojson"))["features"]) == 1
    restored = import_project_zip(zip_bytes)
    assert restored["layers"] == normalize_layers(project["layers"])
    assert restored["map_state"]["overlays"][project["layers"][1]["id"]]["opacity"] == 0.9
    assert restored["notes"][0]["text"] == "Снег на северной стороне"


def test_old_saved_sessions_can_restore_single_image():
    old = {"kind": "median", "satellite": "Sentinel-2", "start": "2025-06-01", "length": 5}
    recovered = migrate_legacy_selection(old)
    assert len(recovered) == 1
    assert recovered[0]["name"] == "S2 · 01–05.06.25 · мед."


def test_reject_bad_archive_and_duplicate_ids():
    with pytest.raises(Exception):
        import_project_zip(b"bad zip")
    project = make_project()
    project["layers"].append(project["layers"][0])
    with pytest.raises(ValueError):
        normalize_layers(project["layers"])


def test_db_schema_persists_complete_catalogue():
    project = make_project()
    cleaned = normalize_payload(project)
    assert len(cleaned["layers"]) == 2
    assert cleaned["map_state"]["overlays"][project["layers"][0]["id"]]["visible"]
    assert safe_export_filename("Дагестан") == "snow-analysis.zip"
