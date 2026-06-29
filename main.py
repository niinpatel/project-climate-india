import argparse
import csv
import io
import json
import time
from pathlib import Path

import ee
from google.api_core import exceptions as gcs_exceptions
from google.cloud import storage
from google.oauth2 import service_account

import data_store

KEY_PATH = Path(__file__).parent / 'service-account-key.json'
PROJECT_ID = 'experiments-487610'

# Default Cloud Storage bucket (in PROJECT_ID) for the batch export. Override with --bucket.
DEFAULT_BUCKET = 'suhi-bucket'

RURAL_BUFFER_METERS = 10000
REDUCE_SCALE = 30

# Default Cloud Storage prefix (folder) under the bucket where Earth Engine writes
# the exported CSVs. The per-run object is f'{GCS_PREFIX}/{city}_{year}.csv'.
GCS_PREFIX = 'suhi_exports'

# How often to poll the Earth Engine batch task while it runs on Google's cloud.
EXPORT_POLL_SECONDS = 15

# Earth Engine batch-task states that are final (the task will not change again).
TERMINAL_TASK_STATES = {'COMPLETED', 'FAILED', 'CANCELLED'}

# Columns to export from the merged FeatureCollection. Restricting to these drops
# the per-feature geometry (the default '.geo' column), keeping the CSV small.
EXPORT_SELECTORS = ['ward_name', 'month_num', 'rural_baseline', 'city_coverage', 'LST']

# Minimum fraction of cloud-free pixels required to trust an aggregated LST value.
# Below this, the median is computed over too few pixels to be meaningful (cloud
# contamination), so the value is treated as null.
COVERAGE_THRESHOLD = 0.10

# The per-ward/per-baseline COVERAGE_THRESHOLD is judged region-by-region, so a month
# where most of the city is clouded can still leave a handful of wards individually
# above 10% — and those surviving pixels are typically cloud-edge contamination that
# biases LST cold. This gate looks at the whole-city footprint: if less than this
# fraction of the city was cloud-free, the entire month is nulled rather than trusted.
CITY_COVERAGE_THRESHOLD = 0.30

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
    # Returned so the Cloud Storage client below can reuse the same service-account
    # credentials to download the exported CSV.
    return scoped_credentials


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
    # SUHI is a land-surface metric; mask water (bit 7) so sea/lake pixels in
    # coastal ward polygons don't bias the LST aggregates.
    water = qa.bitwiseAnd(1 << 7).eq(0)
    return image.updateMask(cloud.And(cloud_shadow).And(water))


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


def _city_coverage_ee(composite: ee.Image, bounds: ee.Geometry) -> ee.Number:
    """Fraction of the whole-city footprint that is cloud-free this month (0..1).

    Mirrors the per-ward/baseline coverage measure (mean of the 0/1 cloud-free band)
    but over the entire city geometry, so it captures city-wide cloudiness even when
    individual wards happen to clear COVERAGE_THRESHOLD. Returns 0 when the composite
    has no bands (no usable scenes), which the city gate treats as a fully-clouded month.
    """
    def _measure():
        cloud_free = composite.select('LST').mask().rename('cloud_free')
        stats = cloud_free.reduceRegion(
            reducer=ee.Reducer.mean(),
            geometry=bounds,
            scale=REDUCE_SCALE,
            maxPixels=1e9,
        )
        return ee.Number(ee.Algorithms.If(stats.get('cloud_free'), stats.get('cloud_free'), 0))

    has_bands = composite.bandNames().size().gt(0)
    return ee.Number(ee.Algorithms.If(has_bands, _measure(), 0))


def _ward_fc_ee(
    composite: ee.Image,
    wards_fc: ee.FeatureCollection,
    baseline: ee.Number,
    city_coverage: ee.Number,
    month: int,
) -> ee.FeatureCollection:
    def tag_null(f):
        return f.set('month_num', month, 'rural_baseline', baseline,
                     'city_coverage', city_coverage, 'LST', None)

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
            return f.set('month_num', month, 'rural_baseline', baseline,
                         'city_coverage', city_coverage, 'LST', lst_val)

        return fc.map(finalize)

    has_bands = composite.bandNames().size().gt(0)
    return ee.FeatureCollection(
        ee.Algorithms.If(has_bands, _measure(), wards_fc.map(tag_null))
    )


def _shift_month(year: int, month: int, delta: int):
    total = year * 12 + (month - 1) + delta
    return total // 12, total % 12 + 1


