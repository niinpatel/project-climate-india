# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A data pipeline that computes monthly **Surface Urban Heat Island (SUHI)** intensity per citizen-recognizable neighborhood ("area") for major Indian cities. For each city/year it derives, per area per month, the mean daytime land-surface temperature (LST) across the area — a spatial mean over the area's pixels of a per-pixel monthly-**median** Landsat 8 composite — and the SUHI score = area LST − a rural baseline. Results are committed to the repo as JSON under `data/`.

## Commands

The project uses `uv` (Python 3.14, pinned in `.python-version`).

```bash
uv sync                                    # install dependencies from uv.lock
uv run main.py --city mumbai --year 2023   # run the SUHI pipeline for one city/year
uv run main.py --city delhi --year 2024 --keep-export   # keep the GCS CSV instead of deleting it
uv run fetch_boundaries.py                 # (re)fetch all area boundaries from OSM
uv run fetch_boundaries.py --city mumbai   # fetch one city's boundaries
```

There are no tests, linter, or build step. The pipeline is the entrypoint.

## Architecture

Three modules, run manually:

- **`fetch_boundaries.py`** — One-time/occasional. Builds recognizable-neighborhood polygons and writes `boundaries/{city}_areas.geojson`. Per city it fetches the municipal **boundary** relation (OSM `admin_level`, default 8) and the `place` **nodes** inside it (`suburb`/`neighbourhood`/`quarter`), collapses sub-block/stage/phase names to their parent (`Koramangala 5th Block` → `Koramangala`), then **Voronoi-tessellates** the seed points (scipy) and clips each cell to the boundary (shapely), unioning cells that share a name. Admin ward relations are *not* used — they're modeled inconsistently across Indian cities and don't match colloquial neighborhoods. Per-city config (`bbox`, optional `boundary_admin_level`/`boundary_name`/`place_types`) lives in the `CITIES` dict. The reused `assemble_rings`/`relation_to_feature` chain raw OSM way segments into the boundary polygon.

- **`main.py`** — The SUHI pipeline. Flow: authenticate to Earth Engine + GCS with the service account → `verify_bucket` (fails fast before the expensive graph build) → load area GeoJSON → build a rural reference mask → construct one Earth Engine computation graph spanning all 12 months → kick off an **EE batch export to Cloud Storage** → poll until terminal → download/parse the CSV → save to `data/`.

- **`data_store.py`** — Storage layer. `save_city_year` writes `data/{city}/{year}.json` (atomic via tmp+rename); `rebuild_manifest` rescans the whole `data/` tree to regenerate `data/index.json`. The manifest is derived purely from files on disk, so hand-edits are safe.

### Why the batch-export indirection

A full year of 30 m Landsat over a large city blows past Earth Engine's synchronous 5-minute `getInfo()` cap. So `main.py` builds the entire 12-month graph **without any `getInfo` calls**, hands it to a background EE batch task (`ee.batch.Export.table.toCloudStorage`) that runs on Google's cloud, then downloads the resulting CSV. The CSV is a transient artifact, deleted after a successful local save unless `--keep-export` is passed. `EXPORT_SELECTORS` restricts exported columns to drop per-feature geometry and keep the CSV small.

### SUHI computation details (constants at top of `main.py`)

- **Rural baseline** = mean LST over a ring `RURAL_BUFFER_METERS` (10 km) beyond the city, masked to ESA WorldCover "rural" classes (`RURAL_LC_CLASSES` = tree/shrub/grass) and to pixels at/below mean+2σ elevation (excludes hills).
- **Cloud masking** uses Landsat `QA_PIXEL` bits for cloud, cloud shadow, **and water** (water is masked so coastal/lake pixels don't bias land-surface LST).
- **Coverage gate**: per area/month, if the cloud-free pixel fraction is below `COVERAGE_THRESHOLD` (0.10), the LST is nulled rather than trusted. LST values outside `LST_MIN_C..LST_MAX_C` are masked as cloud/fill.
- **City-wide coverage gate**: the per-area gate is judged region-by-region, so a mostly-clouded month can still leave a few areas above 0.10 whose surviving pixels are cloud-edge contamination. So per month the cloud-free fraction over the *whole-city* footprint is also measured; if it's below `CITY_COVERAGE_THRESHOLD` (0.30), the entire month is nulled (all area LST/SUHI **and** the rural baseline), which also keeps the anomaly guard below from misreading the all-null result as a bug.
- **Anomaly guard**: if a month has a valid rural baseline (so the composite had usable pixels) but *zero* areas with LST, the run aborts and refuses to overwrite existing data — that pattern signals a processing bug, not cloud cover.

### Known limitation: cold cloud-edge contamination in partially-clouded months

Both coverage gates count *how many* pixels survive masking, not whether their LST *values* are physically reasonable. In partially-clouded months (typically the monsoon shoulder, e.g. **bengaluru 2024-05**, and to a lesser degree bengaluru/delhi 2024-08), cloud-edge pixels can pass the `QA_PIXEL` mask and fall inside `LST_MIN_C..LST_MAX_C` while reading far too cold. When such a month has enough surviving pixels to clear both `COVERAGE_THRESHOLD` (per area) and `CITY_COVERAGE_THRESHOLD` (city-wide), it is **not** gated, so implausibly low `mean_lst`/`suhi_score` values (e.g. a 10 °C area mean in a month whose neighbors are ~40 °C) can land in the committed data. This is a value-quality problem the coverage gates are not designed to catch. A future guard could flag months where an area's LST deviates implausibly from its neighbors or from the seasonal trend; until then, treat low-coverage non-null months (check the per-month `city_coverage` log line) with suspicion.

### Known limitation: area granularity tracks OSM mapping density, not city size

`fetch_boundaries.py` is only as good as the `place` nodes mapped in OSM. Density does **not** scale with population: a densely-mapped metro yields hundreds of areas (Bengaluru ≈ 1091 seeds), while a populous but under-mapped tier-2 city can yield a handful (Indore ≈ 7) and a small well-mapped town many more (Bhuj ≈ 133). When the in-boundary seed count is below `MIN_SEEDS` (12), the fetch escalates to broader place types (`town`/`village`/`hamlet`) and warns that the tessellation is coarse; with 0–1 usable seeds it falls back to a **single city-wide area** (so the pipeline stays functional rather than crashing or emitting fake neighborhoods). The five configured cities are all densely-mapped metros, which is the validated regime.

### Data shape

`data/{city}/{year}.json` is a JSON array of 12 monthly records:
```json
{ "city": "mumbai", "month": "2023-01", "rural_baseline_celsius": 30.86,
  "areas": [ { "area_name": "...", "mean_lst": 31.24, "suhi_score": 0.38 } ] }
```
`mean_lst` (area spatial-mean LST, °C) / `suhi_score` / `rural_baseline_celsius` are `null` when cloud cover left too few usable pixels — including when the city-wide coverage gate nulls the whole month.

## Configuration / credentials

- `service-account-key.json` (gitignored) holds the GCS + Earth Engine service-account credentials. `PROJECT_ID` and `DEFAULT_BUCKET` are constants in `main.py`. The service account needs `roles/storage.objectAdmin` on the export bucket.
- `--bucket` overrides the default GCS bucket; the bucket must already exist (EE won't create it).
