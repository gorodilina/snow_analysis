"""Compose a Leaflet map; all map overlay toggles and opacity sliders live in its Layers panel."""
from __future__ import annotations
import json
from pathlib import Path
from geo_utils import bounding_box

TEMPLATE = Path(__file__).resolve().parent / 'templates' / 'map.html'


def map_html(routes: list[dict], points: list[dict], tiles: list[dict] | None = None,
             caption: str = '', google_terrain: dict | None = None) -> str:
    payload = json.dumps({
        'routes': routes, 'points': points, 'tiles': tiles or [],
        'bounds': bounding_box(routes, [] if routes else points), 'caption': caption,
        'googleTerrain': google_terrain or {},
    }, ensure_ascii=False)
    # Never allow a user-provided object name to terminate the embedding script tag.
    payload = payload.replace('</', '<\\/')
    return TEMPLATE.read_text(encoding='utf-8').replace('__PAYLOAD_JSON__', payload)
