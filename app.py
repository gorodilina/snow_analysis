"""Snow analysis UI: satellite scenes, median mosaics and shareable research sessions."""
from __future__ import annotations

import hashlib
import json
import os
import io
from time import perf_counter
from datetime import date, timedelta
from pathlib import Path

import streamlit as st
from PIL import Image as PILImage

from gee_client import (
    LANDSAT, SENTINEL, initialize_ee, list_month_scenes,
    count_interval_scenes, image_rgb_tile, analysis_overlay_tiles,
)
from geo_utils import GeoInputError, read_geo, search_geometry
from map_builder import map_config
from map_component import research_map
from project_layers import (
    migrate_legacy_selection, new_median_layer, new_scene_layer, normalize_layers,
    contour_flags,
)
from gallery import (
    GalleryStorage, build_gallery_zip, build_gallery_pdf, new_gallery_item,
    normalize_gallery, normalize_annotations, read_snapshot, read_user_image,
    render_annotations, suggested_gallery_title, photo_layer_date, sorted_gallery,
    validate_bounds,
)
from gallery_mosaic import gallery_mosaic, gallery_paste, thumbnail_data
from gallery_clipboard import record_paste_once
from gallery_uploads import submit_gallery_upload
from gallery_editor import edit_gallery_image
from geo_export import export_geotiff, suggested_scale, geotiff_filename
from project_delete import delete_project
from session_store import SessionStorageError, SessionStore, valid_token

ROOT = Path(__file__).resolve().parent
MONTHS = {
    1: "Январь", 2: "Февраль", 3: "Март", 4: "Апрель",
    5: "Май", 6: "Июнь", 7: "Июль", 8: "Август",
    9: "Сентябрь", 10: "Октябрь", 11: "Ноябрь", 12: "Декабрь",
}
st.set_page_config(page_title="Анализ снежной обстановки", page_icon="🏔️", layout="wide")
st.markdown("""<style>
.block-container{padding-top:1.3rem;padding-bottom:1rem;max-width:none}
[data-testid="stSidebar"]{min-width:335px}
.median-heading{display:flex;align-items:center;justify-content:space-between;margin:0 0 .5rem;font-size:1rem}
.median-help-icon{display:inline-flex;width:20px;height:20px;align-items:center;justify-content:center;
 border:1px solid #7b8794;border-radius:50%;color:#64748b;font:600 13px/1 sans-serif;
 position:relative;cursor:help;outline-offset:3px}
.median-help-content{display:none;position:absolute;right:0;top:26px;width:295px;max-width:calc(100vw - 45px);
 padding:12px 14px;border:1px solid #d2d9e2;border-radius:9px;background:#fff;color:#273442;
 text-align:left;font:13px/1.5 sans-serif;font-weight:400;box-shadow:0 5px 18px rgba(28,37,50,.18);
 white-space:normal;z-index:9999}
.median-help-icon:hover .median-help-content,.median-help-icon:focus .median-help-content,
.median-help-icon:focus-within .median-help-content{display:block}
</style>""", unsafe_allow_html=True)


def setting(name: str, default=None):
    try:
        if name in st.secrets:
            return st.secrets[name]
    except (FileNotFoundError, KeyError):
        pass
    return os.environ.get(name, default)


@st.cache_resource(show_spinner=False)
def connect_ee(project: str, service_json: str | None):
    credentials = json.loads(service_json) if service_json else None
    initialize_ee(project, credentials)
    return True


@st.cache_data(ttl=3600, show_spinner=False)
def cached_scenes(satellite: str, bbox: tuple[float, ...], year: int, month: int):
    return list_month_scenes(satellite, list(bbox), year, month)


@st.cache_data(ttl=900, show_spinner=False)
def cached_interval_count(satellite: str, bbox: tuple[float, ...], start: date, days: int):
    return count_interval_scenes(satellite, list(bbox), start, days)


def parse_upload(filename: str, data: bytes):
    try:
        return read_geo(filename, data)
    except GeoInputError as exc:
        st.error(f"Ошибка файла «{filename}»: {exc}")
        return [], []


def apply_research_payload(payload: dict):
    """Hydrate Streamlit widgets before they are instantiated."""
    st.session_state["research_name"] = payload.get("name") or "Новое исследование"
    st.session_state["saved_routes"] = payload.get("routes") or []
    st.session_state["saved_points"] = payload.get("points") or []
    previous_notes = payload.get("notes") or []
    if isinstance(previous_notes, list):
        st.session_state["research_note"] = "\n\n".join(
            str(note.get("text") or "") for note in previous_notes
            if isinstance(note, dict) and note.get("text")
        )
    else:
        st.session_state["research_note"] = str(previous_notes)
    st.session_state["gallery_custom_order"] = bool(payload.get("gallery_custom_order", False))
    loaded_gallery = normalize_gallery(payload.get("gallery") or [])
    st.session_state["gallery"] = (
        loaded_gallery if st.session_state["gallery_custom_order"]
        else sorted_gallery(loaded_gallery)
    )
    st.session_state["gallery_blobs"] = payload.get("_gallery_media") or {}
    st.session_state["gallery_export_bytes"] = None
    st.session_state["gallery_upload_jobs"] = {}
    st.session_state["gallery_upload_errors"] = {}
    st.session_state["gallery_upload_timings"] = {}
    st.session_state["pending_photo"] = None
    st.session_state["gallery_active_id"] = None
    st.session_state["gallery_page"] = 1
    st.session_state["geotiff_bytes"] = None
    st.session_state["gallery_uploader_revision"] = st.session_state.get("gallery_uploader_revision", 0) + 1
    st.session_state["manual_points"] = []
    # Rotate the uploader key instead of writing into a non-settable file widget.
    st.session_state["geo_uploader_revision"] = st.session_state.get("geo_uploader_revision", 0) + 1
    st.session_state["map_state"] = payload.get("map_state") or {}
    # Migrate sessions created before multiple independent imagery layers.
    existing_layers = payload.get("layers")
    use_legacy = not existing_layers and isinstance(payload.get("selection"), dict)
    st.session_state["image_layers"] = normalize_layers(
        migrate_legacy_selection(payload["selection"]) if use_legacy
        else (existing_layers if isinstance(existing_layers, list) else [])
    )
    if use_legacy and st.session_state["image_layers"]:
        old_overlays = st.session_state["map_state"].get("overlays", {})
        old_rgb = old_overlays.get("Снимок без маски") or old_overlays.get("Спутниковый снимок")
        if isinstance(old_rgb, dict):
            old_overlays[st.session_state["image_layers"][0]["id"]] = old_rgb
    st.session_state["runtime_tiles"] = {}
    st.session_state["analysis_tiles"] = {}
    st.session_state["image_caption"] = ""
    st.session_state["warnings"] = []
    ui = payload.get("ui") or {}
    for key in ["satellite", "mode", "year", "month", "median_length"]:
        if key in ui:
            st.session_state[key] = ui[key]
    if ui.get("median_start"):
        st.session_state["median_start"] = date.fromisoformat(ui["median_start"])


