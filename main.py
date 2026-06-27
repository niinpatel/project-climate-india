import argparse
import json
import os
from pathlib import Path

import ee
from google.oauth2 import service_account

KEY_PATH = Path(__file__).parent / 'service-account-key.json'
PROJECT_ID = 'experiments-487610'
DATA_PATH = Path(__file__).parent / 'data.json'

RURAL_BUFFER_METERS = 10000
REDUCE_SCALE = 30

# IGBP LC_Type1 classes kept as "rural" reference (vegetation/cropland/forest).
RURAL_LC_CLASSES = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 14]


def authenticate():
    credentials = service_account.Credentials.from_service_account_file(KEY_PATH)
    scoped_credentials = credentials.with_scopes(['https://www.googleapis.com/auth/cloud-platform'])
    ee.Initialize(credentials=scoped_credentials, project=PROJECT_ID)


def load_wards(city: str) -> ee.FeatureCollection:
    with open(Path(__file__).parent / 'boundaries' / f'{city}_wards.geojson') as f:
        geojson = json.load(f)
    return ee.FeatureCollection(geojson)


def build_rural_mask(city_polygon: ee.Geometry, year: int):
    rural_ring = city_polygon.buffer(RURAL_BUFFER_METERS).difference(city_polygon)

    collection = ee.ImageCollection('MODIS/061/MCD12Q1')
    year_collection = collection.filterDate(f'{year}-01-01', f'{year + 1}-01-01')
    lulc = ee.Image(
        ee.Algorithms.If(
            year_collection.size().gt(0),
            year_collection.first(),
            collection.sort('system:time_start', False).first(),
        )
    ).select('LC_Type1')
    lulc_mask = lulc.remap(RURAL_LC_CLASSES, [1] * len(RURAL_LC_CLASSES), 0).eq(1)

    elevation = ee.Image('USGS/SRTMGL1_003').select('elevation')
    elevation_stats = elevation.reduceRegion(
        reducer=ee.Reducer.mean().combine(ee.Reducer.stdDev(), sharedInputs=True),
        geometry=rural_ring,
        scale=REDUCE_SCALE,
        maxPixels=1e9,
    )
    elevation_threshold = ee.Number(elevation_stats.get('elevation_mean')).add(
        ee.Number(elevation_stats.get('elevation_stdDev')).multiply(2)
    )
    elevation_mask = elevation.lte(elevation_threshold)

    rural_mask = lulc_mask.And(elevation_mask).clip(rural_ring)
    return rural_mask, rural_ring


def apply_cloud_mask(image: ee.Image) -> ee.Image:
    qa = image.select('QA_PIXEL')
    cloud = qa.bitwiseAnd(1 << 3).eq(0)
    cloud_shadow = qa.bitwiseAnd(1 << 4).eq(0)
    return image.updateMask(cloud.And(cloud_shadow))


def lst_celsius(image: ee.Image) -> ee.Image:
    lst = image.select('ST_B10').multiply(0.00341802).add(149.0).subtract(273.15)
    return lst.rename('LST')


def monthly_composite(bounds: ee.Geometry, year: int, month: int) -> ee.Image:
    start = f'{year}-{month:02d}-01'
    next_year, next_month = _shift_month(year, month, 1)
    end = f'{next_year}-{next_month:02d}-01'
    collection = (
        ee.ImageCollection('LANDSAT/LC08/C02/T1_L2')
        .filterBounds(bounds)
        .filterDate(start, end)
        .map(apply_cloud_mask)
        .map(lst_celsius)
    )
    return collection.median()


def _baseline_ee(composite: ee.Image, rural_mask: ee.Image, rural_ring: ee.Geometry) -> ee.Number:
    has_bands = composite.bandNames().size().gt(0)
    result = ee.Dictionary(
        ee.Algorithms.If(
            has_bands,
            composite.updateMask(rural_mask).reduceRegion(
                reducer=ee.Reducer.median(),
                geometry=rural_ring,
                scale=REDUCE_SCALE,
                maxPixels=1e9,
            ),
            ee.Dictionary({'LST': None}),
        )
    )
    return ee.Number(result.get('LST'))


