"""Native Streamlit component for editing vector annotations on gallery images."""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import streamlit.components.v1 as components

_frontend = Path(__file__).resolve().parent / "gallery_editor"
_gallery_editor = components.declare_component(
    "snow_gallery_annotations", path=str(_frontend)
)


def edit_gallery_image(image: bytes, fmt: str, image_id: str, annotations: list) -> dict | None:
    mime = "jpeg" if fmt == "jpg" else "png"
    data_url = "data:image/" + mime + ";base64," + base64.b64encode(image).decode("ascii")
    revision = hashlib.sha256(
        json.dumps(annotations, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]
    return _gallery_editor(
        image_url=data_url, image_id=image_id, annotations=annotations,
        revision=revision, key="gallery_annotations_" + image_id, default=None,
    )
