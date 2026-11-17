"""Private screenshot gallery, portable exports and PDF reports.

The PostgreSQL payload contains only photo metadata. The image files live in
Supabase Storage's private bucket; no secret or public media URL reaches JS.
"""
from __future__ import annotations

import base64
import io
import json
import re
import secrets
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape

import requests
from PIL import Image as PILImage, UnidentifiedImageError

from session_store import SessionStorageError, valid_token

MAX_IMAGE_BYTES = 2_000_000
MAX_PIXELS = 3_500_000
IMAGE_ID_RE = re.compile(r"^gal_[0-9a-f]{20}$")
STORAGE_PATH_RE = re.compile(r"^[A-Za-z0-9_-]{32,90}/gal_[0-9a-f]{20}\.(?:png|jpg)$")
FORMATS = {"png": "image/png", "jpg": "image/jpeg"}


def validate_bounds(bounds: list) -> list[float]:
    if not isinstance(bounds, list) or len(bounds) != 4:
        raise SessionStorageError("Не выбрана прямоугольная область карты.")
    try:
        w, s, e, n = (float(i) for i in bounds)
    except (TypeError, ValueError) as exc:
        raise SessionStorageError("Некорректные координаты области.") from exc
    if not (-180 <= w < e <= 180 and -90 <= s < n <= 90):
        raise SessionStorageError("Неверные границы области карты.")
    return [round(v, 7) for v in (w, s, e, n)]


def read_snapshot(data_url: str, requested: str = "png") -> tuple[bytes, str]:
    """Decode and re-encode an untrusted browser screenshot with strict limits."""
    if not isinstance(data_url, str) or len(data_url) > MAX_IMAGE_BYTES * 3:
        raise SessionStorageError("Изображение слишком большое.")
    if requested not in FORMATS:
        raise SessionStorageError("Поддерживаются PNG и JPG.")
    match = re.fullmatch(r"data:image/(?:png|jpeg);base64,([A-Za-z0-9+/=]+)", data_url)
    if match is None:
        raise SessionStorageError("Некорректный формат снимка карты.")
    try:
        raw = base64.b64decode(match.group(1), validate=True)
        if len(raw) > MAX_IMAGE_BYTES:
            raise SessionStorageError("Снимок превышает 2 МБ.")
        with PILImage.open(io.BytesIO(raw)) as original:
            if original.format not in ("PNG", "JPEG"):
                raise SessionStorageError("Неизвестный формат изображения.")
            if original.width * original.height > MAX_PIXELS:
                raise SessionStorageError("Снимок слишком высокого разрешения.")
            image = original.convert("RGB" if requested == "jpg" else "RGBA")
            output = io.BytesIO()
            if requested == "jpg":
                image.save(output, format="JPEG", quality=86, optimize=True)
            else:
                image.save(output, format="PNG", optimize=True)
        data = output.getvalue()
        if len(data) > MAX_IMAGE_BYTES:
            raise SessionStorageError("Снимок превышает 2 МБ после обработки.")
        return data, requested
    except (ValueError, OSError, UnidentifiedImageError) as exc:
        raise SessionStorageError("Не удалось прочитать снимок карты.") from exc


def normalize_gallery(items: list) -> list[dict]:
    if not isinstance(items, list):
        raise SessionStorageError("Некорректный список изображений галереи.")
    if len(items) > 250:
        raise SessionStorageError("В одной галерее допускается до 250 снимков.")
    ids = set()
    out = []
    for item in items:
        if not isinstance(item, dict):
            raise SessionStorageError("Некорректная запись галереи.")
        ident = str(item.get("id") or "")
        if not IMAGE_ID_RE.fullmatch(ident) or ident in ids:
            raise SessionStorageError("Некорректный или повторяющийся ID фотографии.")
        ids.add(ident)
        fmt = str(item.get("format") or "png")
        if fmt not in FORMATS:
            raise SessionStorageError("Некорректный формат фотографии.")
        key = item.get("storage_key")
        if key is not None and (not isinstance(key, str) or not STORAGE_PATH_RE.fullmatch(key)):
            raise SessionStorageError("Некорректный путь фотографии.")
        out.append({
            "id": ident,
            "title": str(item.get("title") or "")[:150],
            "note": str(item.get("note") or "")[:3000],
            "format": fmt,
            "bounds": validate_bounds(item.get("bounds")),
            "storage_key": key,
            "captured_at": str(item.get("captured_at") or "")[:40],
        })
    return out


