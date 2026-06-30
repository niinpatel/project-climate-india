"""Fetch citizen-recognizable neighborhood boundaries from OpenStreetMap.

Administrative ward relations in OSM are modeled inconsistently across Indian
cities and rarely match the localities people actually name (Mumbai exposes BMC
letter codes, Bengaluru's admin_level=9 returns fringe villages, Delhi has 1200+
micro-colonies). The neighborhoods people recognize live in OSM as ``place``
nodes (points) instead — ``suburb`` / ``neighbourhood`` / ``quarter``.

So per city we: fetch the municipal boundary polygon, fetch place nodes inside
it, collapse sub-block/stage/phase names to their parent, then tessellate the
seed points into polygons (Voronoi cells clipped to the boundary) and union the
cells that share a name. The result is one polygon per recognizable area.

Coverage tracks OSM mapping density, not city size: a densely-mapped metro yields
hundreds of areas; an under-mapped city may yield a handful or none, in which case
we fall back to a single city-wide area. See MIN_SEEDS and the guards in
tessellate_areas.

Run manually and re-run only if boundaries need correcting:
    python fetch_boundaries.py               # fetch all cities
    python fetch_boundaries.py --city mumbai # fetch one city
"""
import argparse
import json
import os
import re
import time
from collections import deque
from pathlib import Path

import requests
from scipy.spatial import Voronoi
from shapely.geometry import Point, Polygon, mapping, shape
from shapely.ops import unary_union

OVERPASS_URL = 'https://overpass-api.de/api/interpreter'

# Below this many in-boundary seed points, broaden the place-type query (town/
# village/hamlet) to try to recover more, and warn that the tessellation will be
# coarse. Density tracks OSM mapping quality, not population — a tier-2 city can
# fall here while a small well-mapped town clears it comfortably.
MIN_SEEDS = 12

# Default OSM place node types treated as recognizable neighborhoods.
DEFAULT_PLACE_TYPES = ['suburb', 'neighbourhood', 'quarter']
# Broader set used as an escalation when DEFAULT_PLACE_TYPES is too sparse.
BROAD_PLACE_TYPES = DEFAULT_PLACE_TYPES + ['town', 'village', 'hamlet']

CITIES = {
    # bbox: (south, west, north, east). boundary_admin_level: the OSM admin_level
    # of the municipal-corporation relation (8 is the typical Indian default).
    # boundary_name: optional case-insensitive substring to disambiguate when the
    # bbox contains several level-N relations (else the largest-area one wins).
    # Greater Mumbai isn't a single level-8 corporation here; it's two level-5
    # districts ("Mumbai City District" + "Mumbai Suburban District") — union them.
    'mumbai':    {'bbox': (18.86, 72.75, 19.30, 72.99),
                  'boundary_admin_level': 5, 'boundary_name': 'Mumbai'},
    # Delhi's municipal corps don't cover the territory cleanly; the NCT of Delhi
    # is a single level-4 relation named "Delhi" — use it as the city footprint.
    'delhi':     {'bbox': (28.40, 76.84, 28.88, 77.35),
                  'boundary_admin_level': 4, 'boundary_name': 'Delhi'},
    # BBMP was split into 5 "Bengaluru * City Corporation" relations at level 8;
    # union them all into one footprint.
    'bengaluru': {'bbox': (12.84, 77.46, 13.14, 77.78), 'boundary_name': 'Bengaluru'},
    'chennai':   {'bbox': (12.85, 80.15, 13.20, 80.32)},
    'hyderabad': {'bbox': (17.28, 78.35, 17.60, 78.60)},
}

BOUNDARIES_DIR = Path(__file__).parent / 'boundaries'


def query_overpass(query_body: str, retries: int = 3) -> dict:
    """POST a raw Overpass QL body (without the [out:json] header) and return JSON."""
    query = f'[out:json][timeout:90];\n{query_body}'
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


