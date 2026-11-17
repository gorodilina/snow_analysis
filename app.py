"""Snow analysis UI: satellite scenes, median mosaics and shareable research sessions."""
from __future__ import annotations

import hashlib
import json
import os
from datetime import date, timedelta
from pathlib import Path

import streamlit as st

from gee_client import (
    LANDSAT, SENTINEL, initialize_ee, list_month_scenes,
    count_interval_scenes, image_rgb_tile, analysis_overlay_tiles,
)
from geo_utils import GeoInputError, read_geo, search_geometry
from google_terrain import get_google_terrain
from map_builder import map_config
from map_component import research_map
from project_layers import (
    migrate_legacy_selection, new_median_layer, new_scene_layer, normalize_layers,
)
from project_archive import (
    build_project_zip, import_project_zip, safe_export_filename,
)
from gallery import (
    GalleryStorage, SessionStorageError as GalleryError, build_gallery_zip,
    build_gallery_pdf, new_gallery_item, normalize_gallery, read_snapshot,
    validate_bounds,
)
from geo_export import export_geotiff, suggested_scale
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


@st.cache_data(ttl=3600, show_spinner=False)
def cached_google_terrain(api_key: str, bbox: tuple[float, ...]):
    return get_google_terrain(api_key, list(bbox))


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
    st.session_state["notes"] = payload.get("notes") or []
    st.session_state["gallery"] = normalize_gallery(payload.get("gallery") or [])
    st.session_state["gallery_blobs"] = payload.get("_gallery_media") or {}
    st.session_state["gallery_export_bytes"] = None
    st.session_state["geotiff_bytes"] = None
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
        "notes": st.session_state.get("notes", []),
        "gallery": normalize_gallery(st.session_state.get("gallery", [])),
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
st.session_state.setdefault("manual_points", [])
st.session_state.setdefault("notes", [])
st.session_state.setdefault("gallery", [])
st.session_state.setdefault("gallery_blobs", {})
st.session_state.setdefault("gallery_export_bytes", None)
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
        raise SessionStorageError("Изображение недоступно. Восстановите проект из ZIP.")
    data = gallery_store.get(item["storage_key"])
    st.session_state["gallery_blobs"][ident] = data
    return data


def collect_gallery_images() -> dict[str, bytes]:
    return {item["id"]: gallery_image_bytes(item) for item in st.session_state["gallery"]}


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