def research_payload(routes: list[dict], points: list[dict]) -> dict:
    ui = {}
    for key in ["satellite", "mode", "year", "month", "median_length"]:
        if key in st.session_state:
            ui[key] = st.session_state[key]
    if "median_start" in st.session_state:
        ui["median_start"] = st.session_state["median_start"].isoformat()
    return {
        "schema": 1,
        "name": st.session_state.get("research_name", "Новое исследование"),
        "routes": routes,
        "points": points,
        "notes": (
            [{"id": "research", "text": st.session_state.get("research_note", ""), "created": ""}]
            if st.session_state.get("research_note", "").strip() else []
        ),
        "gallery": normalize_gallery(st.session_state.get("gallery", [])),
        "gallery_custom_order": bool(st.session_state.get("gallery_custom_order", False)),
        "ui": ui,
        "layers": normalize_layers(st.session_state.get("image_layers", [])),
        "map_state": st.session_state.get("map_state", {}),
    }


def share_link(token: str) -> str:
    try:
        url = st.context.url
    except (AttributeError, RuntimeError):
        url = setting("APP_PUBLIC_URL", "")
    return (url.rstrip("/") + "?session=" + token) if url else "?session=" + token


# Persistent sessions: configured with server-side Supabase secrets.
store, gallery_store = None, None
if setting("SUPABASE_URL", "") and setting("SUPABASE_SECRET_KEY", ""):
    try:
        store = SessionStore(setting("SUPABASE_URL"), setting("SUPABASE_SECRET_KEY"))
        gallery_store = GalleryStorage(setting("SUPABASE_URL"), setting("SUPABASE_SECRET_KEY"))
    except SessionStorageError as exc:
        st.warning(f"Хранилище исследований: {exc}")

if st.session_state.pop("reset_after_delete", False):
    # Rehydrate only at the top of a new run, before widgets are instantiated.
    apply_research_payload({})
    st.session_state["loaded_map_revision"] = "deleted-session"
    st.session_state.pop("delete_project_phrase", None)

requested_token = st.query_params.get("session", "")
if requested_token and requested_token != st.session_state.get("active_token"):
    if not valid_token(requested_token):
        st.error("Некорректная ссылка на исследование.")
    elif not store:
        st.warning("Чтобы открыть сохранённую ссылку, подключите Supabase к приложению.")
    else:
        try:
            saved = store.get(requested_token)
            if saved is None:
                st.error("Исследование по этой ссылке не найдено.")
            else:
                apply_research_payload(saved)
                st.session_state["active_token"] = requested_token
                st.session_state["last_saved_digest"] = hashlib.sha256(
                    json.dumps(saved, ensure_ascii=False, sort_keys=True).encode()
                ).hexdigest()
                st.session_state["loaded_map_revision"] = requested_token
        except (SessionStorageError, ValueError, TypeError) as exc:
            st.error(f"Не удалось открыть исследование: {exc}")

st.session_state.setdefault("research_name", "Новое исследование")
# Saved sessions may still use the old label; keep their selection valid.
if st.session_state.get("mode") == "Медианная мозаика":
    st.session_state["mode"] = "Медиана"
st.session_state.setdefault("manual_points", [])
st.session_state.setdefault("research_note", "")
st.session_state.setdefault("gallery_custom_order", False)
st.session_state.setdefault("gallery", [])
st.session_state.setdefault("gallery_blobs", {})
st.session_state.setdefault("gallery_export_bytes", None)
st.session_state.setdefault("gallery_upload_jobs", {})
st.session_state.setdefault("gallery_upload_errors", {})
st.session_state.setdefault("gallery_upload_timings", {})
st.session_state.setdefault("pending_photo", None)
st.session_state.setdefault("gallery_active_id", None)
st.session_state.setdefault("gallery_page", 1)
st.session_state.setdefault("gallery_uploader_revision", 0)
st.session_state.setdefault("processed_paste_events", [])
st.session_state.setdefault("geotiff_bytes", None)
st.session_state.setdefault("map_state", {})
st.session_state.setdefault("image_layers", [])
st.session_state.setdefault("runtime_tiles", {})
st.session_state.setdefault("analysis_tiles", {})
st.session_state.setdefault("warnings", [])

def gallery_image_bytes(item: dict) -> bytes:
    ident = item["id"]
    local = st.session_state["gallery_blobs"].get(ident)
    if local is not None:
        return local
    if not item.get("storage_key") or not gallery_store:
        raise SessionStorageError("Изображение не найдено в галерее. Проверьте облачное сохранение.")
    data = gallery_store.get(item["storage_key"])
    st.session_state["gallery_blobs"][ident] = data
    return data


def collect_gallery_images(composite: bool = False) -> dict[str, bytes]:
    return {
        item["id"]: (
            render_annotations(gallery_image_bytes(item), item)
            if composite else gallery_image_bytes(item)
        )
        for item in st.session_state["gallery"]
    }


def sync_gallery_to_cloud(token: str) -> None:
    """Upload draft images before saving metadata to the shareable session."""
    if not gallery_store:
        raise SessionStorageError("Не подключено хранилище галереи.")
    for item in st.session_state["gallery"]:
        if item.get("storage_key"):
            continue
        blob = st.session_state["gallery_blobs"].get(item["id"])
        if blob is None:
            raise SessionStorageError("Не найден локальный файл галереи.")
        item["storage_key"] = gallery_store.put(token, item, blob)


@st.cache_data(show_spinner=False, max_entries=96)
def quick_photo_preview(photo: bytes) -> bytes:
    """Send a small preview to Streamlit; keep original full-res data elsewhere."""
    with PILImage.open(io.BytesIO(photo)) as image:
        image.thumbnail((900, 700), PILImage.Resampling.LANCZOS)
        preview = image.convert("RGB")
        out = io.BytesIO()
        preview.save(out, format="JPEG", quality=79)
    return out.getvalue()


def queue_gallery_uploads(token: str) -> None:
    """Schedule missing photos; do not block the Streamlit render thread."""
    if not gallery_store or not token:
        return
    jobs = st.session_state["gallery_upload_jobs"]
    errors = st.session_state["gallery_upload_errors"]
    for item in st.session_state["gallery"]:
        ident = item["id"]
        if item.get("storage_key") or ident in jobs:
            continue
        blob = st.session_state["gallery_blobs"].get(ident)
        if blob is None:
            errors[ident] = "Исходное изображение не найдено в памяти."
            continue
        try:
            jobs[ident] = submit_gallery_upload(gallery_store, token, item, blob)
            errors.pop(ident, None)
        except RuntimeError as exc:
            errors[ident] = "Не удалось запустить загрузку: " + str(exc)


