# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A data pipeline that computes monthly **Surface Urban Heat Island (SUHI)** intensity per administrative ward for major Indian cities. For each city/year it derives, per ward per month, the median daytime land-surface temperature (LST) from Landsat 8 and the SUHI score = ward LST − a rural baseline. Results are committed to the repo as JSON under `data/`.

## Commands

The project uses `uv` (Python 3.14, pinned in `.python-version`).

```bash
uv sync                                    # install dependencies from uv.lock
uv run main.py --city mumbai --year 2023   # run the SUHI pipeline for one city/year
uv run main.py --city delhi --year 2024 --keep-export   # keep the GCS CSV instead of deleting it
uv run fetch_boundaries.py                 # (re)fetch all ward boundaries from OSM
uv run fetch_boundaries.py --city mumbai   # fetch one city's boundaries
```

There are no tests, linter, or build step. The pipeline is the entrypoint.

## Architecture

Three modules, run manually:

- **`fetch_boundaries.py`** — One-time/occasional. Pulls administrative ward polygons from the OpenStreetMap Overpass API and writes `boundaries/{city}_wards.geojson`. City bboxes, expected ward counts, and OSM `admin_level` to query live in the `CITIES` dict at the top — edit there to add a city or fix a boundary mismatch. Ring assembly chains raw OSM way segments into closed polygons.

- **`main.py`** — The SUHI pipeline. Flow: authenticate to Earth Engine + GCS with the service account → `verify_bucket` (fails fast before the expensive graph build) → load ward GeoJSON → build a rural reference mask → construct one Earth Engine computation graph spanning all 12 months → kick off an **EE batch export to Cloud Storage** → poll until terminal → download/parse the CSV → save to `data/`.

- **`data_store.py`** — Storage layer. `save_city_year` writes `data/{city}/{year}.json` (atomic via tmp+rename); `rebuild_manifest` rescans the whole `data/` tree to regenerate `data/index.json`. The manifest is derived purely from files on disk, so hand-edits are safe.

### Why the batch-export indirection

A full year of 30 m Landsat over a large city blows past Earth Engine's synchronous 5-minute `getInfo()` cap. So `main.py` builds the entire 12-month graph **without any `getInfo` calls**, hands it to a background EE batch task (`ee.batch.Export.table.toCloudStorage`) that runs on Google's cloud, then downloads the resulting CSV. The CSV is a transient artifact, deleted after a successful local save unless `--keep-export` is passed. `EXPORT_SELECTORS` restricts exported columns to drop per-feature geometry and keep the CSV small.

### SUHI computation details (constants at top of `main.py`)

- **Rural baseline** = mean LST over a ring `RURAL_BUFFER_METERS` (10 km) beyond the city, masked to ESA WorldCover "rural" classes (`RURAL_LC_CLASSES` = tree/shrub/grass) and to pixels at/below mean+2σ elevation (excludes hills).
- **Cloud masking** uses Landsat `QA_PIXEL` bits for cloud, cloud shadow, **and water** (water is masked so coastal/lake pixels don't bias land-surface LST).
- **Coverage gate**: per ward/month, if the cloud-free pixel fraction is below `COVERAGE_THRESHOLD` (0.10), the LST is nulled rather than trusted. LST values outside `LST_MIN_C..LST_MAX_C` are masked as cloud/fill.
- **Anomaly guard**: if a month has a valid rural baseline (so the composite had usable pixels) but *zero* wards with LST, the run aborts and refuses to overwrite existing data — that pattern signals a processing bug, not cloud cover.

### Data shape

`data/{city}/{year}.json` is a JSON array of 12 monthly records:
```json
{ "city": "mumbai", "month": "2023-01", "rural_baseline_celsius": 30.86,
  "wards": [ { "ward_name": "...", "median_lst": 31.24, "suhi_score": 0.38 } ] }
```
`median_lst` / `suhi_score` / `rural_baseline_celsius` are `null` when cloud cover left too few usable pixels.

## Configuration / credentials

- `service-account-key.json` (gitignored) holds the GCS + Earth Engine service-account credentials. `PROJECT_ID` and `DEFAULT_BUCKET` are constants in `main.py`. The service account needs `roles/storage.objectAdmin` on the export bucket.
- `--bucket` overrides the default GCS bucket; the bucket must already exist (EE won't create it).
