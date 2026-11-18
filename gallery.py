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
from datetime import date, datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape

import requests
from PIL import Image as PILImage, ImageDraw, ImageFont, UnidentifiedImageError

from session_store import SessionStorageError, valid_token

MAX_IMAGE_BYTES = 8_000_000
MAX_PIXELS = 10_000_000
MAX_ANNOTATIONS = 150
MAX_STROKE_POINTS = 350
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
    """Validate browser-encoded screenshot without an expensive second encoding.

    Pillow validates the file and dimension limits, while the exact original
    bytes are retained for storage; no image quality is lost on the server.
    """
    if not isinstance(data_url, str) or len(data_url) > (MAX_IMAGE_BYTES * 4 // 3 + 4096):
        raise SessionStorageError("Изображение слишком большое.")
    if requested not in FORMATS:
        raise SessionStorageError("Поддерживаются PNG и JPG.")
    match = re.fullmatch(r"data:image/(png|jpeg);base64,([A-Za-z0-9+/=]+)", data_url)
    if match is None:
        raise SessionStorageError("Некорректный формат снимка карты.")
    mime_format = "jpg" if match.group(1) == "jpeg" else "png"
    if mime_format != requested:
        raise SessionStorageError("Тип изображения не совпадает с выбранным форматом.")
    try:
        raw = base64.b64decode(match.group(2), validate=True)
        if len(raw) > MAX_IMAGE_BYTES:
            raise SessionStorageError("Снимок превышает 8 МБ.")
        with PILImage.open(io.BytesIO(raw)) as picture:
            expected_format = "JPEG" if requested == "jpg" else "PNG"
            if picture.format != expected_format:
                raise SessionStorageError("Формат файла не соответствует выбранному.")
            if picture.width * picture.height > MAX_PIXELS:
                raise SessionStorageError("Снимок слишком высокого разрешения.")
            picture.verify()
        return raw, requested
    except (ValueError, OSError, UnidentifiedImageError, SyntaxError) as exc:
        raise SessionStorageError("Не удалось проверить снимок карты.") from exc


def read_user_image(raw: bytes, filename: str = "Фото") -> tuple[bytes, str]:
    """Validate uploaded/pasted PNG/JPG without re-encoding or stripping pixels."""
    if not isinstance(raw, bytes) or len(raw) > MAX_IMAGE_BYTES:
        raise SessionStorageError("Изображение превышает 8 МБ.")
    if not raw:
        raise SessionStorageError("Пустое изображение.")
    try:
        with PILImage.open(io.BytesIO(raw)) as im:
            fmt = {"PNG": "png", "JPEG": "jpg"}.get(im.format)
            if not fmt:
                raise SessionStorageError("Поддерживаются только PNG и JPG.")
            if im.width * im.height > MAX_PIXELS:
                raise SessionStorageError("Изображение превышает 10 мегапикселей.")
            im.verify()
        return raw, fmt
    except (OSError, ValueError, SyntaxError, UnidentifiedImageError) as exc:
        raise SessionStorageError("Не удалось проверить изображение.") from exc


def normalize_annotations(annotations: list | None) -> list[dict]:
    """Validate normalized vector annotations; raw image bytes stay untouched."""
    if annotations is None:
        return []
    if not isinstance(annotations, list) or len(annotations) > MAX_ANNOTATIONS:
        raise SessionStorageError("Слишком много пометок на изображении.")
    cleaned = []
    for entry in annotations:
        if not isinstance(entry, dict) or entry.get("type") not in ("pen", "arrow", "text"):
            raise SessionStorageError("Неизвестный инструмент пометки.")
        kind = entry["type"]
        points = entry.get("points")
        if not isinstance(points, list) or not 1 <= len(points) <= MAX_STROKE_POINTS:
            raise SessionStorageError("Слишком длинная линия.")
        if kind == "arrow" and len(points) != 2:
            raise SessionStorageError("Стрелка должна иметь две точки.")
        if kind == "text" and len(points) != 1:
            raise SessionStorageError("Подпись должна иметь одну точку.")
        normalized_points = []
        for pair in points:
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                raise SessionStorageError("Некорректные координаты пометки.")
            try:
                x, y = float(pair[0]), float(pair[1])
            except (TypeError, ValueError) as exc:
                raise SessionStorageError("Некорректные координаты пометки.") from exc
            if not (0 <= x <= 1 and 0 <= y <= 1):
                raise SessionStorageError("Пометка находится за пределами изображения.")
            normalized_points.append([round(x, 5), round(y, 5)])
        color = str(entry.get("color", "#f0004b")).lower()
        if not re.fullmatch(r"#[0-9a-f]{6}", color):
            raise SessionStorageError("Неверный цвет пометки.")
        try:
            width = float(entry.get("width", 0.005))
        except (TypeError, ValueError) as exc:
            raise SessionStorageError("Некорректная толщина линии.") from exc
        if not 0.001 <= width <= 0.04:
            raise SessionStorageError("Толщина линии вне допустимого диапазона.")
        result = {
            "type": kind, "points": normalized_points,
            "color": color, "width": round(width, 5),
        }
        if kind == "text":
            label = str(entry.get("text", "")).strip()
            if not label or len(label) > 100:
                raise SessionStorageError("Подпись должна быть от 1 до 100 символов.")
            result["text"] = label
        cleaned.append(result)
    return cleaned


def _annotation_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed.ttf",
    ):
        if Path(path).is_file():
            return ImageFont.truetype(path, size=size)
    return ImageFont.load_default()