def add_personal_image(raw: bytes, filename: str = "Изображение") -> dict:
    """Add an uploaded or pasted photo (without claiming satellite georeferencing)."""
    if len(st.session_state["gallery"]) >= 250:
        raise SessionStorageError("В галерее достигнут предел 250 изображений.")
    data, fmt = read_user_image(raw, filename)
    item = new_gallery_item(None, fmt)
    label = Path(filename or "Изображение").stem.strip()
    item["title"] = (label or "Загруженное изображение")[:150]
    st.session_state["gallery_blobs"][item["id"]] = data
    st.session_state["gallery"].append(item)
    if not st.session_state.get("gallery_custom_order", False):
        st.session_state["gallery"] = sorted_gallery(st.session_state["gallery"])
    st.session_state["gallery_active_id"] = item["id"]
    st.session_state["gallery_export_bytes"] = None
    token = st.session_state.get("active_token")
    if token:
        queue_gallery_uploads(token)
    return item


def render_pending_photo_form() -> None:
    """Stage the screenshot first; let the user rename/comment BEFORE upload."""
    pending = st.session_state.get("pending_photo")
    if not pending:
        return
    item = pending["item"]
    with st.container(border=True):
        st.subheader("Новый снимок карты — добавление в галерею")
        st.caption("Название уже заполнено по спутниковому слою. Можно изменить его и добавить заметку.")
        st.image(quick_photo_preview(pending["data"]), width=380)
        timing = st.session_state.get("capture_diagnostics", {})
        if timing:
            st.caption(
                f"Размер: {timing.get('bytes', 0)/1024/1024:.1f} МБ · "
                f"захват в браузере: {timing.get('browser_ms', 0)/1000:.1f} с · "
                f"проверка файла: {timing.get('validation_ms', 0)} мс"
            )
        with st.form("new_gallery_photo_" + item["id"]):
            title = st.text_input("Название", value=item["title"], max_chars=150)
            note = st.text_area("Заметка (необязательно)", value="", max_chars=3000, height=90)
            save_photo = st.form_submit_button(
                "Сохранить изображение в галерею", type="primary",
                use_container_width=True,
            )
        if save_photo:
            item["title"] = title.strip() or item["title"]
            item["note"] = note.strip()
            token = st.session_state.get("active_token")
            # Append immediately; a network upload must never block the UI.
            st.session_state["gallery_blobs"][item["id"]] = pending["data"]
            st.session_state["gallery"].append(item)
            if token:
                queue_gallery_uploads(token)
            if not st.session_state.get("gallery_custom_order", False):
                st.session_state["gallery"] = sorted_gallery(st.session_state["gallery"])
            st.session_state["gallery_active_id"] = item["id"]
            st.session_state["pending_photo"] = None
            st.session_state["gallery_export_bytes"] = None
            st.session_state["project_backup_bytes"] = None
            st.toast(
                "Фото добавлено в галерею. " +
                ("Загрузка в облако продолжается…" if token
                 else "Для постоянной ссылки сохраните исследование.")
            )
            st.rerun()
        if st.button("Отменить этот снимок", key="cancel_pending_photo"):
            st.session_state["pending_photo"] = None
            st.rerun()


def render_gallery_export(items: list[dict]) -> None:
    """Keep ZIP/PDF export prominently visible at the top of Gallery."""
    with st.container(border=True):
        st.markdown("#### Выгрузка галереи")
        st.caption(
            "ZIP — все фотографии в заданном порядке, с их названиями. "
            "PDF — сначала общие заметки, затем фотографии с подписями."
        )
        export_mode = st.radio(
            "Формат выгрузки",
            ["ZIP — фотографии", "PDF — фотографии и заметки"],
            key="gallery_export_mode",
            horizontal=True,
        )
        is_zip = export_mode.startswith("ZIP")
        if st.button("Подготовить " + ("ZIP" if is_zip else "PDF"),
                     key="export_gallery_prepare", use_container_width=True,
                     disabled=not items):
            try:
                with st.spinner("Готовлю архив галереи..." if is_zip else "Готовлю PDF..."):
                    pictures = collect_gallery_images(composite=True)
                    if is_zip:
                        output = build_gallery_zip(items, pictures)
                        ext, mime = "zip", "application/zip"
                    else:
                        output = build_gallery_pdf(
                            items, pictures,
                            st.session_state["research_name"],
                            project_notes=st.session_state.get("research_note", ""),
                        )
                        ext, mime = "pdf", "application/pdf"
                    st.session_state["gallery_export_bytes"] = (ext, mime, output)
            except (SessionStorageError, OSError, ValueError) as exc:
                st.error(f"Не удалось подготовить выгрузку: {exc}")
        prepared = st.session_state.get("gallery_export_bytes")
        if prepared and (prepared[0] == "zip") == is_zip:
            st.download_button(
                "Скачать галерею ." + prepared[0].upper(),
                data=prepared[2],
                file_name="snow-gallery." + prepared[0],
                mime=prepared[1],
                use_container_width=True,
            )


@st.dialog("Просмотр и редактирование изображения", width="large")
def gallery_edit_dialog(image_id: str) -> None:
    """A real modal opened by clicking any masonry card (not below the grid)."""
    items = st.session_state["gallery"]
    active = next((item for item in items if item["id"] == image_id), None)
    if active is None:
        st.warning("Изображение уже удалено.")
        return
    with st.container(border=True):
        st.markdown("#### Выбранное изображение")
        try:
            st.image(render_annotations(gallery_image_bytes(active), active), use_container_width=True)
        except SessionStorageError as exc:
            st.warning(str(exc))
        if st.toggle(
            "Рисовать поверх фотографии (линии, стрелки, подписи)",
            key="gallery_draw_" + active["id"],
        ):
            st.caption(
                "Рисунки сохраняются отдельно от оригинала. "
                "Можно отменить действие, очистить пометки или изменить их позже."
            )
            try:
                editor_result = edit_gallery_image(
                    gallery_image_bytes(active), active["format"],
                    active["id"], active.get("annotations", []),
                )
                if (
                    isinstance(editor_result, dict)
                    and editor_result.get("action") == "save_annotations"
                    and editor_result.get("item_id") == active["id"]
                    and editor_result.get("event_id")
                        != st.session_state.get("processed_annotation_event")
                ):
                    updated = normalize_annotations(editor_result.get("annotations"))
                    st.session_state["processed_annotation_event"] = editor_result["event_id"]
                    active["annotations"] = updated
                    st.session_state["gallery_export_bytes"] = None
                    st.session_state["project_backup_bytes"] = None
                    st.rerun()
            except (SessionStorageError, ValueError) as exc:
                st.error(f"Не удалось сохранить пометки: {exc}")
        with st.form("edit_gallery_" + active["id"]):
            title = st.text_input(
                "Название", value=active.get("title") or "", max_chars=150
            )
            note = st.text_area(
                "Заметка", value=active.get("note") or "",
                max_chars=3000, height=100,
            )
            saved = st.form_submit_button("Сохранить подпись", use_container_width=True)
        if saved:
            active["title"], active["note"] = title.strip(), note.strip()
            st.session_state["gallery_export_bytes"] = None
            st.session_state["project_backup_bytes"] = None
            st.rerun()
        if st.button("Удалить изображение", key="delete_gallery_" + active["id"]):
            try:
                if active.get("storage_key") and gallery_store:
                    gallery_store.remove(active["storage_key"])
                st.session_state["gallery"] = [
                    item for item in items if item["id"] != active["id"]
                ]
                st.session_state["gallery_blobs"].pop(active["id"], None)
                st.session_state["gallery_active_id"] = None
                st.session_state["gallery_export_bytes"] = None
                st.session_state["project_backup_bytes"] = None
                st.rerun()
            except SessionStorageError as exc:
                st.error(f"Не удалось удалить изображение: {exc}")




