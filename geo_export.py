"""Georeferenced RGB/SWIR reflectance crop from an Earth Engine source layer.

Unlike the gallery screenshot, a GeoTIFF stores quantitative raster pixels in
EPSG:4326/UTM georeferencing, not the visual Leaflet composition.
"""
from __future__ import annotations

import math

import ee
import requests

from gallery import validate_bounds
from gee_client import image_from_layer, SENTINEL
from session_store import SessionStorageError

MAX_EXPORT_PIXELS = 1_600_000


def suggested_scale(satellite: str) -> int:
    # Sentinel-2 SWIR is 20m, Landsat Collection 2 SR is 30m.
    return 20 if satellite == SENTINEL else 30


def estimate_grid(bounds: list[float], scale: int) -> tuple[int, int]:
    west, south, east, north = validate_bounds(bounds)
    lat = (south + north) / 2
    dx = (east - west) * 111320 * math.cos(math.radians(lat))
    dy = (north - south) * 111320
    return max(1, math.ceil(dx / scale)), max(1, math.ceil(dy / scale))


def export_geotiff(spec: dict, bounds: list[float], scale: int | None = None) -> bytes:
    """Download a bounded 4-band float GeoTIFF (not a screenshot)."""
    scale = int(scale or suggested_scale(spec["satellite"]))
    if scale < 10 or scale > 200:
        raise SessionStorageError("Разрешение GeoTIFF должно быть 10–200 м.")
    area = validate_bounds(bounds)
    width, height = estimate_grid(area, scale)
    if max(width, height) > 9000 or width * height > MAX_EXPORT_PIXELS:
        raise SessionStorageError(
            f"Область слишком велика ({width}×{height} пикселей). "
            "Уменьшите область или увеличьте размер пикселя."
        )
    source = image_from_layer(spec, area, mask_clouds=False)
    image = source.select(["red", "green", "blue", "swir"]).toFloat()
    url = image.getDownloadURL({
        "region": ee.Geometry.Rectangle(area, proj="EPSG:4326", geodesic=False),
        "scale": scale,
        "format": "GEO_TIFF",
        "filePerBand": False,
        "name": "snow_reflectance",
    })
    try:
        response = requests.get(url, timeout=(10, 90))
        response.raise_for_status()
        data = response.content
    except requests.RequestException as exc:
        raise SessionStorageError("Не удалось выгрузить GeoTIFF из Earth Engine.") from exc
    if len(data) > 32_000_000:
        raise SessionStorageError("Результат больше 32 МБ. Уменьшите область.")
    if not data.startswith((b"II*\x00", b"MM\x00*")):
        raise SessionStorageError("Earth Engine вернул не GeoTIFF. Проверьте область.")
    return data
