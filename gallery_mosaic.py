"""Gallery masonry thumbnails and native drag/drop ordering for Streamlit."""
from __future__ import annotations

import base64
import hashlib
import io
import json
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components
from PIL import Image as PILImage

from gallery import render_annotations

COMPONENT_PATH = Path(__file__).resolve().parent / "gallery_mosaic"
_component = components.declare_component("snow_gallery_mosaic", path=str(COMPONENT_PATH))


@st.cache_data(show_spinner=False, max_entries=512)
def thumbnail_data(blob: bytes, fmt: str, annotations_json: str) -> str:
    """Small visual previews only; original full-res bytes remain in Storage."""
    annotations = json.loads(annotations_json)
    source = render_annotations(
        blob, {"format": fmt, "annotations": annotations}
    ) if annotations else blob
    with PILImage.open(io.BytesIO(source)) as original:
        rgb = original.convert("RGB")
        rgb.thumbnail((360, 640), PILImage.Resampling.LANCZOS)
        output = io.BytesIO()
        rgb.save(output, "JPEG", quality=77, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(output.getvalue()).decode("ascii")


def gallery_mosaic(photos: list[dict]) -> dict | None:
    """Interactive photo grid returning one selection or reorder action."""
    revision = hashlib.sha256(
        json.dumps([
            [image["id"], image["title"], image.get("source_date", ""),
             hashlib.sha256(image["thumbnail"].encode("ascii")).hexdigest()[:12]]
            for image in photos
        ], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:20]
    return _component(
        photos=photos, revision=revision, paste_only=False,
        key="snow_gallery_mosaic", default=None,
    )


def gallery_paste() -> dict | None:
    """Standalone clipboard drop zone, independently mounted above the grid."""
    return _component(
        photos=[], revision="paste", paste_only=True,
        key="snow_gallery_paste", default=None,
    )
