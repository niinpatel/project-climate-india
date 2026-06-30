#!/usr/bin/env bash
# Drive the SUHI pipeline across all configured cities and years.
# Resumable: skips any city/year whose data/{city}/{year}.json already exists.
# Runs sequentially to stay within Earth Engine's concurrent batch-task limit.
set -uo pipefail
cd "$(dirname "$0")"

CITIES=(mumbai delhi bengaluru hyderabad chennai)
YEARS=(2020 2021 2022 2023 2024)

mkdir -p run_logs
SUMMARY=run_logs/summary.log
: > "$SUMMARY"

for city in "${CITIES[@]}"; do
  for year in "${YEARS[@]}"; do
    out="data/${city}/${year}.json"
    if [[ -f "$out" ]]; then
      echo "SKIP  ${city} ${year} (already present)" | tee -a "$SUMMARY"
      continue
    fi
    log="run_logs/${city}_${year}.log"
    echo "START ${city} ${year} -> ${log}" | tee -a "$SUMMARY"
    if uv run main.py --city "$city" --year "$year" >"$log" 2>&1; then
      echo "OK    ${city} ${year}" | tee -a "$SUMMARY"
    else
      echo "FAIL  ${city} ${year} (see ${log})" | tee -a "$SUMMARY"
    fi
  done
done

echo "DONE" | tee -a "$SUMMARY"