def relation_to_feature(relation: dict) -> dict:
    """Build a polygon Feature from a boundary relation fetched with ``out geom;``.

    ``out geom;`` embeds each way member's geometry inline (no separate node
    recursion needed), which keeps the boundary query light enough to avoid the
    Overpass gateway timeouts the full ``(._;>;)`` recursion provokes on large
    multi-way city boundaries.
    """
    way_geometries = [
        [(pt['lon'], pt['lat']) for pt in m['geometry']]
        for m in relation.get('members', [])
        if m['type'] == 'way' and m.get('role', 'outer') in ('outer', '') and 'geometry' in m
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
    name = relation.get('tags', {}).get('name', '')
    return {
        'type': 'Feature',
        'properties': {'name': name},
        'geometry': geometry,
    }


def fetch_boundary(city: str, cfg: dict):
    """Fetch the municipal boundary polygon. Returns (shapely_geom, name).

    A city's municipal area can span multiple admin_level relations — e.g.
    Bengaluru's BBMP was split into five "Bengaluru * City Corporation" polygons.
    So when a ``boundary_name`` substring is configured we **union** every matching
    relation into one footprint (the right behavior for a split corporation); with
    no ``boundary_name`` we fall back to the single largest-area relation in the bbox.
    """
    south, west, north, east = cfg['bbox']
    bbox_str = f'{south},{west},{north},{east}'
    level = cfg.get('boundary_admin_level', 8)
    print(f'[{city}] fetching municipal boundary (admin_level={level}) ...')
    osm = query_overpass(
        f'relation["boundary"="administrative"]["admin_level"="{level}"]({bbox_str});\n'
        f'out geom;'
    )
    relations = [el for el in osm['elements'] if el['type'] == 'relation']

    candidates = []
    for rel in relations:
        feat = relation_to_feature(rel)
        if feat is None or not feat['properties']['name']:
            continue
        geom = shape(feat['geometry']).buffer(0)  # buffer(0) repairs self-touching rings
        if not geom.is_empty:
            candidates.append((geom, feat['properties']['name']))
    if not candidates:
        raise SystemExit(
            f'[{city}] ERROR: no admin_level={level} boundary relation found in bbox. '
            f'Set boundary_admin_level (or boundary_name) for this city.'
        )

    wanted = cfg.get('boundary_name')
    if wanted:
        matches = [c for c in candidates if wanted.lower() in c[1].lower()]
        if not matches:
            raise SystemExit(
                f'[{city}] ERROR: no admin_level={level} relation name contains '
                f'{wanted!r}. Found: {[c[1] for c in candidates]}'
            )
        geom = unary_union([g for g, _ in matches])
        name = wanted if len(matches) > 1 else matches[0][1]
        print(f'[{city}] boundary: {name!r} (unioned {len(matches)} relation(s))')
    else:
        geom, name = max(candidates, key=lambda c: c[0].area)
        print(f'[{city}] boundary: {name!r}')
    return geom, name


def fetch_place_nodes(city: str, cfg: dict, boundary, place_types: list) -> list:
    """Return [(lon, lat, name), ...] for named place nodes inside the boundary."""
    south, west, north, east = cfg['bbox']
    bbox_str = f'{south},{west},{north},{east}'
    types = '|'.join(place_types)
    osm = query_overpass(f'node["place"~"{types}"]({bbox_str});\nout body;')
    seeds = []
    for el in osm['elements']:
        if el['type'] != 'node':
            continue
        name = el.get('tags', {}).get('name')
        if not name:
            continue  # drop unnamed (?) nodes
        lon, lat = el['lon'], el['lat']
        if boundary.contains(Point(lon, lat)):
            seeds.append((lon, lat, name))
    return seeds


# Trailing locality descriptors that distinguish sub-units of one neighborhood:
# "Koramangala 5th Block", "JP Nagar 3rd Phase", "BTM 2nd Stage", "Sector 4".
# Collapsing them merges sub-units into the parent name. Note: this won't merge
# variants like "BTM" vs "BTM Layout" (different base names) — acceptable.
_DESCRIPTOR = r'(?:Block|Stage|Phase|Sector|Main|Cross)'
_ORDINAL = r'\d+(?:st|nd|rd|th)?'
_COLLAPSE_RES = [
    re.compile(rf'\s+{_ORDINAL}\s+{_DESCRIPTOR}\b.*$', re.IGNORECASE),  # "... 5th Block"
    re.compile(rf'\s+{_DESCRIPTOR}\s+{_ORDINAL}\b.*$', re.IGNORECASE),  # "... Sector 4"
]


def normalize_name(name: str) -> str:
    """Collapse a sub-block/stage/phase/sector name to its parent neighborhood."""
    result = name.strip()
    for pattern in _COLLAPSE_RES:
        result = pattern.sub('', result)
    return re.sub(r'\s+', ' ', result).strip() or name.strip()


def _single_area(boundary, name: str) -> list:
    """Degenerate fallback: the whole municipality as one area."""
    return [{
        'type': 'Feature',
        'properties': {'area_name': name},
        'geometry': mapping(boundary),
    }]


def tessellate_areas(city: str, seeds: list, boundary, boundary_name: str) -> list:
    """Voronoi-tessellate seed points, clip to boundary, union cells by area name.

    Degenerate-seed handling (coverage tracks OSM mapping quality, not city size):
      - 0 or 1 seeds  -> single city-wide area (scipy needs >=2 non-collinear points)
      - <MIN_SEEDS    -> tessellate anyway, but warn the result is coarse
    """
    # Group by normalized name, deduping coincident points (Voronoi errors on them).
    points_by_name: dict[str, list] = {}
    seen = set()
    for lon, lat, raw_name in seeds:
        if (lon, lat) in seen:
            continue
        seen.add((lon, lat))
        points_by_name.setdefault(normalize_name(raw_name), []).append((lon, lat))

    flat = [(lon, lat, name) for name, pts in points_by_name.items() for (lon, lat) in pts]
    if len(flat) < 2:
        print(f'[{city}] only {len(flat)} usable seed(s) — falling back to a single '
              f'city-wide area ({boundary_name!r}).')
        return _single_area(boundary, boundary_name)
    if len(flat) < MIN_SEEDS:
        print(f'[{city}] WARNING: only {len(flat)} seeds (< {MIN_SEEDS}) — the Voronoi '
              f'tessellation will be coarse and cells may not match real neighborhoods.')

    coords = [(lon, lat) for lon, lat, _ in flat]
    # Four distant bounding points make every real seed's region finite, so we can
    # build each cell as a finite Polygon and clip it to the boundary.
    minx, miny, maxx, maxy = boundary.bounds
    span = max(maxx - minx, maxy - miny) or 1.0
    far = [(minx - span, miny - span), (maxx + span, miny - span),
           (minx - span, maxy + span), (maxx + span, maxy + span)]
    vor = Voronoi(coords + far)

    cells_by_name: dict[str, list] = {}
    for i, (_, _, name) in enumerate(flat):
        verts = vor.regions[vor.point_region[i]]
        if not verts or -1 in verts:
            continue
        cell = Polygon(vor.vertices[verts]).buffer(0).intersection(boundary)
        if not cell.is_empty:
            cells_by_name.setdefault(name, []).append(cell)

    features = []
    for name, cells in cells_by_name.items():
        geom = unary_union(cells)
        if geom.is_empty:
            continue
        features.append({
            'type': 'Feature',
            'properties': {'area_name': name},
            'geometry': mapping(geom),
        })
    if not features:  # every cell clipped away (shouldn't happen) — stay functional
        return _single_area(boundary, boundary_name)
    return features


def fetch_city(city: str, cfg: dict) -> None:
    out_path = BOUNDARIES_DIR / f'{city}_areas.geojson'
    boundary, boundary_name = fetch_boundary(city, cfg)

    place_types = cfg.get('place_types', DEFAULT_PLACE_TYPES)
    time.sleep(5)  # be polite to Overpass between queries
    seeds = fetch_place_nodes(city, cfg, boundary, place_types)
    print(f'[{city}] {len(seeds)} named place nodes inside boundary '
          f'({"/".join(place_types)})')
    if len(seeds) < MIN_SEEDS and place_types != BROAD_PLACE_TYPES:
        print(f'[{city}] sparse — escalating to broader place types ...')
        time.sleep(5)
        seeds = fetch_place_nodes(city, cfg, boundary, BROAD_PLACE_TYPES)
        print(f'[{city}] {len(seeds)} named place nodes after escalation')

    features = tessellate_areas(city, seeds, boundary, boundary_name)
    fc = {'type': 'FeatureCollection', 'features': features}
    print(f'[{city}] produced {len(features)} area(s)')

    os.makedirs(BOUNDARIES_DIR, exist_ok=True)
    with open(out_path, 'w') as f:
        json.dump(fc, f)
    print(f'[{city}] saved → {out_path}')


def main():
    parser = argparse.ArgumentParser(
        description='Fetch citizen-recognizable neighborhood boundaries from OpenStreetMap.'
    )
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
