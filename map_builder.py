"""Map payload builder used by the Leaflet Streamlit component and legacy iframe."""
from __future__ import annotations

import json
from pathlib import Path

from geo_utils import bounding_box

TEMPLATE = Path(__file__).resolve().parent / "templates" / "map.html"


def map_config(
    routes: list[dict],
    points: list[dict],
    tiles: list[dict] | None = None,
    caption: str = "",
    map_state: dict | None = None,
) -> dict:
    return {
        "routes": routes,
        "points": points,
        "tiles": tiles or [],
        "bounds": bounding_box(routes, [] if routes else points),
        "caption": caption,
        "mapState": map_state or {},
    }


def map_html(
    routes: list[dict],
    points: list[dict],
    tiles: list[dict] | None = None,
    caption: str = "",
) -> str:
    # Kept for compatibility; the shareable app uses the bidirectional component.
    payload = json.dumps(
        map_config(routes, points, tiles, caption),
        ensure_ascii=False,
    ).replace("</", "<\\/")
    return TEMPLATE.read_text(encoding="utf-8").replace("__PAYLOAD_JSON__", payload)