def _ward_fc_ee(
    composite: ee.Image,
    wards_fc: ee.FeatureCollection,
    baseline: ee.Number,
    month: int,
) -> ee.FeatureCollection:
    def tag(f):
        return f.set('month_num', month, 'rural_baseline', baseline)

    def tag_null(f):
        return f.set('month_num', month, 'rural_baseline', baseline, 'LST', None)

    has_bands = composite.bandNames().size().gt(0)
    return ee.FeatureCollection(
        ee.Algorithms.If(
            has_bands,
            composite.reduceRegions(
                collection=wards_fc,
                # setOutputs names the column 'LST'; reduceRegions otherwise names
                # a single-band output after the reducer ('median'), unlike reduceRegion.
                reducer=ee.Reducer.median().setOutputs(['LST']),
                scale=REDUCE_SCALE,
            ).map(tag),
            wards_fc.map(tag_null),
        )
    )


def _shift_month(year: int, month: int, delta: int):
    total = year * 12 + (month - 1) + delta
    return total // 12, total % 12 + 1


def merge_into_dataset(existing: list, city: str, year: int, month: int, baseline: float, wards: list) -> list:
    month_key = f'{year}-{month:02d}'
    existing = [e for e in existing if not (e['city'] == city and e['month'] == month_key)]

    prev_year, prev_month = _shift_month(year, month, -1)
    prev_month_key = f'{prev_year}-{prev_month:02d}'
    yoy_key = f'{year - 1}-{month:02d}'

    prev_entry = next((e for e in existing if e['city'] == city and e['month'] == prev_month_key), None)
    yoy_entry = next((e for e in existing if e['city'] == city and e['month'] == yoy_key), None)
    prev_by_ward = {w['ward_name']: w['suhi_score'] for w in prev_entry['wards']} if prev_entry else {}
    yoy_by_ward = {w['ward_name']: w['suhi_score'] for w in yoy_entry['wards']} if yoy_entry else {}

    enriched_wards = []
    for w in wards:
        prev_score = prev_by_ward.get(w['ward_name'])
        yoy_score = yoy_by_ward.get(w['ward_name'])
        enriched_wards.append({
            **w,
            'mom_change': w['suhi_score'] - prev_score if (w['suhi_score'] is not None and prev_score is not None) else None,
            'yoy_change': w['suhi_score'] - yoy_score if (w['suhi_score'] is not None and yoy_score is not None) else None,
        })

    existing.append({
        'city': city,
        'month': month_key,
        'rural_baseline_celsius': baseline,
        'wards': enriched_wards,
    })
    return existing


def load_dataset() -> list:
    try:
        with open(DATA_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_dataset(dataset: list):
    tmp_path = DATA_PATH.with_name(DATA_PATH.name + '.tmp')
    with open(tmp_path, 'w') as f:
        json.dump(dataset, f, indent=2)
    os.replace(tmp_path, DATA_PATH)


def main():
    parser = argparse.ArgumentParser(description='Compute monthly SUHI intensity per ward.')
    parser.add_argument('--city', required=True)
    parser.add_argument('--year', required=True, type=int)
    args = parser.parse_args()

    authenticate()
    wards_fc = load_wards(args.city)
    bounds = wards_fc.geometry().dissolve()
    rural_mask, rural_ring = build_rural_mask(bounds, args.year)

    dataset = load_dataset()

    # Build the full EE computation graph for all 12 months without any getInfo calls.
    combined_fc = None
    for month in range(1, 13):
        composite = monthly_composite(bounds, args.year, month)
        baseline = _baseline_ee(composite, rural_mask, rural_ring)
        month_fc = _ward_fc_ee(composite, wards_fc, baseline, month)
        combined_fc = month_fc if combined_fc is None else combined_fc.merge(month_fc)

    # Single bulk getInfo — replaces 24 sequential blocking calls.
    wards_by_month: dict[int, list] = {m: [] for m in range(1, 13)}
    baselines_by_month: dict[int, float | None] = {}
    for feature in combined_fc.getInfo()['features']:
        props = feature['properties']
        month = int(props['month_num'])
        median_lst = props.get('LST')
        baseline = props.get('rural_baseline')
        baselines_by_month[month] = baseline
        suhi = (median_lst - baseline) if (median_lst is not None and baseline is not None) else None
        wards_by_month[month].append({
            'ward_name': props.get('ward_name', ''),
            'median_lst': median_lst,
            'suhi_score': suhi,
        })

    for month in range(1, 13):
        baseline = baselines_by_month.get(month)
        wards = wards_by_month[month]
        dataset = merge_into_dataset(dataset, args.city, args.year, month, baseline, wards)
        baseline_str = f'{baseline:.2f}C' if baseline is not None else 'N/A'
        print(f'{args.city} {args.year}-{month:02d}: baseline={baseline_str}, {len(wards)} wards')
    save_dataset(dataset)


if __name__ == '__main__':
    main()