def accept_gallery_paste(event: dict | None) -> bool:
    """Handle a single clipboard upload from the gallery iframe."""
    if not isinstance(event, dict) or event.get("action") != "paste_image":
        return False
    # A component retains its last value after subsequent Streamlit reruns.
    # Gallery actions must not re-enable an earlier clipboard paste.
    if not record_paste_once(event, st.session_state["processed_paste_events"]):
        return False
    try:
        data_url = event.get("image_data", "")
        fmt = "jpg" if data_url.startswith("data:image/jpeg;") else "png"
        raw, _ = read_snapshot(data_url, fmt)
        item = add_personal_image(
            raw, event.get("filename") or "Изображение из буфера.png"
        )
        st.toast(f"Добавлено в галерею: {item['title']}")
        st.rerun()
    except (SessionStorageError, ValueError, TypeError) as exc:
        st.error(f"Не удалось вставить изображение: {exc}")
    return True


def render_gallery_tab() -> None:
    """Independent, paged thumbnail gallery with explicit per-photo editor."""
    items = st.session_state["gallery"]
    st.subheader("Галерея")
    st.caption(
        f"Сохранено изображений: {len(items)}. "
        "Выберите фотографию, чтобы отредактировать название и заметку."
    )
    if st.session_state.get("gallery_upload_jobs"):
        st.caption("⏳ Фотографии загружаются в Supabase. До подтверждения не закрывайте вкладку.")
    if st.session_state.get("gallery_upload_errors"):
        st.warning("Не все фотографии сохранены в облаке. Повторите загрузку через раздел сохранения.")
    # Keep upload and export controls side by side; the mosaic remains full-width.
    upload_col, export_col = st.columns(2, gap="medium")
    with upload_col:
        with st.container(border=True):
            st.markdown("#### Добавить свои изображения")
            user_files = st.file_uploader(
                "Выбрать PNG/JPG",
                type=["png", "jpg", "jpeg"],
                accept_multiple_files=True,
                key=f"user_gallery_files_{st.session_state['gallery_uploader_revision']}",
            )
            if user_files:
                imported = 0
                for uploaded in user_files:
                    try:
                        add_personal_image(uploaded.getvalue(), uploaded.name)
                        imported += 1
                    except SessionStorageError as exc:
                        st.error(f"{uploaded.name}: {exc}")
                # Rotate widget ID so uploads are not repeated on every rerun.
                st.session_state["gallery_uploader_revision"] += 1
                if imported:
                    st.toast(f"Добавлено изображений: {imported}")
                st.rerun()
            # Dedicated clipboard zone is placed immediately below the uploader.
            accept_gallery_paste(gallery_paste())
    with export_col:
        render_gallery_export(items)

    if not items:
        st.info("Галерея пока пуста. Выберите PNG/JPG, вставьте картинку "
                "из буфера или сохраните область карты.")
        return

    ids = {item["id"] for item in items}
    if st.session_state.get("gallery_active_id") not in ids:
        st.session_state["gallery_active_id"] = items[-1]["id"]

    cards = []
    for item in items:
        try:
            source = gallery_image_bytes(item)
            annotations = json.dumps(item.get("annotations", []), sort_keys=True)
            thumbnail = thumbnail_data(source, item["format"], annotations)
        except (SessionStorageError, OSError, ValueError) as exc:
            st.warning(f"Миниатюра «{item.get('title', '')}» недоступна: {exc}")
            thumbnail = ""
        cards.append({
            "id": item["id"],
            "title": item.get("title", ""),
            "source_date": photo_layer_date(item),
            "thumbnail": thumbnail,
        })
    mosaic_event = gallery_mosaic(cards)
    if isinstance(mosaic_event, dict) and (
        mosaic_event.get("event_id") != st.session_state.get("processed_mosaic_event")
    ):
        action = mosaic_event.get("action")
        if action == "select" and mosaic_event.get("image_id") in ids:
            st.session_state["processed_mosaic_event"] = mosaic_event["event_id"]
            st.session_state["gallery_active_id"] = mosaic_event["image_id"]
            gallery_edit_dialog(mosaic_event["image_id"])
        elif action == "reorder":
            new_ids = mosaic_event.get("ids") or []
            if (
                isinstance(new_ids, list) and len(new_ids) == len(items)
                and set(new_ids) == ids
            ):
                st.session_state["processed_mosaic_event"] = mosaic_event["event_id"]
                by_id = {item["id"]: item for item in items}
                st.session_state["gallery"] = [by_id[ident] for ident in new_ids]
                st.session_state["gallery_custom_order"] = True
                st.session_state["gallery_export_bytes"] = None
                st.rerun()
            else:
                st.warning("Некорректный порядок галереи. Изменения не применены.")
    if st.session_state.get("gallery_custom_order"):
        if st.button("Вернуть порядок по дате снимков", use_container_width=False):
            st.session_state["gallery_custom_order"] = False
            st.session_state["gallery"] = sorted_gallery(items)
            st.session_state["gallery_export_bytes"] = None
            st.rerun()



@st.dialog("Возможности приложения", width="large")
def show_app_capabilities() -> None:
    st.markdown(
        """
**Маршрут и точки.** Загрузите KML/GPX-треки, CSV с точками или добавьте точки вручную. Управляйте отображением треков и подписей на карте.

**Спутниковые снимки.** Просматривайте отдельные сцены Sentinel-2 и Landsat 8/9 или создавайте медиану за выбранный период. Видимость и прозрачность слоёв настраиваются. Выбирайте параметры так, чтобы из-под снимка проглядывала карта-подложка. Удобнее всего использовать OpenTopoMap или «Рельеф (Esri)».

**Снег и температура.** Для снимков доступны граница снега по NDSI и изотермы −5, 0, +5, +10 °C на основе ERA5-Land.

**Галерея.** Сохраняйте фрагменты карты, добавляйте PNG/JPG с устройства или через Ctrl+V, меняйте порядок изображений, подписи и рисуйте поверх снимков.

**Выгрузка результатов.** Экспортируйте галерею в ZIP или в PDF вместе с заметками. Для выбранной области спутникового слоя доступен геопривязанный GeoTIFF.

**Сохранение проекта.** Исследование можно сохранить по ссылке; при подключённом хранилище последующие изменения сохраняются автоматически. Проект можно удалить целиком.

*Важно:* снежная граница по NDSI может включать ледники, а изотермы ERA5-Land являются модельной оценкой, а не измерениями на склоне.
"""
    )


