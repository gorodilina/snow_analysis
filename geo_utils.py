"""Safe, dependency-light readers for KML, GPX and point CSV files."""

from __future__ import annotations

import csv
import io
import math
from typing import Iterable

from defusedxml import ElementTree as SafeET

MAX_FILE_BYTES = 15 * 1024 * 1024
MAX_COORDS = 100_000


class GeoInputError(ValueError):
    """Invalid or unsupported geospatial upload."""


def _parse_position(token: str) -> list[float]:
    fields = token.strip().split(",")
    if len(fields) < 2:
        raise GeoInputError("Ожидаются координаты 'долгота,широта'.")
    try:
        lon, lat = float(fields[0]), float(fields[1])
    except ValueError as exc:
        raise GeoInputError("Некорректное число в координатах.") from exc
    if not (math.isfinite(lon) and math.isfinite(lat) and -180 <= lon <= 180 and -90 <= lat <= 90):
        raise GeoInputError("Координаты выходят за пределы широты/долготы.")
    return [lon, lat]


def _read_xml(data: bytes):
    if len(data) > MAX_FILE_BYTES:
        raise GeoInputError("Файл больше 15 МБ.")
    try:
        return SafeET.fromstring(data)
    except (ValueError, SafeET.ParseError) as exc:
        raise GeoInputError(f"Не удалось прочитать XML: {exc}") from exc


def _parse_coordinates(text: str) -> list[list[float]]:
    vertices = [_parse_position(chunk) for chunk in text.replace("\n", " ").split()]
    if len(vertices) > MAX_COORDS:
        raise GeoInputError("Слишком много вершин (лимит 100 000).")
    return vertices


def _kml(data: bytes) -> tuple[list[dict], list[dict]]:
    root = _read_xml(data)
    namespace = "{http://www.opengis.net/kml/2.2}"
    routes: list[dict] = []
    points: list[dict] = []
    placemarks = root.findall(".//" + namespace + "Placemark")
    for placemark in placemarks:
        name_node = placemark.find(namespace + "name")
        name = (name_node.text or "Объект").strip() if name_node is not None else "Объект"
        for line in placemark.findall(".//" + namespace + "LineString"):
            coord = line.find(namespace + "coordinates")
            if coord is not None and coord.text:
                positions = _parse_coordinates(coord.text)
                if len(positions) >= 2:
                    routes.append({"name": name, "coordinates": positions})
        for multi in placemark.findall(".//" + namespace + "MultiGeometry"):
            # Nested LineStrings and Points are already included in the selectors above.
            pass
        for point in placemark.findall(".//" + namespace + "Point"):
            coord = point.find(namespace + "coordinates")
            if coord is not None and coord.text:
                pts = _parse_coordinates(coord.text)
                if pts:
                    points.append({"name": name, "coordinates": pts[0]})
    if not routes and not points:
        raise GeoInputError("В KML не найдено линий LineString или точек Point.")
    return routes, points


def _gpx(data: bytes) -> tuple[list[dict], list[dict]]:
    root = _read_xml(data)
    routes: list[dict] = []
    points: list[dict] = []

    def tag(element):
        return element.tag.rsplit("}", 1)[-1]

    def coords(el):
        try:
            lon = float(el.attrib["lon"])
            lat = float(el.attrib["lat"])
        except (KeyError, ValueError) as exc:
            raise GeoInputError("Некорректные координаты GPX.") from exc
        return _parse_position(f"{lon},{lat}")

    def child_name(el, default):
        for child in el:
            if tag(child) == "name" and child.text:
                return child.text.strip()
        return default

    for el in root.iter():
        if tag(el) == "wpt":
            points.append({"name": child_name(el, "Точка"), "coordinates": coords(el)})
        elif tag(el) == "rte":
            positions = [coords(x) for x in el if tag(x) == "rtept"]
            if len(positions) >= 2:
                routes.append({"name": child_name(el, "Маршрут GPX"), "coordinates": positions})
        elif tag(el) == "trk":
            segments = [x for x in el if tag(x) == "trkseg"]
            for idx, seg in enumerate(segments):
                positions = [coords(x) for x in seg if tag(x) == "trkpt"]
                if len(positions) >= 2:
                    routes.append({"name": f"{child_name(el, 'Трек GPX')} ({idx + 1})", "coordinates": positions})
    if sum(len(r["coordinates"]) for r in routes) > MAX_COORDS:
        raise GeoInputError("Слишком много вершин GPX (лимит 100 000).")
    if not routes and not points:
        raise GeoInputError("В GPX не найдены треки, маршруты или точки.")
    return routes, points


def _csv_points(data: bytes) -> tuple[list[dict], list[dict]]:
    if len(data) > MAX_FILE_BYTES:
        raise GeoInputError("Файл больше 15 МБ.")
    try:
        content = data.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(content))
        rows = []
        for index, row in enumerate(reader, 1):
            lon = row.get("lon", row.get("longitude", ""))
            lat = row.get("lat", row.get("latitude", ""))
            rows.append({"name": row.get("name") or f"Точка {index}", "coordinates": _parse_position(f"{lon},{lat}")})
        if not rows:
            raise GeoInputError("CSV пуст. Ожидаются колонки name, lat, lon.")
        return [], rows
    except UnicodeDecodeError as exc:
        raise GeoInputError("CSV должен быть в UTF-8.") from exc


def read_geo(filename: str, data: bytes) -> tuple[list[dict], list[dict]]:
    """Return (routes, points), all coordinates stored in [lon,lat] WGS84."""
    filename = filename.lower()
    if filename.endswith(".kml"):
        return _kml(data)
    if filename.endswith(".gpx"):
        return _gpx(data)
    if filename.endswith(".csv"):
        return _csv_points(data)
    raise GeoInputError("Поддерживаются KML, GPX и CSV с точками.")


def bounding_box(routes: Iterable[dict], points: Iterable[dict]) -> list[list[float]]:
    """Leaflet bounds [[south, west], [north, east]]."""
    coordinates = [c for route in routes for c in route["coordinates"]]
    coordinates += [p["coordinates"] for p in points]
    if not coordinates:
        return [[41.83, 46.15], [42.40, 47.30]]
    return [[min(c[1] for c in coordinates), min(c[0] for c in coordinates)],
            [max(c[1] for c in coordinates), max(c[0] for c in coordinates)]]


def search_geometry(routes: list[dict], points: list[dict], buffer_m: int = 4500) -> dict:
    """Represent a buffered area as bbox (simple, robust; expanded in lat/lon)."""
    bounds = bounding_box(routes, points)
    south, west = bounds[0]
    north, east = bounds[1]
    latitude_pad = buffer_m / 111_320
    latitude_middle = (south + north) / 2
    longitude_pad = buffer_m / (111_320 * max(math.cos(math.radians(latitude_middle)), 0.15))
    return {"bbox": [west - longitude_pad, south - latitude_pad,
                     east + longitude_pad, north + latitude_pad]}
