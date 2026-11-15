"""Streamlit frontend for snow conditions and satellite scene analysis."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

from geo_utils import GeoInputError, read_geo, search_geometry
from gee_client import (
    EarthEngineUnavailable, SENTINEL, LANDSAT,
    initialize_ee, list_month_scenes, scene_image, median_images, build_tile_layers,
)
from map_builder import map_html
from google_terrain import get_google_terrain

ROOT = Path(__file__).resolve().parent
MONTHS = {
    1: 'Январь', 2: 'Февраль', 3: 'Март', 4: 'Апрель',
    5: 'Май', 6: 'Июнь', 7: 'Июль', 8: 'Август',
    9: 'Сентябрь', 10: 'Октябрь', 11: 'Ноябрь', 12: 'Декабрь',
}

st.set_page_config(page_title='Анализ снежной обстановки', page_icon='🏔️', layout='wide')
st.markdown('''<style>
.block-container{padding-top:1.3rem;padding-bottom:1rem;max-width:none}
[data-testid="stSidebar"]{min-width:335px}
</style>''', unsafe_allow_html=True)


def setting(name: str, default=None):
    try:
        if name in st.secrets:
            return st.secrets[name]
    except (FileNotFoundError, KeyError):
        pass
    return os.environ.get(name, default)


@st.cache_resource(show_spinner=False)
def connect_ee(project: str, serialized_service_account: str | None):
    service_account = json.loads(serialized_service_account) if serialized_service_account else None
    initialize_ee(project, service_account)
    return True


@st.cache_data(ttl=3600, show_spinner=False)
def cached_scenes(satellite: str, bbox: tuple[float, ...], year: int, month: int):
    return list_month_scenes(satellite, list(bbox), year, month)


@st.cache_data(ttl=3600, show_spinner=False)
def cached_google_terrain(api_key: str, bbox: tuple[float, ...]):
    return get_google_terrain(api_key, list(bbox))


def load_uploaded(filename: str, data: bytes):
    try:
        return read_geo(filename, data)
    except GeoInputError as exc:
        st.error(f'Ошибка файла «{filename}»: {exc}')
        return [], []


with st.sidebar:
    st.title('🏔️ Анализ снежной обстановки')
    st.caption('Спутниковые снимки, граница снега и изотермы вдоль маршрута')
    st.subheader('1. Трек и точки')
    route_upload = st.file_uploader('Загрузить маршрут KML или GPX', type=['kml', 'gpx'], key='route_file')
    points_upload = st.file_uploader(
        'Дополнительные точки KML, GPX или CSV',
        type=['kml', 'gpx', 'csv'], accept_multiple_files=True, key='points_files'
    )

    if 'manual_points' not in st.session_state:
        st.session_state['manual_points'] = []
    with st.expander('Добавить точку вручную'):
        with st.form('point_form'):
            point_name = st.text_input('Название', value='Новая точка')
            col1, col2 = st.columns(2)
            point_lat = col1.number_input('Широта', min_value=-90.0, max_value=90.0, value=42.35, format='%.6f')
            point_lon = col2.number_input('Долгота', min_value=-180.0, max_value=180.0, value=46.25, format='%.6f')
            submitted = st.form_submit_button('Добавить точку', use_container_width=True)
            if submitted:
                st.session_state['manual_points'].append({
                    'name': point_name.strip() or 'Точка',
                    'coordinates': [point_lon, point_lat],
                })
        if st.button('Очистить добавленные вручную точки', use_container_width=True):
            st.session_state['manual_points'] = []

    st.divider()
    st.subheader('2. Спутниковые снимки')
    satellite = st.selectbox('Источник', [SENTINEL, LANDSAT], index=0)
    mode = st.radio('Режим', ['Конкретный снимок', 'Медианная мозаика'], label_visibility='visible')

# The app opens with the current supplied KML; uploading a route replaces it.
if route_upload is None:
    routes, _ = read_geo('default_route.kml', (ROOT / 'data/default_route.kml').read_bytes())
    route_status = 'Пример трека из проекта'
else:
    routes, _ = load_uploaded(route_upload.name, route_upload.getvalue())
    route_status = route_upload.name

points = []
# Demo obstacles belong ONLY to the bundled example route.
# They never appear on a user's uploaded GPX/KML track.
if route_upload is None:
    _, original = read_geo('default_points.kml', (ROOT / 'data/default_points.kml').read_bytes())
    points += original
for file in points_upload or []:
    _, found = load_uploaded(file.name, file.getvalue())
    points += found
points += st.session_state['manual_points']

geo_hash = hashlib.sha256(json.dumps([routes, points], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
if st.session_state.get('last_geo_hash') != geo_hash:
    st.session_state['last_geo_hash'] = geo_hash
    st.session_state.pop('tiles', None)
    st.session_state.pop('image_caption', None)
    st.session_state.pop('scenes', None)
    st.session_state.pop('scene_query', None)
    st.session_state.pop('median_scene_count', None)

bbox = search_geometry(routes, points if not routes else [], buffer_m=4500)['bbox']

project = setting('GEE_PROJECT', '')
service = setting('GEE_SERVICE_ACCOUNT', None)
if service is None:
    try:
        if 'gee_service_account' in st.secrets:
            service = dict(st.secrets['gee_service_account'])
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
except (EarthEngineUnavailable, Exception) as exc:
    connection_error = str(exc)

st.title('Анализ снежной обстановки')
st.caption(f'Маршрут: {route_status} · линий: {len(routes)} · точек: {len(points)}. '
           'Данные о снеге — NDSI; изотермы — модельная оценка ERA5-Land.')

with st.sidebar:
    if not connected:
        st.warning('Earth Engine пока не подключён. Маршрут отображается, но спутниковые слои недоступны.')
    if mode == 'Конкретный снимок':
        st.markdown('**3. Найти реальную дату съёмки**')
        col_year, col_month = st.columns(2)
        year = col_year.selectbox('Год', list(range(date.today().year, 2016, -1)), index=0, key='year')
        month = col_month.selectbox('Месяц', list(MONTHS), index=5, format_func=lambda x: MONTHS[x], key='month')
        query = (satellite, tuple(round(v, 6) for v in bbox), year, month)
        if st.button('Найти доступные даты', disabled=not connected, use_container_width=True):
            with st.spinner('Получаю перечень реальных сцен из GEE...'):
                try:
                    scenes = cached_scenes(*query)
                    st.session_state['scenes'] = scenes
                    st.session_state['scene_query'] = query
                except Exception as exc:
                    st.error(f'Не удалось найти снимки: {exc}')
        if st.session_state.get('scene_query') == query:
            available = st.session_state.get('scenes') or []
            actual_days = sorted({x['date'] for x in available}, reverse=True)
            st.caption(f'Найдено сцен: {len(available)}; дней съёмки: {len(actual_days)}')
            if actual_days:
                chosen_day = st.selectbox('Дата съёмки', actual_days)
                chosen_scenes = [x for x in available if x['date'] == chosen_day]
                choices = ['Все сцены этой даты (без смешивания дней)'] + [x['label'] for x in chosen_scenes]
                choice = st.selectbox('Сцена', choices)
                if st.button('Показать снимок', disabled=not connected, type='primary', use_container_width=True):
                    image_id = None if choice == choices[0] else chosen_scenes[choices.index(choice)-1]['image_id']
                    if choice != choices[0] and not image_id:
                        st.error('У сцены отсутствует идентификатор GEE; попробуйте «Все сцены этой даты».')
                    else:
                        with st.spinner('Создаю картографические слои...'):
                            try:
                                day = date.fromisoformat(chosen_day)
                                image = scene_image(satellite, bbox, day, image_id, mask_clouds=False)
                                masked = scene_image(satellite, bbox, day, image_id, mask_clouds=True)
                                tiles, warnings = build_tile_layers(image, bbox, day, masked)
                                st.session_state['tiles'] = tiles
                                st.session_state['image_caption'] = f'{satellite} · {chosen_day} · {choice}'
                                st.session_state['warnings'] = warnings
                            except Exception as exc:
                                st.error(f'Ошибка при создании снимка: {exc}')
            else:
                st.info('В этом месяце снимков нет.')
        else:
            st.caption('Выберите год и месяц, затем нажмите «Найти доступные даты».')
    else:
        st.markdown('**3. Медианная мозаика**')
        median_start = st.date_input('Первый день интервала', value=date(2025, 6, 1))
        median_length = st.slider('Длительность, календарных дней', min_value=2, max_value=9, value=5, step=1)
        median_end = median_start + timedelta(days=median_length - 1)
        st.caption(f'В мозаику попадут сцены с {median_start:%d.%m.%Y} по {median_end:%d.%m.%Y} включительно.')
        if st.button('Показать медиану', disabled=not connected, type='primary', use_container_width=True):
            with st.spinner('Рассчитываю медианную мозаику...'):
                try:
                    image, masked, count = median_images(satellite, bbox, median_start, median_length)
                    tiles, warnings = build_tile_layers(image, bbox, median_start, masked)
                    st.session_state['tiles'] = tiles
                    st.session_state['image_caption'] = (
                        f'Медианная мозаика · {satellite} · {median_start}–{median_end} · '
                        f'{count} сцен за {median_length} дней'
                    )
                    st.session_state['median_scene_count'] = count
                    st.session_state['warnings'] = warnings
                except Exception as exc:
                    st.error(f'Ошибка при расчёте медианы: {exc}')

    st.divider()
    st.caption('В «Слоях карты» можно переключать маску облаков, прозрачность, подписи точек и подложку.')

if connection_error:
    with st.expander('Настройка доступа к Earth Engine'):
        st.write(connection_error)
        st.markdown('Укажите `GEE_PROJECT` и настройте авторизацию по инструкции из `README.md`. Секреты не загружайте на GitHub.')

caption = st.session_state.get('image_caption', 'Трек без спутникового снимка')
tiles = st.session_state.get('tiles', [])

# Optional: Google Maps Platform Maps Tiles API, not the Earth Engine key.
google_terrain = None
maps_api_key = setting('GOOGLE_MAPS_API_KEY', '')
if maps_api_key:
    try:
        google_terrain = cached_google_terrain(
            str(maps_api_key), tuple(round(float(v), 6) for v in bbox)
        )
    except Exception:
        # Google basemap is optional; fall back gracefully to Esri and OSM.
        google_terrain = None
        st.info('Google Terrain недоступен: проверьте Maps Tile API, API-ключ и биллинг. '
                'Остальные подложки работают без этого ключа.')

components.html(
    map_html(routes, points, tiles, caption, google_terrain=google_terrain),
    height=790,
    scrolling=False,
)
for warning in st.session_state.get('warnings', []):
    st.warning(warning)
if tiles:
    st.caption('Изначально показан снимок без маски облаков; её можно включить в «Слоях карты». '
               'Граница снега определена по NDSI и может включать ледники. Изотермы являются приближёнными: '
               'ERA5-Land (~11 км) с фиксированной высотной поправкой 6 °C/км, а не измерения на перевалах.')
