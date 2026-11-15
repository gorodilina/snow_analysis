"""Optional, officially licensed Google Terrain basemap via Map Tiles API.

A separate Google Maps Platform API key, enabled Map Tiles API and billing
are required. The key is embedded in browser tile requests, as documented
by Google; restrict it to this API and the Streamlit app's website origin.
"""
from __future__ import annotations

from urllib.parse import quote

import requests


def get_google_terrain(api_key: str, bbox: list[float]) -> dict:
    """Create a Google terrain tile session and obtain viewport attribution."""
    key = api_key.strip()
    if not key:
        return {}
    response = requests.post(
        "https://tile.googleapis.com/v1/createSession",
        params={"key": key},
        json={
            "mapType": "terrain",
            "language": "ru-RU",
            "region": "RU",
            "layerTypes": ["layerRoadmap"],
        },
        timeout=12,
    )
    response.raise_for_status()
    session = response.json()["session"]
    tiles = (
        "https://tile.googleapis.com/v1/2dtiles/{z}/{x}/{y}"
        "?session=" + quote(session, safe="") + "&key=" + quote(key, safe="")
    )
    west, south, east, north = [float(v) for v in bbox]
    viewport = requests.get(
        "https://tile.googleapis.com/tile/v1/viewport",
        params={
            "session": session, "key": key, "zoom": 9,
            "west": west, "south": south, "east": east, "north": north,
        },
        timeout=12,
    )
    viewport.raise_for_status()
    data = viewport.json()
    return {
        "url": tiles,
        "attribution": str(data.get("copyright", "Map data © Google Maps")),
        # Dynamic attribution uses these endpoints only when Google is active.
        "viewportUrl": (
            "https://tile.googleapis.com/tile/v1/viewport"
            "?session=" + quote(session, safe="") + "&key=" + quote(key, safe="")
        ),
    }