def verify_bucket(client: storage.Client, bucket_name: str) -> None:
    """Fail fast if the export bucket is missing or not writable.

    Earth Engine's toCloudStorage will not create the bucket, and a permission or
    typo problem otherwise only surfaces ~minutes later as a FAILED task — after the
    whole year of Landsat has been recomputed. Checking up front turns that into an
    immediate, actionable error.

    The probe uses test_iam_permissions on the object-level permissions the pipeline
    actually uses (export writes, download reads, cleanup deletes). It deliberately
    avoids bucket.exists(), which needs storage.buckets.get — a permission that
    roles/storage.objectAdmin (the role this pipeline requires) does NOT grant, so
    checking it would false-fail on a correctly configured bucket.
    """
    bucket = client.bucket(bucket_name)
    needed = ['storage.objects.create', 'storage.objects.delete', 'storage.objects.get']
    try:
        granted = set(bucket.test_iam_permissions(needed))
    except gcs_exceptions.NotFound:
        raise SystemExit(
            f'ERROR: bucket gs://{bucket_name} does not exist.\n'
            f'  Create it:  gcloud storage buckets create gs://{bucket_name} '
            f'--project {PROJECT_ID}\n'
            f'  Then grant the service account roles/storage.objectAdmin on it.'
        )
    except gcs_exceptions.GoogleAPICallError as e:  # auth/network/project misconfiguration
        raise SystemExit(
            f'ERROR: could not reach Cloud Storage in project {PROJECT_ID}: {e}'
        )
    missing = [p for p in needed if p not in granted]
    if missing:
        raise SystemExit(
            f'ERROR: the service account lacks {", ".join(missing)} on '
            f'gs://{bucket_name}. Grant it roles/storage.objectAdmin on the bucket.'
        )


def start_export(combined_fc: ee.FeatureCollection, bucket: str, prefix: str,
                 city: str, year: int) -> ee.batch.Task:
    """Kick off an Earth Engine batch export of the merged collection to GCS.

    Returns the started task. The computation runs on Google's cloud, so it is not
    subject to the 5-minute getInfo() timeout that a year of 30 m Landsat data would
    otherwise blow past for a large city.
    """
    task = ee.batch.Export.table.toCloudStorage(
        collection=combined_fc,
        description=f'suhi_{city}_{year}',
        bucket=bucket,
        fileNamePrefix=prefix,
        fileFormat='CSV',
        selectors=EXPORT_SELECTORS,
    )
    task.start()
    return task


def wait_for_export(task: ee.batch.Task) -> dict:
    """Block until the export task reaches a terminal state, then return its status."""
    while True:
        status = task.status()
        state = status['state']
        if state in TERMINAL_TASK_STATES:
            return status
        print(f'  export {state.lower()}...')
        time.sleep(EXPORT_POLL_SECONDS)


def download_export_csv(client: storage.Client, bucket: str, prefix: str) -> str:
    """Return the text of the exported CSV.

    Earth Engine writes the table to ``{prefix}.csv``. We resolve it via the prefix
    so a (rare) sharded export — ``{prefix}ee-1.csv`` etc. — is still picked up; the
    blobs are concatenated with the duplicated header rows dropped.
    """
    blobs = sorted(
        (b for b in client.list_blobs(bucket, prefix=prefix) if b.name.endswith('.csv')),
        key=lambda b: b.name,
    )
    if not blobs:
        raise SystemExit(
            f'ERROR: export task reported COMPLETED but no CSV was found at '
            f'gs://{bucket}/{prefix}*.csv'
        )

    parts = [blob.download_as_text() for blob in blobs]
    if len(parts) == 1:
        return parts[0]
    lines = parts[0].splitlines()
    header = lines[0]
    for part in parts[1:]:
        rows = part.splitlines()
        if rows and rows[0] == header:
            rows = rows[1:]
        lines.extend(rows)
    return '\n'.join(lines) + '\n'


def _to_float(value):
    """Parse a CSV cell to float, treating empty/null cells as None."""
    if value is None or value == '':
        return None
    try:
        return float(value)
    except ValueError:
        return None


def parse_export_csv(text: str):
    """Group exported rows into per-month ward lists and per-month rural baselines.

    Mirrors the structure the old getInfo() loop produced, so the downstream
    record-building and anomaly checks are unchanged.
    """
    wards_by_month: dict[int, list] = {m: [] for m in range(1, 13)}
    baselines_by_month: dict[int, float | None] = {}
    coverage_by_month: dict[int, float | None] = {}
    for row in csv.DictReader(io.StringIO(text)):
        month = int(float(row['month_num']))
        # Per-ward value is the spatial mean (Reducer.mean) over the ward's pixels of
        # the per-pixel monthly-median composite — i.e. a mean, hence the field name.
        mean_lst = _to_float(row.get('LST'))
        baseline = _to_float(row.get('rural_baseline'))
        baselines_by_month[month] = baseline
        coverage_by_month[month] = _to_float(row.get('city_coverage'))
        suhi = (mean_lst - baseline) if (mean_lst is not None and baseline is not None) else None
        wards_by_month[month].append({
            'ward_name': row.get('ward_name', '') or '',
            'mean_lst': mean_lst,
            'suhi_score': suhi,
        })
    return wards_by_month, baselines_by_month, coverage_by_month