with st.sidebar:
    st.title("🏔️ Анализ снежной обстановки")
    if st.button("ⓘ Возможности приложения", use_container_width=True):
        show_app_capabilities()
    st.caption("Исправления и пожелания: [@GorodVera](https://t.me/GorodVera)")
    st.subheader("1. Трек и точки")
    uploads = st.file_uploader(
        "Загрузить файлы KML или GPX",
        type=["kml", "gpx", "csv"],
        accept_multiple_files=True,
        key=f"geo_files_{st.session_state.get('geo_uploader_revision', 0)}",
    )

    with st.expander("Добавить точку вручную"):
        with st.form("point_form", clear_on_submit=False):
            point_name = st.text_input("Название", value="Новая точка")
            col_lat, col_lon = st.columns(2)
            point_lat = col_lat.number_input(
                "Широта", min_value=-90., max_value=90., value=42.35, format="%.6f"
            )
            point_lon = col_lon.number_input(
                "Долгота", min_value=-180., max_value=180., value=46.25, format="%.6f"
            )
            if st.form_submit_button("Добавить точку", use_container_width=True):
                st.session_state["manual_points"].append({
                    "name": point_name.strip() or "Точка",
                    "coordinates": [point_lon, point_lat],
                })
        if st.button("Очистить добавленные вручную точки", use_container_width=True):
            st.session_state["manual_points"] = []

    st.divider()
    st.subheader("2. Спутниковые снимки")
    mode = st.radio("Режим", ["Конкретный снимок", "Медиана"], key="mode")
    if mode == "Медиана":
        satellite = st.selectbox("Источник", [SENTINEL, LANDSAT], key="satellite")
    else:
        satellite = None  # Both satellites are searched simultaneously.

demo_routes, _ = read_geo(
    "default_route.kml", (ROOT / "data" / "default_route.kml").read_bytes()
)
_, demo_points = read_geo(
    "default_points.kml", (ROOT / "data" / "default_points.kml").read_bytes()
)

uploaded_routes, uploaded_points = [], []
for uploaded_file in uploads or []:
    lines, markers = parse_upload(uploaded_file.name, uploaded_file.getvalue())
    uploaded_routes.extend(lines)
    uploaded_points.extend(markers)

saved_routes = st.session_state.get("saved_routes")
saved_points = st.session_state.get("saved_points")
if uploaded_routes:
    # Uploaded tracks REPLACE the demo (and its 28 demo obstacles).
    routes = uploaded_routes
    points = uploaded_points + st.session_state["manual_points"]
    route_status = f"Загружено треков: {len(routes)}"
elif saved_routes is not None:
    routes = saved_routes
    points = (saved_points or []) + uploaded_points + st.session_state["manual_points"]
    route_status = "Сохранённый трек"
else:
    routes = demo_routes
    points = demo_points + uploaded_points + st.session_state["manual_points"]
    route_status = "Пример трека из проекта"

geo_hash = hashlib.sha256(
    json.dumps([routes, points], ensure_ascii=False, sort_keys=True).encode()
).hexdigest()
if st.session_state.get("last_geo_hash") != geo_hash:
    st.session_state["last_geo_hash"] = geo_hash
    st.session_state.pop("scenes", None)
    st.session_state.pop("scene_query", None)
    # Keep the layer catalogue; refresh its temporary tiles for a new ROI.
    st.session_state["runtime_tiles"] = {}
    st.session_state["analysis_tiles"] = {}

bbox = search_geometry(routes, points if not routes else [], buffer_m=4500)["bbox"]

project = setting("GEE_PROJECT", "")
service = setting("GEE_SERVICE_ACCOUNT")
if service is None:
    try:
        if "gee_service_account" in st.secrets:
            service = dict(st.secrets["gee_service_account"])
    except (FileNotFoundError, KeyError):
        pass
if isinstance(service, str):
    try:
        service = json.loads(service)
    except json.JSONDecodeError:
        service = None

connected = False
connection_error = None
try:
    connected = connect_ee(project, json.dumps(service, sort_keys=True) if service else None)
except Exception as exc:
    connection_error = str(exc)

def add_image_layer(spec: dict, region: list[float]) -> None:
    """Append one independent RGB layer; imagery never contains cloud masking."""
    tile = image_rgb_tile(spec, region)
    st.session_state["image_layers"].append(spec)
    st.session_state["runtime_tiles"][spec["id"]] = tile



def current_tile_layers(region: list[float], is_connected: bool) -> list[dict]:
    """Load only the individually enabled outlines and isotherms per scene."""
    if not is_connected:
        return []
    catalogue = st.session_state["image_layers"]
    runtime = st.session_state["runtime_tiles"]
    cache = st.session_state["analysis_tiles"]
    active = contour_flags(st.session_state.get("map_state", {}))
    tiles = []
    for spec in catalogue:
        ident = spec["id"]
        if ident not in runtime:
            try:
                runtime[ident] = image_rgb_tile(spec, region)
            except Exception as exc:
                st.warning(f"Снимок «{spec['name']}» пока недоступен: {exc}")
                continue
        tiles.append(runtime[ident])
        if not any(active.values()):
            continue
        if not isinstance(cache.get(ident), dict):
            cache[ident] = {}
        available = cache[ident]
        missing = [key for key, visible in active.items()
                   if visible and key not in available]
        if missing:
            try:
                overlays, warnings = analysis_overlay_tiles(
                    spec, region,
                    include_snow="snow" in missing,
                    iso_levels=[int(k.split("_")[1]) for k in missing if k.startswith("iso_")],
                )
                for layer in overlays:
                    available[layer["analysis_key"]] = layer
                for warning in warnings:
                    st.warning(f"{spec['name']}: {warning}")
            except Exception as exc:
                st.warning(f"Контуры «{spec['name']}» временно недоступны: {exc}")
        tiles.extend(available[k] for k in active if active[k] and k in available)
    valid_ids = {spec["id"] for spec in catalogue}
    for key in list(runtime):
        if key not in valid_ids:
            del runtime[key]
    for key in list(cache):
        if key not in valid_ids:
            del cache[key]
    return tiles


st.title("Анализ снежной обстановки")
st.caption("Снег — по NDSI; изотермы — модельная оценка ERA5-Land.")

