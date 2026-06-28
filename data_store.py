"""Storage layer for SUHI results.

Results are partitioned on disk as ``data/{city}/{year}.json`` — each file is a
JSON array of that city/year's monthly records (the same record shape the
pipeline produces). A top-level ``data/index.json`` manifest lists the available
cities, their ward counts, and which years have data, so a consumer (API or
frontend) can discover what exists without scanning the directory tree.
"""

import json
import os
from pathlib import Path

DATA_DIR = Path(__file__).parent / 'data'
MANIFEST_PATH = DATA_DIR / 'index.json'


def city_year_path(city: str, year: int) -> Path:
    return DATA_DIR / city / f'{year}.json'


def _atomic_write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + '.tmp')
    with open(tmp_path, 'w') as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp_path, path)


def load_city_year(city: str, year: int) -> list:
    """Return the monthly records for one city/year, or [] if none exist."""
    try:
        with open(city_year_path(city, year)) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_city_year(city: str, year: int, records: list) -> None:
    """Atomically write one city/year's monthly records."""
    _atomic_write_json(city_year_path(city, year), records)


def rebuild_manifest() -> dict:
    """Scan the data tree and (re)write data/index.json.

    Order-independent and robust to files added or removed by hand: it derives
    everything from whatever ``data/*/*.json`` files are present.
    """
    cities: dict[str, dict] = {}
    for year_file in sorted(DATA_DIR.glob('*/*.json')):
        city = year_file.parent.name
        try:
            year = int(year_file.stem)
        except ValueError:
            continue  # ignore non-year files
        try:
            with open(year_file) as f:
                records = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        if not records:
            continue
        entry = cities.setdefault(city, {'wards': len(records[0].get('wards', [])), 'years': []})
        entry['years'].append(year)

    for entry in cities.values():
        entry['years'].sort()

    manifest = {'cities': dict(sorted(cities.items()))}
    _atomic_write_json(MANIFEST_PATH, manifest)
    return manifest