with st.sidebar:
    st.title("🏔️ Анализ снежной обстановки")
    st.caption("Спутниковые снимки, граница снега и изотермы вдоль трека")
    st.subheader("1. Трек и точки")
    # Import must run before widget keys are instantiated, so the restored
    # satellite/date controls can be populated without Streamlit errors.
    with st.expander("Восстановить проект из ZIP"):
        archive_input = st.file_uploader(
            "Архив исследования", type=["zip"], key="project_zip"
        )
        if st.button("Загрузить проект из ZIP", disabled=archive_input is None,
                     use_container_width=True):
            try:
                apply_research_payload(import_project_zip(archive_input.getvalue()))
                st.session_state.pop("active_token", None)
                st.session_state.pop("last_saved_digest", None)
                st.session_state["loaded_map_revision"] = "imported"
                st.query_params.clear()
                st.rerun()
            except (SessionStorageError, ValueError, KeyError, TypeError) as exc:
                st.error(f"Ошибка импорта проекта: {exc}")
    uploads = st.file_uploader(
        "Загрузить файлы KML или GPX",
        type=["kml", "gpx", "csv"],
        accept_multiple_files=True,
        key=f"geo_files_{st.session_state.get('geo_uploader_revision', 0)}",
        help="Выберите несколько файлов одновременно. CSV — для точек.",
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
    satellite = st.selectbox("Источник", [SENTINEL, LANDSAT], key="satellite")
    mode = st.radio("Режим", ["Конкретный снимок", "Медианная мозаика"], key="mode")

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
    """Rebuild ephemeral URLs; overlays belong to each source image."""
    if not is_connected:
        return []
    catalogue = st.session_state["image_layers"]
    runtime = st.session_state["runtime_tiles"]
    analysis_cache = st.session_state["analysis_tiles"]
    contours_visible = bool(st.session_state.get("map_state", {}).get("analysis_visible", False))
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
        if contours_visible:
            if ident not in analysis_cache:
                try:
                    overlays, warnings = analysis_overlay_tiles(spec, region)
                    analysis_cache[ident] = overlays
                    for warning in warnings:
                        st.warning(f"{spec['name']}: {warning}")
                except Exception as exc:
                    st.warning(f"Контуры «{spec['name']}» пока недоступны: {exc}")
                    analysis_cache[ident] = []
            tiles.extend(analysis_cache[ident])
    valid_ids = {spec["id"] for spec in catalogue}
    for key in list(runtime):
        if key not in valid_ids:
            del runtime[key]
    for key in list(analysis_cache):
        if key not in valid_ids:
            del analysis_cache[key]
    return tiles


st.title("Анализ снежной обстановки")
st.caption(
    f"Трек: {route_status} · линий: {len(routes)} · точек: {len(points)}. "
    "Снег — по NDSI; изотермы — модельная оценка ERA5-Land."
)

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
        query = (satellite, tuple(round(v, 6) for v in bbox), year, month)
        if st.button("Найти доступные даты", disabled=not connected, use_container_width=True):
            with st.spinner("Получаю перечень сцен из Earth Engine..."):
                try:
                    st.session_state["scenes"] = cached_scenes(*query)
                    st.session_state["scene_query"] = query
                except Exception as exc:
                    st.error(f"Не удалось найти снимки: {exc}")
        if st.session_state.get("scene_query") == query:
            available = st.session_state.get("scenes") or []
            days_available = sorted({s["date"] for s in available}, reverse=True)
            st.caption(f"Найдено сцен: {len(available)}; дней съёмки: {len(days_available)}")
            if days_available:
                chosen = st.selectbox("Дата съёмки", days_available)
                scene_items = [s for s in available if s["date"] == chosen]
                scene_choices = ["Все сцены этой даты"] + [s["label"] for s in scene_items]
                choice = st.selectbox("Сцена", scene_choices)
                if st.button("Добавить снимок как слой", disabled=not connected,
                             type="primary", use_container_width=True):
                    index = scene_choices.index(choice)
                    image_id = None if index == 0 else scene_items[index - 1]["image_id"]
                    if index != 0 and not image_id:
                        st.error("У выбранной сцены отсутствует ID GEE.")
                    else:
                        with st.spinner("Добавляю независимый слой снимка..."):
                            try:
                                spec = new_scene_layer(
                                    satellite, date.fromisoformat(chosen), image_id,
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
        st.markdown("**3. Медианная мозаика**")
        median_start = st.date_input(
            "Первый день интервала", value=date(2025, 6, 1), key="median_start"
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
    with st.form("note_form", clear_on_submit=True):
        new_note = st.text_area(
            "Наблюдение о снеговой обстановке",
            placeholder="Например, 15 июня на северном склоне сохраняется снег...",
            max_chars=3000,
            height=90,
        )
        if st.form_submit_button("Добавить заметку", use_container_width=True):
            if new_note.strip():
                import secrets
                st.session_state["notes"].append({
                    "id": secrets.token_hex(8),
                    "text": new_note.strip(),
                    "created": date.today().isoformat(),
                })
    for note in st.session_state["notes"]:
        with st.expander(f"{note.get('created','')} · {note.get('text','')[:45]}"):
            st.write(note.get("text", ""))
            if st.button("Удалить заметку", key="delete_note_"+note["id"]):
                st.session_state["notes"] = [
                    n for n in st.session_state["notes"] if n["id"] != note["id"]
                ]
                st.rerun()

    st.divider()
    st.subheader("5. Сохранение по ссылке")
    st.text_input("Название исследования", key="research_name", max_chars=120)
    current_token = st.session_state.get("active_token")
    if not store:
        st.caption(
            "Чтобы сохранить исследование по ссылке, настройте "
            "SUPABASE_URL и SUPABASE_SECRET_KEY в Secrets. "
            "До этого текущая карта работает без сохранения на сервере."
        )
    elif current_token:
        st.caption("Автосохранение включено. Любой, у кого есть ссылка, может редактировать исследование.")
        st.code(share_link(current_token), language=None)
        try:
            st.link_button(
                "Создать другое исследование",
                st.context.url,
                use_container_width=True,
            )
        except (AttributeError, RuntimeError):
            st.caption("Для нового исследования откройте адрес приложения без ?session=…")
    elif st.button("Сохранить и получить ссылку", type="primary", use_container_width=True):
        try:
            payload = research_payload(routes, points)
            token = store.create(payload)
            st.session_state["active_token"] = token
            st.session_state["loaded_map_revision"] = token
            st.session_state["last_saved_digest"] = hashlib.sha256(
                json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
            ).hexdigest()
            st.query_params["session"] = token
            st.success("Ссылка создана. Скопируйте адрес исследования ниже.")
            st.code(share_link(token), language=None)
        except (SessionStorageError, ValueError) as exc:
            st.error(f"Не удалось сохранить исследование: {exc}")

    st.divider()
    st.caption(f"Открыто спутниковых слоёв: {len(st.session_state['image_layers'])}. "
               "Удаление, прозрачность и видимость — в «Слоях карты».")
    if st.button("Обновить спутниковые тайлы", disabled=not connected,
                 use_container_width=True):
        st.session_state["runtime_tiles"] = {}
        st.session_state["analysis_tiles"] = {}
        st.rerun()
    try:
        st.download_button(
            "Скачать исследование (ZIP)",
            data=build_project_zip(research_payload(routes, points)),
            file_name=safe_export_filename(st.session_state.get("research_name", "")),
            mime="application/zip",
            use_container_width=True,
            help="Сохраняются все треки, точки, заметки, снимки и настройки. "
                 "Растровые файлы не включаются.",
        )
    except (SessionStorageError, ValueError, TypeError) as exc:
        st.warning(f"Экспорт проекта недоступен: {exc}")
    st.caption(
        "Снимки накладываются друг на друга, новые выше старых. "
        "Граница снега и изотермы рассчитываются отдельно для каждого снимка "
        "и включаются общим переключателем в слоях карты."
    )

if connection_error:
    with st.expander("Настройка доступа к Earth Engine"):
        st.write(connection_error)
        st.caption("Секреты не нужно добавлять в GitHub.")

caption = (
    f"Слоёв спутниковых снимков: {len(st.session_state['image_layers'])}."
    + (" Для каждого снимка доступны собственные граница снега и изотермы."
       if st.session_state["image_layers"] else
       " Добавьте снимок или медиану в панели слева.")
)
tiles = current_tile_layers(bbox, connected)
google_terrain = None
maps_key = setting("GOOGLE_MAPS_API_KEY", "")
if maps_key:
    try:
        google_terrain = cached_google_terrain(
            str(maps_key), tuple(round(float(v), 6) for v in bbox)
        )
    except Exception:
        st.caption("Google Terrain недоступен; другие подложки работают.")

config = map_config(
    routes, points, tiles, caption, google_terrain,
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
    if isinstance(map_event.get("map_state"), dict):
        st.session_state["map_state"] = map_event["map_state"]
    if map_event.get("action") == "delete_layer":
        ident = map_event.get("layer_id", "")
        if ident and ident != st.session_state.get("last_deleted_image_id"):
            old_count = len(st.session_state["image_layers"])
            st.session_state["image_layers"] = [
                item for item in st.session_state["image_layers"]
                if item["id"] != ident
            ]
            if len(st.session_state["image_layers"]) != old_count:
                st.session_state["last_deleted_image_id"] = ident
                st.session_state["runtime_tiles"].pop(ident, None)
                st.session_state["analysis_tiles"].pop(ident, None)
                st.session_state.get("map_state", {}).get("overlays", {}).pop(ident, None)
                st.rerun()

for warning in st.session_state.get("warnings", []):
    st.warning(warning)
if tiles:
    st.caption(
        "Каждый снимок отображается без облачной маски. "
        "Для каждого снимка граница снега рассчитывается из облачно-маскированных "
        "данных NDSI (может включать ледники), а изотермы ERA5-Land — модельная оценка. "
        "Оба вида контуров включаются и скрываются одновременно."
    )

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
            store.save(token, snapshot)
            st.session_state["last_saved_digest"] = snapshot_digest
    except (SessionStorageError, ValueError, TypeError) as exc:
        st.warning(f"Автосохранение не выполнено: {exc}")
