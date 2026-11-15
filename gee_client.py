"""Google Earth Engine-backed scene discovery and tile layers."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from typing import Any

import ee

SENTINEL = "Sentinel-2"
LANDSAT = "Landsat 8/9"


class EarthEngineUnavailable(RuntimeError):
    pass


def initialize_ee(project: str, service_account: dict | None = None) -> None:
    """Use cloud service-account secrets or existing local Earth Engine OAuth credentials."""
    if not project:
        raise EarthEngineUnavailable("Укажите GEE_PROJECT в секретах приложения или переменных окружения.")
    try:
        if service_account:
            credentials = ee.ServiceAccountCredentials(
                service_account["client_email"],
                key_data=json.dumps(service_account),
            )
            ee.Initialize(credentials=credentials, project=project)
        else:
            ee.Initialize(project=project)
    except Exception as exc:
        raise EarthEngineUnavailable(
            "Не получилось подключиться к Earth Engine. Проверьте проект, права и авторизацию. "
            f"Техническая причина: {exc}"
        ) from exc


def _roi(bbox: list[float]) -> ee.Geometry:
    return ee.Geometry.Rectangle(bbox, proj="EPSG:4326", geodesic=False)


def _source_images(satellite: str, bbox: list[float], start: date, end_exclusive: date) -> ee.ImageCollection:
    region = _roi(bbox)
    if satellite == SENTINEL:
        collection = ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
    elif satellite == LANDSAT:
        collection = ee.ImageCollection("LANDSAT/LC08/C02/T1_L2").merge(
            ee.ImageCollection("LANDSAT/LC09/C02/T1_L2")
        )
    else:
        raise ValueError("Неизвестный спутник.")
    return collection.filterBounds(region).filterDate(start.isoformat(), end_exclusive.isoformat()).sort("system:time_start")


def list_month_scenes(satellite: str, bbox: list[float], year: int, month: int) -> list[dict[str, Any]]:
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    collection = _source_images(satellite, bbox, start, end)
    cloud_prop = "CLOUDY_PIXEL_PERCENTAGE" if satellite == SENTINEL else "CLOUD_COVER"
    fields = ee.Dictionary({
        "ids": collection.aggregate_array("system:id"),
        "indices": collection.aggregate_array("system:index"),
        "times": collection.aggregate_array("system:time_start"),
        "clouds": collection.aggregate_array(cloud_prop),
    }).getInfo()
    output = []
    for i, timestamp in enumerate(fields.get("times", [])):
        if timestamp is None:
            continue
        dt = datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc)
        ids = fields.get("ids") or []
        indices = fields.get("indices") or []
        clouds = fields.get("clouds") or []
        image_id = ids[i] if i < len(ids) else None
        index = indices[i] if i < len(indices) else ""
        cloud = clouds[i] if i < len(clouds) else None
        output.append({
            "date": dt.date().isoformat(),
            "time": dt.strftime("%H:%M"),
            "image_id": image_id,
            "index": index,
            "cloud": cloud,
            "label": f"{dt.strftime('%H:%M')} UTC · {str(index)[-24:]} · облачность {cloud if cloud is not None else '?'}%",
        })
    return output


def _sentinel(image: ee.Image, mask_clouds: bool = False) -> ee.Image:
    """Keep original scene pixels by default; optional SCL cloud mask."""
    rgb = image.select(["B2", "B3", "B4", "B11"], ["blue", "green", "red", "swir"]).multiply(0.0001)
    if not mask_clouds:
        return rgb
    scl = image.select("SCL")
    clear = (scl.neq(0).And(scl.neq(1)).And(scl.neq(3))
             .And(scl.neq(8)).And(scl.neq(9)).And(scl.neq(10)))
    # SCL 11 (snow/ice) is NOT removed.
    return rgb.updateMask(clear)


def _landsat(image: ee.Image, mask_clouds: bool = False) -> ee.Image:
    """Keep all native valid scene pixels unless cloud masking is requested."""
    rgb = (image.select(["SR_B2", "SR_B3", "SR_B4", "SR_B6"],
                        ["blue", "green", "red", "swir"])
           .multiply(0.0000275).add(-0.2))
    if not mask_clouds:
        return rgb
    qa = image.select("QA_PIXEL")
    # Bits 0–4: fill, dilated cloud, cirrus, cloud and cloud shadow; snow bit retained.
    clear = qa.bitwiseAnd(31).eq(0).And(image.select("QA_RADSAT").eq(0))
    return rgb.updateMask(clear)


def _prepare(image: ee.Image, satellite: str, mask_clouds: bool = False) -> ee.Image:
    return (_sentinel(image, mask_clouds) if satellite == SENTINEL
            else _landsat(image, mask_clouds))


def scene_image(satellite: str, bbox: list[float], day: date,
                image_id: str | None, mask_clouds: bool = False) -> ee.Image:
    if image_id:
        return _prepare(ee.Image(image_id), satellite, mask_clouds)
    collection = _source_images(satellite, bbox, day, day + timedelta(days=1))
    return collection.map(lambda im: _prepare(im, satellite, mask_clouds)).mosaic()


def median_images(satellite: str, bbox: list[float], first: date,
                  length: int) -> tuple[ee.Image, ee.Image, int]:
    """Unmasked and cloud-masked median composites built from the SAME scenes."""
    if not 2 <= length <= 9:
        raise ValueError("Длительность медианы должна составлять 2–9 дней.")
    collection = _source_images(satellite, bbox, first, first + timedelta(days=length))
    count = collection.size().getInfo()
    if count == 0:
        raise ValueError("В выбранном интервале нет спутниковых сцен.")
    raw = collection.map(lambda im: _prepare(im, satellite, False)).median()
    masked = collection.map(lambda im: _prepare(im, satellite, True)).median()
    return raw, masked, count


def _tile_url(image: ee.Image, vis: dict) -> str:
    return image.getMapId(vis)["tile_fetcher"].url_format


def _utm_projection(bbox: list[float], scale: int) -> ee.Projection:
    lon = (bbox[0] + bbox[2]) / 2
    lat = (bbox[1] + bbox[3]) / 2
    zone = min(60, max(1, int((lon + 180) // 6) + 1))
    epsg = 32600 + zone if lat >= 0 else 32700 + zone
    return ee.Projection(f"EPSG:{epsg}").atScale(scale)


def _temperature_layers(bbox: list[float], day: date) -> list[dict]:
    daily = ee.ImageCollection("ECMWF/ERA5_LAND/DAILY_AGGR").filterDate(
        day.isoformat(), (day + timedelta(days=1)).isoformat()
    )
    if daily.size().getInfo() == 0:
        return []
    temp = ee.Image(daily.first()).select("temperature_2m").subtract(273.15)
    area = _roi(bbox)
    proj30 = _utm_projection(bbox, 30)
    proj500 = _utm_projection(bbox, 500)
    proj90 = _utm_projection(bbox, 90)
    native_dem = ee.ImageCollection("COPERNICUS/DEM/GLO30_2024_1").filterBounds(area).select("DEM")
    dem = native_dem.mosaic().setDefaultProjection(proj30)
    coarse = (dem.resample("bilinear").reproject(proj500)
              .reduceResolution(reducer=ee.Reducer.mean(), maxPixels=1024)
              .reproject(temp.projection()))
    corrected = (temp.resample("bilinear")
                 .subtract(dem.resample("bilinear").subtract(coarse).multiply(0.006))
                 .reproject(proj90))
    result = []
    for level, color in [(-5, "536bd7"), (0, "ff3472"), (5, "fa9c40"), (10, "f6da4f")]:
        above = corrected.gte(level).toByte()
        line = above.focalMax(1).neq(above.focalMin(1)).selfMask().clip(area)
        result.append({
            "name": f"Изотерма {level:+d} °C",
            "url": _tile_url(line, {"palette": [color]}),
            "opacity": 0.9,
            "visible": False,
        })
    return result


def build_tile_layers(image: ee.Image, bbox: list[float], day: date,
                      masked_image: ee.Image) -> tuple[list[dict], list[str]]:
    """Default unmasked scene + switchable masked layer, snow boundary and isotherms."""
    area = _roi(bbox)
    rgb_vis = {
        "bands": ["red", "green", "blue"], "min": 0.025, "max": 0.35, "gamma": 1.15,
    }
    layers = [
        {
            "name": "Снимок без маски",
            "role": "raw",
            "url": _tile_url(image.clip(area), rgb_vis),
            "opacity": 0.55,
            "visible": True,
        },
        {
            "name": "Снимок с маской облаков",
            "role": "masked",
            "url": _tile_url(masked_image.clip(area), rgb_vis),
            "opacity": 0.55,
            "visible": False,
        },
    ]
    ndsi = image.normalizedDifference(["green", "swir"])
    valid = image.select("green").mask().And(image.select("swir").mask())
    snow = ndsi.gte(0.4).And(image.select("green").gt(0.1)).toByte().updateMask(valid)
    edge = (snow.focalMax(1).neq(snow.focalMin(1))
            .updateMask(valid.focalMin(1)).selfMask().clip(area))
    layers.append({
        "name": "Граница снега (NDSI)",
        "url": _tile_url(edge, {"palette": ["00ffff"]}),
        "opacity": 0.95,
        "visible": False,
    })
    warnings = []
    try:
        layers.extend(_temperature_layers(bbox, day))
    except Exception as exc:
        warnings.append(f"Не получилось построить изотермы ERA5-Land: {exc}")
    return layers, warnings