def render_annotations(image_bytes: bytes, item: dict) -> bytes:
    """Non-destructively flatten saved vector strokes for previews and exports."""
    strokes = normalize_annotations(item.get("annotations", []))
    if not strokes:
        return image_bytes
    with PILImage.open(io.BytesIO(image_bytes)) as original:
        image = original.convert("RGB" if item["format"] == "jpg" else "RGBA")
    width, height = image.size
    measure = min(width, height)
    draw = ImageDraw.Draw(image)
    from math import atan2, cos, sin, pi
    for stroke in strokes:
        points = [(round(x * width), round(y * height)) for x, y in stroke["points"]]
        color = stroke["color"]
        thick = max(1, round(measure * stroke["width"]))
        if stroke["type"] == "pen":
            if len(points) == 1:
                x, y = points[0]
                draw.ellipse((x - thick, y - thick, x + thick, y + thick), fill=color)
            else:
                draw.line(points, fill=color, width=thick, joint="curve")
        elif stroke["type"] == "arrow":
            start, end = points
            draw.line((start, end), fill=color, width=thick)
            angle = atan2(end[1] - start[1], end[0] - start[0])
            radius = max(9, thick * 5)
            wings = [
                (round(end[0] - radius * cos(angle - offset)),
                 round(end[1] - radius * sin(angle - offset)))
                for offset in (pi / 6, -pi / 6)
            ]
            draw.polygon((end, wings[0], wings[1]), fill=color)
        else:
            font = _annotation_font(max(12, thick * 5))
            draw.text(points[0], stroke["text"], fill=color, font=font, stroke_width=1)
    output = io.BytesIO()
    if item["format"] == "jpg":
        image.save(output, "JPEG", quality=95, subsampling=0, optimize=True)
    else:
        image.save(output, "PNG", optimize=True)
    if output.tell() > MAX_IMAGE_BYTES:
        raise SessionStorageError("Изображение с пометками превышает 8 МБ. Попробуйте JPG.")
    return output.getvalue()


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
            "bounds": (
                validate_bounds(item["bounds"]) if item.get("bounds") is not None
                else None
            ),
            "storage_key": key,
            "captured_at": str(item.get("captured_at") or "")[:40],
            "source_date": str(item.get("source_date") or "")[:10],
            "source_layer_id": str(item.get("source_layer_id") or "")[:80],
            "annotations": normalize_annotations(item.get("annotations", [])),
        })
    return out


def suggested_gallery_title(layers: list[dict], map_state: dict) -> str:
    """Default caption reflects the visible satellite composition, topmost first.

    Gallery screenshots can include several acquisitions; their image descriptors
    are already compact (S2 · DD.MM.YY and L8/9 · DD–DD.MM.YY · мед.).
    """
    states = (map_state or {}).get("overlays") or {}
    visible = [
        str(layer.get("name") or "Снимок")
        for layer in layers
        if states.get(layer.get("id"), {}).get("visible", True)
    ]
    if not visible:
        return "Карта · " + datetime.now(timezone.utc).strftime("%d.%m.%y")
    newest_first = visible[::-1]
    if len(newest_first) <= 2:
        return " + ".join(newest_first)[:150]
    return (newest_first[0] + f" + {len(newest_first)-1} слоя")[:150]


def photo_layer_date(item: dict) -> str:
    """Date of its source acquisition; fallback for pre-upgrade gallery items."""
    direct = str(item.get("source_date") or "")
    try:
        if direct:
            return date.fromisoformat(direct).isoformat()
    except ValueError:
        pass
    # Old images only saved the source date as part of the display title.
    match = re.search(r"(\d{1,2})\.(\d{2})\.(\d{2,4})", str(item.get("title", "")))
    if match:
        day, month, year = map(int, match.groups())
        if year < 100:
            year += 2000
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            pass
    return str(item.get("captured_at") or "")[:10]


