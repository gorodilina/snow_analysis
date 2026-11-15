"""Bidirectional Leaflet component returning viewport and layer control state."""
from __future__ import annotations

from pathlib import Path

import streamlit.components.v1 as components

FRONTEND = Path(__file__).resolve().parent / "map_component"
_map_component = components.declare_component("snow_research_map", path=str(FRONTEND))


def research_map(config: dict, render_id: str) -> dict | None:
    """Map state events are returned via a safe, server-owned Streamlit component."""
    return _map_component(config=config, render_id=render_id, key="snow_research_map", default=None)
