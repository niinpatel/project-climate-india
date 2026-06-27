"""One-shot fetch of MCGM ward boundaries from OpenStreetMap (Overpass API).

Run manually and re-run only if boundaries need correcting:
    python fetch_boundaries.py
"""
import json

import requests

OVERPASS_URL = 'https://overpass-api.de/api/interpreter'

# Bounding box around Greater Mumbai (south, west, north, east).
MUMBAI_BBOX = (18.86, 72.75, 19.30, 72.99)

OUT_PATH = 'boundaries/mumbai_wards.geojson'


def query_overpass(admin_level: int) -> dict:
    bbox = ','.join(str(v) for v in MUMBAI_BBOX)
    query = f"""
    [out:json][timeout:90];
    relation["boundary"="administrative"]["admin_level"="{admin_level}"]({bbox});
    (._;>;);
    out geom;
    """
    response = requests.post(
        OVERPASS_URL,
        data={'data': query},
        headers={'User-Agent': 'project-climate-india/1.0 (SUHI research)'},
        timeout=120,
    )
    response.raise_for_status()
    return response.json()


def assemble_rings(way_geometries: list) -> list:
    """Chain way segments (each a list of (lon, lat) tuples) into closed rings."""
    segments = [list(seg) for seg in way_geometries if len(seg) >= 2]
    rings = []
    while segments:
        ring = segments.pop(0)
        merged = True
        while merged and ring[0] != ring[-1]:
            merged = False
            for i, seg in enumerate(segments):
                if seg[0] == ring[-1]:
                    ring.extend(seg[1:])
                    segments.pop(i)
                    merged = True
                    break
                if seg[-1] == ring[-1]:
                    ring.extend(list(reversed(seg))[1:])
                    segments.pop(i)
                    merged = True
                    break
                if seg[-1] == ring[0]:
                    ring[0:0] = seg[:-1]
                    segments.pop(i)
                    merged = True
                    break
                if seg[0] == ring[0]:
                    ring[0:0] = list(reversed(seg))[:-1]
                    segments.pop(i)
                    merged = True
                    break
        rings.append(ring)
    return [r for r in rings if len(r) >= 4 and r[0] == r[-1]]


def relation_to_feature(relation: dict, ways_by_id: dict) -> dict:
    outer_ways = [
        ways_by_id[m['ref']]
        for m in relation.get('members', [])
        if m['type'] == 'way' and m.get('role', 'outer') == 'outer' and m['ref'] in ways_by_id
    ]
    way_geometries = [
        [(pt['lon'], pt['lat']) for pt in way['geometry']]
        for way in outer_ways
        if 'geometry' in way
    ]
    rings = assemble_rings(way_geometries)
    if not rings:
        return None

    coords = [[list(pt) for pt in ring] for ring in rings]
    geometry = (
        {'type': 'Polygon', 'coordinates': coords}
        if len(coords) == 1
        else {'type': 'MultiPolygon', 'coordinates': [[ring] for ring in coords]}
    )
    ward_name = relation.get('tags', {}).get('name', '')
    return {
        'type': 'Feature',
        'properties': {'ward_name': ward_name},
        'geometry': geometry,
    }


def build_feature_collection(osm_data: dict) -> dict:
    ways_by_id = {el['id']: el for el in osm_data['elements'] if el['type'] == 'way'}
    relations = [el for el in osm_data['elements'] if el['type'] == 'relation']

    features = []
    for relation in relations:
        feature = relation_to_feature(relation, ways_by_id)
        if feature is not None and feature['properties']['ward_name']:
            features.append(feature)

    return {'type': 'FeatureCollection', 'features': features}


def main():
    osm_data = query_overpass(admin_level=10)
    fc = build_feature_collection(osm_data)

    if len(fc['features']) < 20:
        print(f'Only found {len(fc["features"])} wards at admin_level=10, retrying with admin_level=9')
        osm_data = query_overpass(admin_level=9)
        fc = build_feature_collection(osm_data)

    print(f'Fetched {len(fc["features"])} ward boundaries')

    with open(OUT_PATH, 'w') as f:
        json.dump(fc, f)


if __name__ == '__main__':
    main()