def sorted_gallery(items: list[dict]) -> list[dict]:
    """Default gallery chronology: newest source acquisition first."""
    return sorted(items, key=lambda item: (
        photo_layer_date(item), str(item.get("captured_at") or "")
    ), reverse=True)


def new_gallery_item(bounds: list | None, fmt: str = "png") -> dict:
    return {
        "id": "gal_" + secrets.token_hex(10),
        "title": "",
        "note": "",
        "format": fmt,
        "bounds": validate_bounds(bounds) if bounds is not None else None,
        "storage_key": None,
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        "source_date": "",
        "source_layer_id": "",
        "annotations": [],
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

    def list_project_keys(self, token: str) -> list[str]:
        """List every private file under the exact session token prefix."""
        if not valid_token(token):
            raise SessionStorageError("Некорректная ссылка на исследование.")
        prefix = token + "/"
        files = []
        offset = 0
        while True:
            result = self._call(
                "POST", self.url + "/storage/v1/object/list/" + self.bucket,
                headers={"Content-Type": "application/json"},
                json={"prefix": prefix, "limit": 100, "offset": offset},
            )
            import json
            try:
                batch = json.loads(result)
            except (ValueError, TypeError) as exc:
                raise SessionStorageError("Supabase не вернул список фотографий.") from exc
            if not isinstance(batch, list):
                raise SessionStorageError("Некорректный ответ хранилища фотографий.")
            for obj in batch:
                if not isinstance(obj, dict):
                    raise SessionStorageError("Некорректный файл в хранилище.")
                filename = str(obj.get("name", ""))
                path = prefix + filename
                if not STORAGE_PATH_RE.fullmatch(path):
                    raise SessionStorageError(
                        "Обнаружен посторонний файл в каталоге проекта; удаление остановлено."
                    )
                files.append(path)
            if len(batch) < 100:
                break
            offset += len(batch)
            if offset > 5000:
                raise SessionStorageError("Слишком много фотографий в каталоге проекта.")
        return files

    def remove_project_files(self, token: str) -> int:
        """Delete *all* project images through Storage API, not SQL metadata.

        This catches orphaned uploads absent from the JSON gallery. When Storage
        raises, the caller must NOT delete the corresponding research row.
        """
        keys = self.list_project_keys(token)
        if not keys:
            return 0
        endpoint = self.url + "/storage/v1/object/" + self.bucket
        for start in range(0, len(keys), 100):
            chunk = keys[start:start + 100]
            self._call(
                "DELETE", endpoint,
                headers={"Content-Type": "application/json"},
                json={"prefixes": chunk},
            )
        if self.list_project_keys(token):
            raise SessionStorageError(
                "Не все фотографии удалены из Storage; запись проекта сохранена."
            )
        return len(keys)



def gallery_export_filename(title: str, fmt: str, used: set[str]) -> str:
    """Unicode-safe names, no traversal; deterministic collision suffixes."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", str(title or "Снимок"))
    name = re.sub(r"\s+", " ", name).strip(" ._")
    if name.upper().split(".")[0] in {
        "CON", "PRN", "AUX", "NUL", *{f"COM{i}" for i in range(1, 10)},
        *{f"LPT{i}" for i in range(1, 10)},
    }:
        name = "_" + name
    name = name[:110].rstrip(" .") or "Снимок"
    candidate = f"{name}.{fmt}"
    counter = 2
    while candidate.casefold() in used:
        suffix = f" ({counter})"
        candidate = f"{name[:110-len(suffix)]}{suffix}.{fmt}"
        counter += 1
    used.add(candidate.casefold())
    return candidate


def build_gallery_zip(items: list[dict], images: dict[str, bytes]) -> bytes:
    """ZIP filenames match gallery captions instead of numeric placeholders."""
    output = io.BytesIO()
    used: set[str] = set()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for item in items:
            ident = item["id"]
            if ident not in images:
                raise SessionStorageError(f"Нет изображения {ident} для экспорта.")
            filename = gallery_export_filename(item.get("title", ""), item["format"], used)
            z.writestr(filename, images[ident])
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


def build_gallery_pdf(
    items: list[dict], images: dict[str, bytes], name: str = "Галерея",
    project_notes: str = "",
) -> bytes:
    """Write project notes FIRST, then one annotated picture per landscape A4 page."""
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
    if project_notes.strip():
        story.append(Paragraph(escape(str(name)[:150]), title_style))
        story.append(Paragraph("Заметки об исследовании", title_style))
        for part in project_notes.strip().split("\n\n"):
            story.append(Paragraph(
                escape(part).replace("\n", "<br/>"), note_style
            ))
        story.append(PageBreak())
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
