import argparse
import json
from pathlib import Path

import ee
from google.oauth2 import service_account

import data_store

KEY_PATH = Path(__file__).parent / 'service-account-key.json'
PROJECT_ID = 'experiments-487610'

RURAL_BUFFER_METERS = 10000
REDUCE_SCALE = 30

# Minimum fraction of cloud-free pixels required to trust an aggregated LST value.
# Below this, the median is computed over too few pixels to be meaningful (cloud
# contamination), so the value is treated as null.
COVERAGE_THRESHOLD = 0.10

# Physically plausible daytime land-surface-temperature range (Celsius). Pixels
# outside it are cloud/fill values that slip past the QA_PIXEL mask; masking them
# both removes garbage and lets COVERAGE_THRESHOLD null out the affected months.
LST_MIN_C = 0
LST_MAX_C = 65

# ESA WorldCover classes kept as "rural" reference: tree cover, shrubland, grassland.
RURAL_LC_CLASSES = [10, 20, 30]


def authenticate():
    credentials = service_account.Credentials.from_service_account_file(KEY_PATH)
    scoped_credentials = credentials.with_scopes(['https://www.googleapis.com/auth/cloud-platform'])
    ee.Initialize(credentials=scoped_credentials, project=PROJECT_ID)


def load_wards(city: str) -> ee.FeatureCollection:
    with open(Path(__file__).parent / 'boundaries' / f'{city}_wards.geojson') as f:
        geojson = json.load(f)
    return ee.FeatureCollection(geojson)


def _worldcover_image(year: int) -> ee.Image:
    # ESA WorldCover has two epochs: v100 = 2020, v200 = 2021. Each is an
    # ImageCollection holding a single global 10 m mosaic (band 'Map'). Years
    # beyond 2021 reuse v200 (the latest available static layer).
    dataset = 'ESA/WorldCover/v100' if year <= 2020 else 'ESA/WorldCover/v200'
    return ee.ImageCollection(dataset).first().select('Map')


def build_rural_mask(city_polygon: ee.Geometry, year: int):
    rural_ring = city_polygon.buffer(RURAL_BUFFER_METERS).difference(city_polygon)

    lulc = _worldcover_image(year)
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
    lst = lst.updateMask(lst.gte(LST_MIN_C).And(lst.lte(LST_MAX_C)))
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

    def _measure():
        lst = composite.select('LST').updateMask(rural_mask).rename('LST')
        # 0/1 over rural pixels: 1 where cloud-free, 0 where cloudy. Its mean is the
        # fraction of rural pixels that are cloud-free.
        cloud_free = composite.select('LST').mask().updateMask(rural_mask).rename('cloud_free')
        stats = lst.addBands(cloud_free).reduceRegion(
            reducer=ee.Reducer.mean(),
            geometry=rural_ring,
            scale=REDUCE_SCALE,
            maxPixels=1e9,
        )
        coverage = ee.Number(ee.Algorithms.If(stats.get('cloud_free'), stats.get('cloud_free'), 0))
        return ee.Algorithms.If(coverage.gte(COVERAGE_THRESHOLD), stats.get('LST'), None)

    return ee.Number(ee.Algorithms.If(has_bands, _measure(), None))


def _ward_fc_ee(
    composite: ee.Image,
    wards_fc: ee.FeatureCollection,
    baseline: ee.Number,
    month: int,
) -> ee.FeatureCollection:
    def tag_null(f):
        return f.set('month_num', month, 'rural_baseline', baseline, 'LST', None)

    def _measure():
        lst = composite.select('LST').rename('LST')
        # 0/1 over the ward: 1 where cloud-free, 0 where cloudy. Its mean per ward is
        # the fraction of the ward's pixels that are cloud-free.
        cloud_free = composite.select('LST').mask().rename('cloud_free')
        # With multiple bands, reduceRegions names each output column by band name.
        fc = lst.addBands(cloud_free).reduceRegions(
            collection=wards_fc,
            reducer=ee.Reducer.mean(),
            scale=REDUCE_SCALE,
        )

        def finalize(f):
            coverage = ee.Number(ee.Algorithms.If(f.get('cloud_free'), f.get('cloud_free'), 0))
            lst_val = ee.Algorithms.If(coverage.gte(COVERAGE_THRESHOLD), f.get('LST'), None)
            return f.set('month_num', month, 'rural_baseline', baseline, 'LST', lst_val)

        return fc.map(finalize)

    has_bands = composite.bandNames().size().gt(0)
    return ee.FeatureCollection(
        ee.Algorithms.If(has_bands, _measure(), wards_fc.map(tag_null))
    )


def _shift_month(year: int, month: int, delta: int):
    total = year * 12 + (month - 1) + delta
    return total // 12, total % 12 + 1


def main():
    parser = argparse.ArgumentParser(description='Compute monthly SUHI intensity per ward.')
    parser.add_argument('--city', required=True)
    parser.add_argument('--year', required=True, type=int)
    args = parser.parse_args()

    authenticate()
    wards_fc = load_wards(args.city)
    bounds = wards_fc.geometry().dissolve()
    rural_mask, rural_ring = build_rural_mask(bounds, args.year)

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

    records = []
    anomalies = []
    for month in range(1, 13):
        baseline = baselines_by_month.get(month)
        wards = wards_by_month[month]
        records.append({
            'city': args.city,
            'month': f'{args.year}-{month:02d}',
            'rural_baseline_celsius': baseline,
            'wards': wards,
        })
        with_lst = sum(1 for w in wards if w['median_lst'] is not None)
        baseline_str = f'{baseline:.2f}C' if baseline is not None else 'N/A'
        print(f'{args.city} {args.year}-{month:02d}: baseline={baseline_str}, '
              f'{with_lst}/{len(wards)} wards with LST')
        # A non-null baseline means the composite had usable pixels, so every ward
        # being null signals a processing bug (e.g. wrong reduceRegions output name),
        # not cloud cover — which would also leave the baseline null.
        if baseline is not None and wards and with_lst == 0:
            anomalies.append(f'{args.year}-{month:02d}')

    if anomalies:
        raise SystemExit(
            f'ERROR: {args.city} has months with a valid rural baseline but zero wards '
            f'with LST ({", ".join(anomalies)}). This indicates a processing bug, not '
            f'cloud cover. Refusing to overwrite {args.city}/{args.year} with empty results.'
        )

    data_store.save_city_year(args.city, args.year, records)
    data_store.rebuild_manifest()


if __name__ == '__main__':
    main()
