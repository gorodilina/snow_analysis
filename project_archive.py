"""Small portable ZIP backup of a snow-analysis study (never includes raster tiles)."""
from __future__ import annotations

import io
import json
import re
import zipfile
from datetime import datetime, timezone

from project_layers import normalize_layers, migrate_legacy_selection
from session_store import normalize_payload, SessionStorageError
from gallery import normalize_gallery, MAX_IMAGE_BYTES

MAX_IMPORT_BYTES = 60 * 1024 * 1024
MAX_PROJECT_JSON_BYTES = 1_000_000


def _as_geojson(payload: dict, kind: str) -> dict:
    features = []
    if kind == "routes":
        for route in payload["routes"]:
            features.append({
                "type": "Feature",
                "properties": {"name": route["name"]},
                "geometry": {"type": "LineString", "coordinates": route["coordinates"]},
            })
    else:
        for point in payload["points"]:
            features.append({
                "type": "Feature",
                "properties": {"name": point["name"]},
                "geometry": {"type": "Point", "coordinates": point["coordinates"]},
            })
    return {"type": "FeatureCollection", "features": features}


def build_project_zip(payload: dict, images: dict[str, bytes] | None = None) -> bytes:
    clean = normalize_payload(payload)
    normalize_layers(clean.get("layers", []))
    gallery = normalize_gallery(clean.get("gallery", []))
    images = images or {}
    # Portable archives must not retain object keys belonging to an older URL.
    clean["gallery"] = [{**item, "storage_key": None} for item in gallery]
    now = datetime.now(timezone.utc).isoformat()
    instructions = (
        "Проект «Анализ снежной обстановки»\n\n"
        "project.json — полная копия исследования (включая все слои, заметки, "
        "параметры отображения).\n"
        "tracks.geojson — геометрия треков.\n"
        "points.geojson — точки.\n"
        "manifest.json — информация об экспорте.\n\n"
        "В папке gallery/ лежат снимки пользователя из галереи.\n"
        "Спутниковые растры НЕ включены в ZIP: сохранены их ID, даты и режимы.\n"
        "При восстановлении нужен доступ к Google Earth Engine.\n"
        "Не публикуйте архив, если треки или заметки конфиденциальны.\n"
    )
    manifest = {
        "format": "snow-analysis-zip",
        "version": 1,
        "exported_utc": now,
        "image_layer_count": len(clean.get("layers", [])),
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED, compresslevel=8) as z:
        for path, value in [
            ("project.json", clean),
            ("tracks.geojson", _as_geojson(clean, "routes")),
            ("points.geojson", _as_geojson(clean, "points")),
            ("manifest.json", manifest),
        ]:
            z.writestr(path, json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False))
        for item in gallery:
            data = images.get(item["id"])
            if data is None or len(data) > MAX_IMAGE_BYTES:
                raise SessionStorageError("Не хватает фотографии галереи для резервного ZIP.")
            z.writestr(f"gallery/{item['id']}.{item['format']}", data)
        z.writestr("README.txt", instructions)
    return output.getvalue()


def import_project_zip(data: bytes) -> dict:
    if len(data) > MAX_IMPORT_BYTES:
        raise SessionStorageError("ZIP больше 5 МБ.")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            if "project.json" not in z.namelist():
                raise SessionStorageError("В архиве отсутствует project.json.")
            if z.getinfo("project.json").file_size > MAX_PROJECT_JSON_BYTES:
                raise SessionStorageError("Файл project.json слишком большой.")
            obj = json.loads(z.read("project.json"))
            clean = normalize_payload(obj)
            layers = clean.get("layers") or migrate_legacy_selection(clean.get("selection"))
            clean["layers"] = normalize_layers(layers)
            gallery = normalize_gallery(clean.get("gallery", []))
            blobs = {}
            total = 0
            for item in gallery:
                path = f"gallery/{item['id']}.{item['format']}"
                if path not in z.namelist():
                    raise SessionStorageError(f"Нет файла {path} в архиве.")
                info = z.getinfo(path)
                total += info.file_size
                if info.file_size > MAX_IMAGE_BYTES or total > MAX_IMPORT_BYTES:
                    raise SessionStorageError("Фотографии в ZIP слишком большие.")
                blobs[item["id"]] = z.read(path)
            clean["gallery"] = [{**item, "storage_key": None} for item in gallery]
            clean["_gallery_media"] = blobs
        return clean
    except (zipfile.BadZipFile, UnicodeDecodeError, ValueError, KeyError) as exc:
        raise SessionStorageError("Не удалось прочитать архив проекта.") from exc


def safe_export_filename(name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", name.lower()).strip("-")
    return (slug[:55] or "snow-analysis") + ".zip"