with st.sidebar:
    if not connected:
        st.warning("Earth Engine недоступен. Трек отображается, но снимки пока не загрузить.")
    if mode == "Конкретный снимок":
        st.markdown("**3. Найти реальную дату съёмки**")
        year_col, month_col = st.columns(2)
        year = year_col.selectbox(
            "Год", list(range(date.today().year, 2016, -1)), key="year"
        )
        month = month_col.selectbox(
            "Месяц", list(MONTHS), format_func=lambda m: MONTHS[m], key="month"
        )
        query = (tuple(round(v, 6) for v in bbox), year, month)
        if st.button("Найти доступные даты", disabled=not connected, use_container_width=True):
            with st.spinner("Ищу съёмки Sentinel-2 и Landsat 8/9..."):
                all_scenes, errors = [], []
                for source in [SENTINEL, LANDSAT]:
                    try:
                        all_scenes.extend(
                            {**scene, "satellite": source}
                            for scene in cached_scenes(source, *query)
                        )
                    except Exception as exc:
                        errors.append(f"{source}: {exc}")
                if errors:
                    st.warning("Не удалось получить часть данных: " + "; ".join(errors))
                if all_scenes or not errors:
                    st.session_state["scenes"] = all_scenes
                    st.session_state["scene_query"] = query
                else:
                    st.session_state.pop("scene_query", None)
                    st.error("Не удалось получить даты ни от одного спутника.")
        if st.session_state.get("scene_query") == query:
            available = st.session_state.get("scenes") or []
            days_available = sorted(
                {(scene["date"], scene["satellite"]) for scene in available},
                reverse=True,
            )
            st.caption(
                f"Всего сцен: {len(available)}; "
                f"дат Sentinel-2: {len({x['date'] for x in available if x['satellite'] == SENTINEL})}; "
                f"дат Landsat: {len({x['date'] for x in available if x['satellite'] == LANDSAT})}."
            )
            if days_available:
                chosen_date, chosen_satellite = st.selectbox(
                    "Дата съёмки и спутник",
                    days_available,
                    format_func=lambda pair: (
                        f"{date.fromisoformat(pair[0]):%d.%m.%Y} · "
                        f"{'S2' if pair[1] == SENTINEL else 'L8/9'}"
                    ),
                    key="selected_scene_date_and_satellite",
                )
                scene_items = [
                    item for item in available
                    if item["date"] == chosen_date and item["satellite"] == chosen_satellite
                ]
                scene_choices = ["Все сцены этой даты"] + [item["label"] for item in scene_items]
                choice = st.selectbox("Сцена", scene_choices, key="selected_scene")
                if st.button(
                    "Добавить снимок как слой", disabled=not connected,
                    type="primary", use_container_width=True,
                ):
                    index = scene_choices.index(choice)
                    image_id = None if index == 0 else scene_items[index - 1]["image_id"]
                    if index != 0 and not image_id:
                        st.error("У выбранной сцены отсутствует ID GEE.")
                    else:
                        with st.spinner("Добавляю независимый слой снимка..."):
                            try:
                                spec = new_scene_layer(
                                    chosen_satellite, date.fromisoformat(chosen_date),
                                    image_id,
                                    "" if index == 0 else scene_items[index - 1]["time"],
                                )
                                add_image_layer(spec, bbox)
                                st.caption(f"Добавлен слой: {spec['name']}")
                            except Exception as exc:
                                st.error(f"Ошибка добавления слоя: {exc}")
            else:
                st.caption("Для выбранного месяца снимков не найдено.")
        else:
            st.caption("Выберите год и месяц, затем нажмите «Найти доступные даты».")
    else:
        st.markdown(
            """<div class="median-heading"><strong>3. Медиана</strong>
<span class="median-help-icon" tabindex="0" aria-describedby="median-help-text">?
<span id="median-help-text" class="median-help-content" role="tooltip">
Медиана — изображение, в котором для каждого пикселя и канала берётся медианное
значение по спутниковым сценам выбранного периода. Это уменьшает влияние
случайных изменений. В этом приложении RGB-медиана вычисляется без маски
облаков, поэтому облака могут оставаться. Медиана не является снимком на
конкретную дату.
</span></span></div>""", unsafe_allow_html=True,
        )
        median_start = st.date_input(
            "Первый день интервала", value=date(2025, 6, 1),
            key="median_start", format="DD.MM.YYYY"
        )
        median_length = st.slider(
            "Длительность, календарных дней", 2, 9, 5, 1, key="median_length"
        )
        median_end = median_start + timedelta(days=median_length-1)
        st.caption(
            f"В мозаику попадут сцены с {median_start:%d.%m.%Y} "
            f"по {median_end:%d.%m.%Y} включительно."
        )
        count = None
        if connected:
            try:
                with st.spinner("Считаю сцены за выбранный интервал..."):
                    count = cached_interval_count(
                        satellite, tuple(round(v, 6) for v in bbox),
                        median_start, median_length
                    )
                # Neutral grey caption rather than a blue alert.
                st.caption(f"Найдено спутниковых сцен за выбранный интервал: {count}.")
            except Exception as exc:
                st.warning(f"Не удалось определить количество сцен: {exc}")
        if st.button(
            "Добавить медиану как слой",
            disabled=(not connected or count is None or count == 0),
            type="primary", use_container_width=True
        ):
            with st.spinner("Добавляю медианную мозаику отдельным слоем..."):
                try:
                    spec = new_median_layer(satellite, median_start, median_length)
                    add_image_layer(spec, bbox)
                    st.caption(f"Добавлен слой: {spec['name']} · {count} сцен")
                except Exception as exc:
                    st.error(f"Ошибка добавления медианы: {exc}")

    st.divider()
    st.subheader("4. Заметки")
    st.text_area(
        "Доступны к выгрузке вместе с галереей",
        key="research_note",
        placeholder="Жалобы, петиции, ходатайства, прошения, уведомления, заявления...",
        height=180,
        max_chars=18000,
    )

    st.divider()
    st.subheader("5. Сохранение исследования по ссылке")
    gallery_items = st.session_state["gallery"]

    current_token = st.session_state.get("active_token")
    if not store:
        st.caption(
            "Для постоянной ссылки нужны SUPABASE_URL и SUPABASE_SECRET_KEY в Secrets. "
            "Экспорт ZIP/PDF работает и без подключения базы."
        )
    elif current_token:
        st.caption("Автосохранение включено. Владелец ссылки может редактировать исследование.")
        st.code(share_link(current_token), language=None)
        incomplete = [p for p in gallery_items if not p.get("storage_key")]
        if incomplete:
            st.caption(
                f"Ожидают облачного сохранения: {len(incomplete)}. "
                "Не закрывайте вкладку до завершения."
            )
            if st.button(
                "Повторить загрузку изображений в Supabase",
                use_container_width=True,
                disabled=bool(st.session_state["gallery_upload_jobs"]),
            ):
                queue_gallery_uploads(current_token)
                st.rerun()
        with st.expander("Удалить проект полностью"):
            st.warning(
                "Это необратимое действие. Будут удалены данные исследования "
                "из Supabase и все его фотографии в приватном Storage. "
                "Ссылка перестанет работать. Любой, у кого есть ссылка, "
                "может удалить проект, поскольку авторизации пока нет."
            )
            if st.session_state["gallery_upload_jobs"]:
                st.info(
                    "Перед удалением дождитесь завершения загрузки фотографий."
                )
            phrase = st.text_input(
                "Для подтверждения напишите УДАЛИТЬ",
                key="delete_project_phrase",
                placeholder="УДАЛИТЬ",
            )
            if st.button(
                "Удалить проект безвозвратно",
                key="confirm_delete_project",
                use_container_width=True,
                disabled=(
                    phrase.strip() != "УДАЛИТЬ"
                    or bool(st.session_state["gallery_upload_jobs"])
                    or gallery_store is None
                ),
            ):
                try:
                    with st.spinner("Удаляю фотографии и исследование из Supabase..."):
                        removed_photos = delete_project(
                            store, gallery_store, current_token
                        )
                    st.session_state.pop("active_token", None)
                    st.session_state.pop("last_saved_digest", None)
                    st.session_state["reset_after_delete"] = True
                    st.query_params.clear()
                    st.toast(
                        f"Проект удалён из БД. Файлов Storage удалено: {removed_photos}."
                    )
                    st.rerun()
                except SessionStorageError as exc:
                    st.error(
                        "Не удалось полностью удалить проект: " + str(exc)
                        + " Запись исследования сохранена, повторите удаление."
                    )
        try:
            st.link_button(
                "Создать другое исследование",
                st.context.url, use_container_width=True,
            )
        except (AttributeError, RuntimeError):
            st.caption("Откройте адрес приложения без ?session=… для нового исследования.")
    elif st.button("Сохранить и получить ссылку", type="primary", use_container_width=True):
        try:
            # Do not expose an incomplete photo in the public study before
            # the corresponding file is safely written to private Storage.
            initial = research_payload(routes, points)
            initial["gallery"] = [
                p for p in initial["gallery"] if p.get("storage_key")
            ]
            token = store.create(initial)
            st.session_state["active_token"] = token
            st.session_state["loaded_map_revision"] = token
            st.session_state["last_saved_digest"] = hashlib.sha256(
                json.dumps(initial, ensure_ascii=False, sort_keys=True).encode()
            ).hexdigest()
            if gallery_items:
                queue_gallery_uploads(token)
            st.query_params["session"] = token
            st.success(
                "Ссылка создана. " +
                ("Снимки галереи загружаются в облако — не закрывайте вкладку."
                 if gallery_items else "Исследование сохранено.")
            )
            st.code(share_link(token), language=None)
        except (SessionStorageError, ValueError) as exc:
            st.error(f"Не удалось полностью сохранить исследование: {exc}")

    st.divider()
    st.subheader("6. Геопривязанный GeoTIFF")
    available_areas = []
    current_area = st.session_state.get("map_state", {}).get("selection_bounds")
    if current_area:
        available_areas.append(("current", "Последняя выделенная область", current_area))
    available_areas += [
        (item["id"], f"Галерея: {item['title'] or str(i+1)}", item["bounds"])
        for i, item in enumerate(gallery_items)
        if item.get("bounds") is not None
    ]
    if available_areas and st.session_state["image_layers"]:
        area_choice = st.selectbox(
            "Географическая область",
            range(len(available_areas)),
            format_func=lambda idx: available_areas[idx][1],
            key="geotiff_area",
        )
        source_choice = st.selectbox(
            "Исходный спутниковый слой",
            range(len(st.session_state["image_layers"])),
            format_func=lambda idx: st.session_state["image_layers"][idx]["name"],
            key="geotiff_layer",
        )
        source_spec = st.session_state["image_layers"][source_choice]
        scale = st.number_input(
            "Размер пикселя, м",
            min_value=10, max_value=200, step=10,
            value=suggested_scale(source_spec["satellite"]),
            key="geotiff_scale",
        )
        st.caption("GeoTIFF содержит 4 канала отражения: red, green, blue, swir. "
                   "Это НЕ скриншот и не включает подложку, трек и нарисованные линии.")
        if st.button("Подготовить GeoTIFF", disabled=not connected,
                     use_container_width=True):
            with st.spinner("Выгружаю геопривязанный фрагмент из GEE..."):
                try:
                    st.session_state["geotiff_bytes"] = export_geotiff(
                        source_spec, available_areas[area_choice][2], int(scale)
                    )
                    st.session_state["geotiff_name"] = geotiff_filename(
                        source_spec, available_areas[area_choice][2], int(scale)
                    )
                except Exception as exc:
                    st.error(f"Не удалось создать GeoTIFF: {exc}")
        if st.session_state.get("geotiff_bytes"):
            st.download_button(
                "Скачать GeoTIFF",
                data=st.session_state["geotiff_bytes"],
                file_name=st.session_state.get("geotiff_name", "snow_area.tif"),
                mime="image/tiff",
                use_container_width=True,
            )
    else:
        st.caption("Выделите область на карте и добавьте хотя бы один снимок.")

    st.divider()
    st.caption(f"Открыто спутниковых слоёв: {len(st.session_state['image_layers'])}. "
               "Их видимость, прозрачность и удаление — в «Слоях карты».")
    if st.button("Обновить спутниковые тайлы", disabled=not connected,
                 use_container_width=True):
        st.session_state["runtime_tiles"] = {}
        st.session_state["analysis_tiles"] = {}
        st.rerun()

