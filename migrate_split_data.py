"""One-off migration: split the monolithic data.json into a city/year tree.

Reads the legacy ``data.json`` array and writes one ``data/{city}/{year}.json``
file per (city, year) group, then rebuilds the manifest. Non-destructive — the
original ``data.json`` is left untouched so the result can be verified before it
is removed.

    python migrate_split_data.py
"""

import json
from collections import defaultdict
from pathlib import Path

import data_store

LEGACY_PATH = Path(__file__).parent / 'data.json'


def main():
    with open(LEGACY_PATH) as f:
        records = json.load(f)

    groups: dict[tuple[str, int], list] = defaultdict(list)
    for record in records:
        year = int(record['month'][:4])
        # Drop the legacy per-ward mom_change/yoy_change fields — the pipeline no
        # longer produces them.
        for ward in record['wards']:
            ward.pop('mom_change', None)
            ward.pop('yoy_change', None)
        groups[(record['city'], year)].append(record)

    for (city, year), group in sorted(groups.items()):
        # Keep months ordered within each file.
        group.sort(key=lambda r: r['month'])
        data_store.save_city_year(city, year, group)
        print(f'wrote {data_store.city_year_path(city, year).relative_to(Path(__file__).parent)} '
              f'({len(group)} months)')

    manifest = data_store.rebuild_manifest()
    print(f'rebuilt manifest: {len(manifest["cities"])} cities')


if __name__ == '__main__':
    main()
