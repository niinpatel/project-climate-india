"""Fetch administrative ward boundaries from OpenStreetMap (Overpass API).

Run manually and re-run only if boundaries need correcting:
    python fetch_boundaries.py               # fetch all cities
    python fetch_boundaries.py --city mumbai # fetch one city
"""
import argparse
import json
import os
import time
from collections import deque
from pathlib import Path

import requests

OVERPASS_URL = 'https://overpass-api.de/api/interpreter'

CITIES = {
    # admin_levels: ordered list of OSM admin_level values to try; first with >=20 results wins.
    # Default fallback sequence is [10, 9] when not specified.
    'mumbai':    {'bbox': (18.86, 72.75, 19.30, 72.99), 'expected_wards': 24},
    'delhi':     {'bbox': (28.40, 76.84, 28.88, 77.35), 'expected_wards': 272},
    'kolkata':   {'bbox': (22.45, 88.20, 22.65, 88.50), 'expected_wards': 144},
    # Bengaluru BBMP wards (~198) sit at admin_level=9; level 10 has ~370 sub-ward zones
    'bengaluru': {'bbox': (12.84, 77.46, 13.14, 77.78), 'expected_wards': 198, 'admin_levels': [9]},
    'chennai':   {'bbox': (12.85, 80.15, 13.20, 80.32), 'expected_wards': 200},
    'hyderabad': {'bbox': (17.28, 78.35, 17.60, 78.60), 'expected_wards': 150},
}

BOUNDARIES_DIR = Path(__file__).parent / 'boundaries'


def query_overpass(admin_level: int, bbox: tuple, retries: int = 3) -> dict:
    bbox_str = ','.join(str(v) for v in bbox)
    query = f"""
    [out:json][timeout:90];
    relation["boundary"="administrative"]["admin_level"="{admin_level}"]({bbox_str});
    (._;>;);
    out geom;
    """
    for attempt in range(retries):
        response = requests.post(
            OVERPASS_URL,
            data={'data': query},
            headers={'User-Agent': 'project-climate-india/1.0 (SUHI research)'},
            timeout=120,
        )
        if response.status_code in (429, 504):
            wait = 30 * (attempt + 1)
            print(f'  HTTP {response.status_code}, waiting {wait}s before retry ...')
            time.sleep(wait)
            continue
        response.raise_for_status()
        return response.json()
    response.raise_for_status()  # raise after exhausting retries


def assemble_rings(way_geometries: list) -> list:
    """Chain way segments (each a list of (lon, lat) tuples) into closed rings."""
    segments = [list(seg) for seg in way_geometries if len(seg) >= 2]
    rings = []
    while segments:
        ring = deque(segments.pop())  # O(1) pop from end
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
                    ring.extend(seg[-2::-1])
                    segments.pop(i)
                    merged = True
                    break
                if seg[-1] == ring[0]:
                    ring.extendleft(reversed(seg[:-1]))  # O(1) prepend, preserves order
                    segments.pop(i)
                    merged = True
                    break
                if seg[0] == ring[0]:
                    ring.extendleft(seg[1:])  # O(1) prepend, reverses seg[1:]
                    segments.pop(i)
                    merged = True
                    break
        rings.append(list(ring))
    closed = [r for r in rings if len(r) >= 4 and r[0] == r[-1]]
    open_count = len(rings) - len(closed)
    if open_count:
        print(f'  Warning: {open_count} open ring(s) discarded — OSM relation may have gaps')
    return closed


def relation_to_feature(relation: dict, ways_by_id: dict) -> dict:
    outer_ways = [
        ways_by_id[m['ref']]
        for m in relation.get('members', [])
        if m['type'] == 'way' and m.get('role', 'outer') in ('outer', '') and m['ref'] in ways_by_id
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


def fetch_city(city: str, cfg: dict) -> None:
    bbox = cfg['bbox']
    expected = cfg['expected_wards']
    admin_levels = cfg.get('admin_levels', [10, 9])
    out_path = BOUNDARIES_DIR / f'{city}_wards.geojson'

    fc = {'type': 'FeatureCollection', 'features': []}
    for level in admin_levels:
        print(f'[{city}] querying admin_level={level} ...')
        osm_data = query_overpass(admin_level=level, bbox=bbox)
        fc = build_feature_collection(osm_data)
        if len(fc['features']) >= 20:
            break
        if level != admin_levels[-1]:
            print(f'[{city}] only {len(fc["features"])} features at level {level}, trying next level ...')

    ward_count = len(fc['features'])
    if ward_count < expected:
        print(
            f'[{city}] Warning: {ward_count} of expected {expected} wards assembled — '
            f'saving anyway. Check OSM boundary relations for gaps.'
        )
    else:
        print(f'[{city}] fetched {ward_count} wards')

    os.makedirs(BOUNDARIES_DIR, exist_ok=True)
    with open(out_path, 'w') as f:
        json.dump(fc, f)
    print(f'[{city}] saved → {out_path}')


def main():
    parser = argparse.ArgumentParser(description='Fetch ward boundaries from OpenStreetMap.')
    parser.add_argument(
        '--city',
        choices=list(CITIES),
        help='Fetch a single city. Omit to fetch all cities.',
    )
    args = parser.parse_args()

    targets = {args.city: CITIES[args.city]} if args.city else CITIES
    for i, (city, cfg) in enumerate(targets.items()):
        if i > 0:
            time.sleep(15)  # be polite to the Overpass API between cities
        fetch_city(city, cfg)


if __name__ == '__main__':
    main()