if connection_error:
    with st.expander("Настройка доступа к Earth Engine"):
        st.write(connection_error)
        st.caption("Секреты не нужно добавлять в GitHub.")

# Stable tab labels avoid resetting selection when a photo is added.
# Streamlit 1.55+ skips expensive hidden-tab code when on_change="rerun".
tab_map, tab_gallery = st.tabs(
    ["Карта", "Галерея"], key="active_view", on_change="rerun"
)

if tab_map.open:
  with tab_map:
    # Populate this space after the map component reports a screenshot;
    # the editing form is visually above the map, not hidden below the iframe.
    pending_slot = st.empty()
    caption = (
        f"Слоёв спутниковых снимков: {len(st.session_state['image_layers'])}."
        + (" Для каждого снимка доступны собственные граница снега и изотермы."
           if st.session_state["image_layers"] else
           " Добавьте снимок или медиану в панели слева.")
    )
    tiles = current_tile_layers(bbox, connected)
    config = map_config(
        routes, points, tiles, caption,
        st.session_state.get("map_state", {}),
    )
    # The component stays mounted on ordinary reruns; it is recreated only for a
    # changed geometry/image or when a different shareable session is loaded.
    render_id = hashlib.sha256(
        json.dumps(
            [routes, points, tiles, caption,
             st.session_state.get("loaded_map_revision", "")],
            ensure_ascii=False, sort_keys=True
        ).encode()
    ).hexdigest()[:24]
    map_event = research_map(config=config, render_id=render_id)
    if isinstance(map_event, dict):
        previous_state = st.session_state.get("map_state", {})
        incoming_state = map_event.get("map_state")
        if isinstance(incoming_state, dict):
            st.session_state["map_state"] = incoming_state
        changed_analysis = (
            isinstance(incoming_state, dict)
            and contour_flags(previous_state) != contour_flags(incoming_state)
        )
        action = map_event.get("action")
        event_id = map_event.get("event_id")
        is_fresh = event_id is not None and event_id != st.session_state.get("processed_map_event")
        if action == "delete_layer" and is_fresh:
            st.session_state["processed_map_event"] = event_id
            ident = map_event.get("layer_id", "")
            old_count = len(st.session_state["image_layers"])
            st.session_state["image_layers"] = [
                item for item in st.session_state["image_layers"]
                if item["id"] != ident
            ]
            if len(st.session_state["image_layers"]) != old_count:
                st.session_state["runtime_tiles"].pop(ident, None)
                st.session_state["analysis_tiles"].pop(ident, None)
                st.session_state.get("map_state", {}).get("overlays", {}).pop(ident, None)
                st.rerun()
        elif action == "area_selected" and is_fresh:
            st.session_state["processed_map_event"] = event_id
            try:
                bounds = validate_bounds(map_event.get("bounds"))
                st.session_state["map_state"]["selection_bounds"] = bounds
                st.rerun()
            except SessionStorageError as exc:
                st.warning(f"Не удалось выбрать область: {exc}")
        elif action == "capture_image" and is_fresh:
            st.session_state["processed_map_event"] = event_id
            try:
                if st.session_state.get("pending_photo"):
                    st.warning("Сначала сохраните или отмените предыдущий снимок.")
                else:
                    bounds = validate_bounds(map_event.get("bounds"))
                    fmt = map_event.get("image_format", "png")
                    validation_start = perf_counter()
                    blob, fmt = read_snapshot(map_event.get("image_data", ""), fmt)
                    st.session_state["capture_diagnostics"] = {
                        "bytes": len(blob),
                        "browser_ms": int(map_event.get("capture_ms") or 0),
                        "validation_ms": round((perf_counter() - validation_start) * 1000),
                    }
                    item = new_gallery_item(bounds, fmt)
                    capture_state = (
                        incoming_state if isinstance(incoming_state, dict)
                        else st.session_state["map_state"]
                    )
                    visible_layers = [
                        layer for layer in st.session_state["image_layers"]
                        if capture_state.get("overlays", {}).get(
                            layer["id"], {}
                        ).get("visible", True)
                    ]
                    if visible_layers:
                        source = visible_layers[-1]
                        item["source_layer_id"] = source["id"]
                        item["source_date"] = source.get("day", source.get("start", ""))
                    item["title"] = suggested_gallery_title(
                        st.session_state["image_layers"], capture_state
                    )
                    # Keep the image in a draft until the title/note form is confirmed.
                    # Every new screenshot gets its own random gallery ID.
                    st.session_state["pending_photo"] = {"item": item, "data": blob}
                    st.session_state["map_state"]["selection_bounds"] = bounds
                    # The component event already caused this Streamlit run.
                    # The pending-photo form is rendered below, in the same run.
                    # A second st.rerun() here used to make the first click seem lost.
                    st.toast("Снимок готов — добавьте название или заметку и сохраните.")
            except (SessionStorageError, ValueError, TypeError) as exc:
                st.error(f"Не удалось подготовить снимок для галереи: {exc}")
        elif changed_analysis:
            # Rebuild the map tile payload only when an individual contour flag changes.
            st.rerun()
    
    for warning in st.session_state.get("warnings", []):
        st.warning(warning)
    if tiles:
        st.caption(
            "Каждый снимок отображается без облачной маски. "
            "Для каждого снимка граница снега рассчитывается из облачно-маскированных "
            "данных NDSI (может включать ледники), а изотермы ERA5-Land — модельная оценка. "
            "Граница снега и каждая изотерма включаются независимо в «Слоях карты»."
        )
    with pending_slot.container():
        render_pending_photo_form()