def main():
    parser = argparse.ArgumentParser(description='Compute monthly SUHI intensity per ward.')
    parser.add_argument('--city', required=True)
    parser.add_argument('--year', required=True, type=int)
    parser.add_argument('--bucket', default=DEFAULT_BUCKET,
                        help='Cloud Storage bucket (in project %s) for the batch export '
                             '(default: %s).' % (PROJECT_ID, DEFAULT_BUCKET))
    parser.add_argument('--keep-export', action='store_true',
                        help='Keep the exported CSV in the bucket (default: delete it after a successful local save).')
    args = parser.parse_args()

    credentials = authenticate()

    # Validate the export destination before the expensive 12-month graph build, so a
    # missing/typo'd/unwritable bucket fails in seconds instead of after the export runs.
    storage_client = storage.Client(project=PROJECT_ID, credentials=credentials)
    verify_bucket(storage_client, args.bucket)

    wards_fc = load_wards(args.city)
    bounds = wards_fc.geometry().dissolve()
    rural_mask, rural_ring = build_rural_mask(bounds, args.year)

    # Build the full EE computation graph for all 12 months without any getInfo calls.
    combined_fc = None
    for month in range(1, 13):
        composite = monthly_composite(bounds, args.year, month)
        baseline = _baseline_ee(composite, rural_mask, rural_ring)
        city_coverage = _city_coverage_ee(composite, bounds)
        month_fc = _ward_fc_ee(composite, wards_fc, baseline, city_coverage, month)
        combined_fc = month_fc if combined_fc is None else combined_fc.merge(month_fc)

    # Hand the year of 30 m Landsat work to a background batch task on Google's
    # cloud instead of forcing it through a synchronous (5-min-capped) getInfo().
    prefix = f'{GCS_PREFIX}/{args.city}_{args.year}'
    print(f'Starting Earth Engine export to gs://{args.bucket}/{prefix}.csv ...')
    task = start_export(combined_fc, args.bucket, prefix, args.city, args.year)
    print(f'  task id: {task.id}')
    status = wait_for_export(task)
    if status['state'] != 'COMPLETED':
        raise SystemExit(
            f'ERROR: export task {task.id} ended in state {status["state"]}: '
            f'{status.get("error_message", "no error message")}'
        )

    csv_text = download_export_csv(storage_client, args.bucket, prefix)
    wards_by_month, baselines_by_month, coverage_by_month = parse_export_csv(csv_text)

    records = []
    anomalies = []
    for month in range(1, 13):
        baseline = baselines_by_month.get(month)
        wards = wards_by_month[month]
        coverage = coverage_by_month.get(month)
        # City-wide coverage gate: when too little of the city was cloud-free, the few
        # per-ward values that cleared COVERAGE_THRESHOLD are cloud-edge contamination,
        # not land surface — so null the whole month (baseline included, which keeps the
        # anomaly check below from mistaking the all-null wards for a processing bug).
        gated = coverage is not None and coverage < CITY_COVERAGE_THRESHOLD
        if gated:
            baseline = None
            for w in wards:
                w['mean_lst'] = None
                w['suhi_score'] = None
        records.append({
            'city': args.city,
            'month': f'{args.year}-{month:02d}',
            'rural_baseline_celsius': baseline,
            'wards': wards,
        })
        with_lst = sum(1 for w in wards if w['mean_lst'] is not None)
        baseline_str = f'{baseline:.2f}C' if baseline is not None else 'N/A'
        coverage_str = f'{coverage * 100:.0f}%' if coverage is not None else 'N/A'
        gate_note = '  [GATED: city coverage below threshold]' if gated else ''
        print(f'{args.city} {args.year}-{month:02d}: baseline={baseline_str}, '
              f'city_coverage={coverage_str}, '
              f'{with_lst}/{len(wards)} wards with LST{gate_note}')
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

    # The CSV is a transient artifact this run created; remove it once the results
    # are safely persisted locally, unless the caller asked to keep it.
    if not args.keep_export:
        for blob in storage_client.list_blobs(args.bucket, prefix=prefix):
            if blob.name.endswith('.csv'):
                blob.delete()
    else:
        print(f'Kept export at gs://{args.bucket}/{prefix}.csv')


if __name__ == '__main__':
    main()
