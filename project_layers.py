"""Stable metadata for independent satellite layers (no raster data in sessions)."""
from __future__ import annotations

import secrets
from datetime import date, timedelta
from typing import Any

CONTOUR_COLORS = {
    "snow": "#00ffff",
    "iso_-5": "#536bd7",
    "iso_0": "#ff3472",
    "iso_5": "#fa9c40",
    "iso_10": "#f6da4f",
}


def contour_flags(map_state: dict | None) -> dict[str, bool]:
    """Migrate the former one-switch contour mode into five independent flags."""
    state = map_state if isinstance(map_state, dict) else {}
    saved = state.get("analysis_toggles")
    if isinstance(saved, dict):
        return {key: bool(saved.get(key, False)) for key in CONTOUR_COLORS}
    value = bool(state.get("analysis_visible", False))
    return {key: value for key in CONTOUR_COLORS}


SATELLITE_CODES = {"Sentinel-2": "S2", "Landsat 8/9": "L8/9"}


def layer_label(spec: dict[str, Any]) -> str:
    """Compact labels; include satellite, acquisition date/range and mode."""
    satellite = SATELLITE_CODES.get(spec.get("satellite"), str(spec.get("satellite", "?")))
    if spec.get("kind") == "median":
        start = date.fromisoformat(spec["start"])
        end = start + timedelta(days=int(spec["length"]) - 1)
        if start.year == end.year and start.month == end.month:
            window = f"{start:%d}–{end:%d.%m.%y}"
        elif start.year == end.year:
            window = f"{start:%d.%m}–{end:%d.%m.%y}"
        else:
            window = f"{start:%d.%m.%y}–{end:%d.%m.%y}"
        return f"{satellite} · {window} · мед."
    day = date.fromisoformat(spec["day"])
    if spec.get("image_id"):
        time = spec.get("time", "")
        return f"{satellite} · {day:%d.%m.%y}" + (f" {time}" if time else "")
    return f"{satellite} · {day:%d.%m.%y} · день"


def new_scene_layer(satellite: str, day: date, image_id: str | None,
                    time: str = "") -> dict[str, Any]:
    spec = {
        "id": "img_" + secrets.token_hex(9),
        "kind": "scene",
        "satellite": satellite,
        "day": day.isoformat(),
        "image_id": image_id,
        "time": time if image_id else "",
    }
    spec["name"] = layer_label(spec)
    return spec


def new_median_layer(satellite: str, start: date, length: int) -> dict[str, Any]:
    if not 2 <= length <= 9:
        raise ValueError("Медианный интервал должен быть от 2 до 9 дней.")
    spec = {
        "id": "img_" + secrets.token_hex(9),
        "kind": "median",
        "satellite": satellite,
        "start": start.isoformat(),
        "length": int(length),
    }
    spec["name"] = layer_label(spec)
    return spec


def migrate_legacy_selection(selection: dict | None) -> list[dict]:
    """Older saved studies stored one 'selection' rather than a list."""
    if not selection or not isinstance(selection, dict):
        return []
    kind = selection.get("kind")
    try:
        if kind == "scene":
            return [new_scene_layer(
                selection["satellite"], date.fromisoformat(selection["day"]),
                selection.get("image_id"),
            )]
        if kind == "median":
            return [new_median_layer(
                selection["satellite"], date.fromisoformat(selection["start"]),
                int(selection["length"]),
            )]
    except (KeyError, ValueError, TypeError):
        return []
    return []


def normalize_layers(layers: list[dict]) -> list[dict]:
    """Check layer descriptors when loading a ZIP or remote session."""
    result, ids = [], set()
    if not isinstance(layers, list):
        raise ValueError("Список слоёв должен быть массивом.")
    for item in layers:
        if not isinstance(item, dict):
            raise ValueError("Некорректное описание спутникового слоя.")
        ident = str(item.get("id", ""))
        if not ident.startswith("img_") or len(ident) > 80 or not ident[4:].isalnum():
            raise ValueError("Некорректный идентификатор слоя.")
        if ident in ids:
            raise ValueError("Повторяется идентификатор спутникового слоя.")
        ids.add(ident)
        sat = item.get("satellite")
        if sat not in SATELLITE_CODES:
            raise ValueError("Неизвестный спутник в сохранённом проекте.")
        kind = item.get("kind")
        if kind == "scene":
            date.fromisoformat(item["day"])
            image_id = item.get("image_id")
            if image_id is not None and (not isinstance(image_id, str) or
                                         len(image_id) > 350 or not image_id.startswith(("COPERNICUS/", "LANDSAT/"))):
                raise ValueError("Некорректный идентификатор GEE.")
            clean = {k: item.get(k) for k in ("id", "kind", "satellite", "day", "image_id", "time")}
            clean["time"] = str(clean.get("time") or "")[:5]
        elif kind == "median":
            date.fromisoformat(item["start"])
            n = int(item["length"])
            if not 2 <= n <= 9:
                raise ValueError("Интервал медианы должен быть от 2 до 9 дней.")
            clean = {k: item[k] for k in ("id", "kind", "satellite", "start")}
            clean["length"] = n
        else:
            raise ValueError("Неизвестный тип спутникового слоя.")
        clean["name"] = layer_label(clean)
        result.append(clean)
    return result
