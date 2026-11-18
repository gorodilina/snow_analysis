"""Georeferenced RGB/SWIR reflectance crop from an Earth Engine source layer.

Unlike the gallery screenshot, a GeoTIFF stores quantitative raster pixels in
EPSG:4326/UTM georeferencing, not the visual Leaflet composition.
"""
from __future__ import annotations

import math
from datetime import date, timedelta

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


def geotiff_filename(spec: dict, bounds: list[float], scale: int) -> str:
    """Human-readable filename with source, date/range, resolution and area."""
    satellite = "S2" if spec["satellite"] == SENTINEL else "L8-9"
    if spec.get("kind") == "median":
        start = date.fromisoformat(spec["start"])
        end = start + timedelta(days=int(spec["length"]) - 1)
        scene = f"median_{start:%Y-%m-%d}_to_{end:%Y-%m-%d}"
    else:
        day = date.fromisoformat(spec["day"])
        scene = day.isoformat()
        time = str(spec.get("time") or "").strip().replace(":", "-")
        if time and len(time) == 5 and time.replace("-", "").isdigit():
            scene += "_" + time
    west, south, east, north = validate_bounds(bounds)
    lon, lat = (west + east) / 2, (south + north) / 2
    area = f"{abs(lat):.2f}{'N' if lat >= 0 else 'S'}_{abs(lon):.2f}{'E' if lon >= 0 else 'W'}"
    return f"{satellite}_{scene}_{int(scale)}m_{area}.tif"


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
