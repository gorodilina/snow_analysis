from pathlib import Path
from geo_utils import read_geo, bounding_box, GeoInputError

HERE = Path(__file__).resolve().parents[1]


def test_reference_kmls():
    lines, points = read_geo('default_route.kml', (HERE/'data/default_route.kml').read_bytes())
    assert len(lines) == 1
    assert len(lines[0]['coordinates']) == 798
    assert not points
    lines2, points2 = read_geo('default_points.kml', (HERE/'data/default_points.kml').read_bytes())
    assert not lines2
    assert len(points2) == 28
    assert len(bounding_box(lines, points2)) == 2


def test_gpx():
    data = b'''<gpx xmlns="http://www.topografix.com/GPX/1/1"><trk><name>X</name>
    <trkseg><trkpt lat="42" lon="46"/><trkpt lat="43" lon="47"/></trkseg>
    </trk><wpt lat="42.3" lon="46.3"><name>A</name></wpt></gpx>'''
    routes, points = read_geo('foo.gpx', data)
    assert routes[0]['coordinates'] == [[46,42],[47,43]]
    assert points[0]['name'] == 'A'


def test_csv():
    routes, points = read_geo('foo.csv', b'name,lat,lon\nTest,42.1,46.2\n')
    assert len(points) == 1
    assert points[0]['coordinates'] == [46.2,42.1]


def test_reject_incorrect():
    try:
        read_geo('bad.csv', b'name,lat,lon\nTest,97,46\n')
        assert False
    except GeoInputError:
        pass