if tab_gallery.open:
    with tab_gallery:
        render_gallery_tab()

@st.fragment(run_every=2.0)
def watch_gallery_uploads() -> None:
    """Poll asynchronous Supabase uploads without rerunning the entire map."""
    pending = st.session_state["gallery_upload_jobs"]
    errors = st.session_state["gallery_upload_errors"]
    completed = False
    if pending:
        for ident, future in list(pending.items()):
            if not future.done():
                continue
            pending.pop(ident, None)
            try:
                storage_key, seconds = future.result()
                match = next(
                    (item for item in st.session_state["gallery"]
                     if item["id"] == ident), None
                )
                if match is None:
                    # Uploaded photo was deleted while in flight; avoid returning
                    # its private object path as a live gallery attachment.
                    if gallery_store:
                        try:
                            gallery_store.remove(storage_key)
                        except SessionStorageError:
                            pass
                else:
                    match["storage_key"] = storage_key
                    st.session_state["gallery_upload_timings"][ident] = round(seconds, 2)
                    errors.pop(ident, None)
                completed = True
            except Exception as exc:
                errors[ident] = str(exc)
                completed = True
    if completed:
        # Let the normal, top-level autosave commit only successfully uploaded
        # images. The in-memory photo remains available if an upload fails.
        st.session_state.pop("last_saved_digest", None)
        st.rerun()
    if pending:
        st.caption(
            f"⏳ Сохраняю в Supabase: {len(pending)} фото. "
            "Не закрывайте вкладку, пока загрузка не завершена."
        )
    elif errors:
        st.warning(
            f"Не удалось загрузить {len(errors)} фото. "
            "Используйте «Повторить загрузку» в боковой панели."
        )
    elif st.session_state.get("gallery_upload_timings"):
        seconds = list(st.session_state["gallery_upload_timings"].values())
        st.caption(
            f"✓ Фото в Supabase. Последняя загрузка: {seconds[-1]:.1f} с."
        )


if st.session_state.get("gallery_upload_jobs") or st.session_state.get("gallery_upload_errors"):
    watch_gallery_uploads()

# Autosave if the state has actually changed. No credentials or tile URLs
# are included in the stored payload.
token = st.session_state.get("active_token")
if store and token:
    try:
        snapshot = research_payload(routes, points)
        snapshot_digest = hashlib.sha256(
            json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
        if snapshot_digest != st.session_state.get("last_saved_digest"):
            if any(not image.get("storage_key") for image in st.session_state["gallery"]):
                # Preserve the old valid version of the session until all
                # referenced photos are safely persisted. Non-photo edits
                # will be saved once outstanding uploads have completed.
                pass
            else:
                store.save(token, snapshot)
                st.session_state["last_saved_digest"] = snapshot_digest
    except (SessionStorageError, ValueError, TypeError) as exc:
        st.warning(f"Автосохранение не выполнено: {exc}")