def new_gallery_item(bounds: list, fmt: str = "png") -> dict:
    return {
        "id": "gal_" + secrets.token_hex(10),
        "title": "",
        "note": "",
        "format": fmt,
        "bounds": validate_bounds(bounds),
        "storage_key": None,
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


class GalleryStorage:
    """Supabase Storage REST, with the secret key in the apikey header only."""

    def __init__(self, url: str, secret_key: str, bucket: str = "snow-gallery"):
        self.url = url.strip().rstrip("/")
        self.key = secret_key.strip()
        if not self.url.startswith("https://") or not self.key or not re.fullmatch(r"[a-z0-9-]+", bucket):
            raise SessionStorageError("Не удалось настроить хранилище изображений.")
        self.bucket = bucket

    def _url(self, item_key: str, private: bool = False) -> str:
        if not STORAGE_PATH_RE.fullmatch(item_key):
            raise SessionStorageError("Некорректный путь изображения.")
        prefix = "/storage/v1/object/authenticated/" if private else "/storage/v1/object/"
        return self.url + prefix + self.bucket + "/" + item_key

    def _call(self, method: str, url: str, **kwargs) -> bytes:
        headers = {"apikey": self.key}
        headers.update(kwargs.pop("headers", {}))
        try:
            response = requests.request(method, url, headers=headers, timeout=(5, 35), **kwargs)
            response.raise_for_status()
            return response.content
        except requests.RequestException as exc:
            code = getattr(exc.response, "status_code", "нет ответа")
            raise SessionStorageError(
                f"Ошибка хранения фотографии (HTTP {code}). "
                "Проверьте приватный bucket snow-gallery в Supabase."
            ) from None

    def put(self, token: str, item: dict, data: bytes) -> str:
        if not valid_token(token):
            raise SessionStorageError("Сначала сохраните исследование по ссылке.")
        fmt = item["format"]
        if fmt not in FORMATS or len(data) > MAX_IMAGE_BYTES:
            raise SessionStorageError("Снимок превышает допустимый размер.")
        key = f"{token}/{item['id']}.{fmt}"
        self._call(
            "POST", self._url(key),
            headers={"Content-Type": FORMATS[fmt], "x-upsert": "false"},
            data=data,
        )
        return key

    def get(self, storage_key: str) -> bytes:
        return self._call("GET", self._url(storage_key, private=True))

    def remove(self, storage_key: str) -> None:
        self._call("DELETE", self._url(storage_key))


def build_gallery_zip(items: list[dict], images: dict[str, bytes]) -> bytes:
    """ZIP contains only selected images, without hidden project/raster files."""
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for index, item in enumerate(items, 1):
            ident = item["id"]
            if ident not in images:
                raise SessionStorageError(f"Нет изображения {ident} для экспорта.")
            z.writestr(f"{index:03d}.{item['format']}", images[ident])
    return output.getvalue()


def _report_font() -> str:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    path = next((p for p in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/local/share/fonts/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ] if Path(p).is_file()), None)
    if path is None:
        raise SessionStorageError("Не найден шрифт для русских подписей PDF.")
    if "GalleryCyrillic" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("GalleryCyrillic", path))
    return "GalleryCyrillic"


def build_gallery_pdf(items: list[dict], images: dict[str, bytes], name: str = "Галерея") -> bytes:
    """One annotated image per landscape A4 page, preserving Cyrillic captions."""
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.utils import ImageReader
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Image as PDFImage, PageBreak, Spacer,
    )
    if not items:
        raise SessionStorageError("В галерее пока нет изображений.")
    font = _report_font()
    stream = io.BytesIO()
    page = landscape(A4)
    doc = SimpleDocTemplate(
        stream, pagesize=page, rightMargin=40, leftMargin=40,
        topMargin=33, bottomMargin=33,
        title=str(name)[:150],
    )
    title_style = ParagraphStyle(
        "gallery_title", fontName=font, fontSize=15, leading=21,
        spaceAfter=10, textColor=colors.HexColor("#222222"), alignment=TA_LEFT,
    )
    note_style = ParagraphStyle(
        "gallery_note", fontName=font, fontSize=10, leading=15,
        spaceBefore=8, spaceAfter=3,
    )
    story = []
    for index, item in enumerate(items, 1):
        raw = images.get(item["id"])
        if raw is None:
            raise SessionStorageError("Не все фотографии доступны для PDF.")
        title = str(item.get("title") or f"Изображение {index}")
        story.append(Paragraph(escape(title), title_style))
        with PILImage.open(io.BytesIO(raw)) as image:
            w, h = image.size
        maxw, maxh = page[0] - 80, page[1] - 210
        scale = min(maxw / w, maxh / h)
        story.append(PDFImage(io.BytesIO(raw), width=w * scale, height=h * scale))
        note = str(item.get("note") or "")
        if note:
            story.append(Spacer(1, 6))
            story.append(Paragraph(escape(note).replace("\n", "<br/>"), note_style))
        if index != len(items):
            story.append(PageBreak())
    doc.build(story)
    return stream.getvalue()
