# Urban Heat, from scratch — what we learned & how to rebuild for "lived heat"

> Written after auditing the existing daytime-SUHI pipeline (5 Indian cities, 2020–2024).
> Purpose: capture the hard-won lessons so the *next* version answers the question we
> actually care about — **how hot a neighborhood is to live in** — instead of the question
> the old pipeline answered by accident.

---

## 0. TL;DR

- **The goal was "lived heat." The old pipeline measures daytime *surface* urban heat island
  (SUHI) from Landsat.** Those are not the same thing, and the gap is large enough to invalidate
  the product for its intended use. This is the single most important lesson.
- **The core statistical idea in the old pipeline is sound** and worth keeping: measuring heat as
  a *contrast* against a rural baseline cancels 53–70% of year-to-year weather noise (we measured it).
- **The implementation has three structural weak points**: a fragile single-scalar baseline,
  coverage gates that count pixels instead of checking values, and aggregation that's biased by
  which months survive. All three produced real, wrong numbers in the committed data.
- **For a rebuild, change the question first (lived heat, night-weighted), then change the atomic
  unit (per-scene contrast + uncertainty), then fix the data starvation (fuse sensors).**

---

## 1. The core conceptual mistake: SUHI ≠ lived heat

SUHI seemed like "the recommended option" because it's what's easy to measure from free satellite
imagery. But it answers a different question than "is this neighborhood hot to live in."

### The three different "urban heat" quantities

| Quantity | What it is | Data | Relationship to lived experience |
|---|---|---|---|
| **Surface UHI (SUHI)** | Land *surface* temperature (LST) of the ground/roofs, from thermal satellite | Landsat/MODIS/VIIRS thermal bands | **Weak, especially by day.** Surfaces ≠ air you breathe. |
| **Canopy-layer UHI (air UHI)** | Air temperature at ~2 m, where people actually are | Weather stations, reanalysis, sensor networks | **This is closest to "lived heat."** |
| **Thermal comfort / heat stress** | What the body feels: combines air temp + humidity + radiation + wind | Derived indices (Heat Index, WBGT, UTCI) | **This is lived heat.** |

The old pipeline measures the *first* row. Lived heat is the *third* row.

### Why daytime surface LST is a poor proxy for lived heat — with evidence from our own data

1. **Daytime surface temperature is dominated by surface moisture and material, not "urban-ness."**
   We found the SUHI **collapses to ~0 or goes negative in summer** across most cities. That's not
   a measurement bug — it's real physics: in the pre-monsoon dry season, bare/parched *rural* land
   bakes as hot as (or hotter than) the built city, so "city minus rural" goes negative. A resident
   does *not* experience summer as the coolest-relative season; the metric just inverts because it's
   really measuring a surface-dryness contrast.

2. **Landsat's overpass is ~10:30 in the morning.** Peak human heat exposure is mid-afternoon and,
   critically for health, *overnight* (when the body can't recover). A morning surface snapshot misses both.

3. **Nighttime is where urban heat actually hurts, and it's the opposite story.** The canonical,
   health-relevant UHI is nocturnal: cities stay warm at night while the countryside cools. Daytime
   LST barely sees this. A lived-heat product should be **night-weighted.**

**Takeaway for the rebuild:** decide the estimand *first*. If the product is about lived heat and
heat vulnerability, the primary signal should be **air temperature and nighttime warmth**, with
surface LST used only as a high-resolution *spatial texture* to downscale, not as the headline.

---

## 2. What the old pipeline got RIGHT (keep these ideas)

Don't throw out the good parts. The audit validated:

- **Heat-as-a-contrast (relative, not absolute).** Reporting "this area vs a rural reference"
  rather than raw °C is the right instinct. We measured city LST and rural LST moving together
  year-to-year with **correlation 0.89–0.96**; differencing them cancels that shared weather.
  Result: interannual noise drops from **σ ≈ 1.4–2.7 °C (raw city LST)** to **σ ≈ 0.6–1.1 °C (contrast)**,
  i.e. **53–70% of the noise removed.** Keep the contrast framing for whatever quantity you choose.

- **Robust spatial reduction.** Taking a *median* across a neighborhood's pixels (not a mean)
  survives a handful of contaminated pixels. Keep robust estimators everywhere.

- **The signal it does capture is real and stable.** The seasonal signature (post-monsoon peak,
  Delhi cool year-round) repeats: **12 of 15 city-season signs are identical in every one of the 5
  years.** The method is not noise — it's just measuring the wrong *thing* for "lived heat."

- **Coverage gating exists at all.** The instinct to refuse a number when clouds ate the data is
  correct. The gates are just built on the wrong test (see §3).

---

## 3. What broke — the three structural failures (with receipts)

Every wrong value we found traces to one of these. Design the rebuild to make each *impossible*.

### 3a. The rural baseline is a single scalar → one bad month corrupts the whole city

SUHI = area_LST − `rural_baseline`, where `rural_baseline` is **one number per city-month**.
Subtracting one number from every neighborhood means **any error in that number shifts the entire
city's result in lockstep.**

- Same calendar month, non-monsoon, the baseline swings **3–11 °C across years** — some real weather,
  but the tails are contamination.
- In the monsoon it goes **physically impossible**: Mumbai's rural baseline reached **−14.7 °C**;
  its June baseline spanned **59 °C** across five years.
- **Case study — Chennai, Oct 2022, SUHI = +5.35 (a spike):** the rural baseline that month was
  **25.1 °C**, 8–10 °C below every other October (31 / 33 / 35 °C), with 39% of areas voided and a
  14.9 °C cold pixel. A cloud-cooled baseline inflated *every* neighborhood's score at once. The
  "+5.35" is an artifact of the reference, not the city.

**Fix:** the reference must be robust and hard to corrupt — a **climatology-anchored** estimate
(this month vs the multi-year expectation for that month) with its own anomaly detector, and ideally
estimated **per scene** (§4) so it shares the atmosphere of the pixels it's subtracted from.

### 3b. Coverage gates count pixels, not values → cold-cloud contamination leaks through

The gates ask "did enough pixels survive the cloud mask?" — never "are the surviving values
physically sane?" Cloud edges pass the QA mask but read far too cold.

- Contamination is **rare but real in committed data**: 0.00–0.08% of non-monsoon neighborhood
  readings sit **>12 °C below their own month's spatial median** (physically impossible for a
  same-city neighbor). Worst gaps: **26.6 °C (Bengaluru), 15.5 °C (Chennai).** Delhi/Hyderabad/Mumbai: zero.
- **Case study — Bengaluru, Nov 2023:** an area read **11 °C** while the city median was ~34 °C. It
  cleared every gate and dragged that month's score negative.

**Fix:** add a **value-based QC** step — flag/winsorize any unit whose value is implausible relative
to (i) its spatial neighbors and (ii) its own climatology. This is the "future guard" the old code's
own docs admit is missing.

### 3c. Temporal aggregation is biased by *which* months survive

Aggregating to "season" or "year" with a fixed rule silently compares different underlying samples.

- **Whole months get voided constantly:** 1 (Delhi) to **12 of 60 months (Bengaluru)** per city,
  concentrated in monsoon *and post-monsoon*.
- **Case study — the Bengaluru "fade" (+2.0 → +0.1 post-monsoon, 2020→2024):** looked like a cooling
  trend. It isn't. Post-monsoon has only two months (Oct, Nov); in 2024 **October was fully voided**,
  so the "season" was **November alone** — and November runs ~1 °C cooler than October *every* year
  (Oct ≈ +2.5, Nov ≈ +1.5). The trend was an artifact of unequal month coverage.

**Fix:** never store aggregates as the primary artifact. Store the **atomic observations** and compute
month/season/year **as views on read**, weighting by sample count and **carrying uncertainty** so an
under-sampled season shows a wide error bar instead of a confident wrong number.

### 3d. (Bonus) Neighborhood units track OSM mapping density, not reality

Areas come from OSM `place` nodes → Voronoi tessellation. Density reflects *how well-mapped* a city
is, not its size: our five metros have 298–908 areas each, but the method yields ~1091 seeds for
Bengaluru and as few as ~7 for an under-mapped tier-2 city. Fine for these five; a landmine if you
scale to more cities. Consider a consistent spatial unit (fixed grid, census wards, or morphological
built-up zones) and always report **usable land area** per unit (after masking water/parks).

---

## 4. The rebuild — recommended architecture

### 4.1 Decide the estimand (do this before any code)

Pick explicitly. For a lived-heat product, the strongest default:

> **A night-weighted neighborhood heat-exposure index**, expressed as a contrast against a
> metro-wide or rural reference, carrying uncertainty, at monthly resolution.

Candidate building blocks (choose based on how much you want to bite off):
- **Minimum viable:** nighttime LST anomaly (MODIS/VIIRS) as the heat signal + tree-canopy & built
  fraction as exposure modifiers.
- **Better:** downscaled 2 m **air temperature** (ERA5-Land + LST/land-cover downscaling), converted
  to a **heat-stress index** (Heat Index or UTCI) using humidity, night-weighted.
- **Best / research-grade:** a full **Heat Vulnerability Index** = Exposure (heat) × Sensitivity
  (age, density, poverty) × (lack of) Adaptive capacity (green space, AC access). This is what public-health
  heat maps actually use.

### 4.2 Atomic unit = per-scene contrast, not monthly composite

The biggest structural change. Instead of "composite a month, then subtract a scalar":

1. For **each individual satellite overpass / reanalysis timestep**, compute the neighborhood value
   **and** its reference **from the same scene** (same day, sun angle, atmospheric column).
2. Store one atomic record per (unit, scene): `value, reference, contrast, n_pixels, qc_flags, timestamp, source`.
3. Aggregate to month/season/year **on read**, with weights + uncertainty.

Why: this makes the "differencing cancels weather" property (§2) *structural* rather than incidental,
and it makes Chennai-style baseline-corruption self-cancel instead of contaminating a whole month.

### 4.3 Uncertainty is a first-class output

Every emitted value ships as `(estimate, standard_error, n_scenes, usable_fraction)`. Half of the
audit pain was that `+0.13` and `+2.5` looked equally authoritative. With SEs, the Bengaluru "fade"
shows overlapping error bars and never gets reported as a trend.

### 4.4 QC by physics; reference by climatology

- **Pixel/unit QC:** physical plausibility bounds + spatial-neighbor + climatology outlier tests
  (kills §3b). QC *before* compositing.
- **Reference model:** robust, climatology-anchored, with an anomaly detector that flags a month
  whose reference deviates implausibly from its own seasonal expectation (kills §3a).

### 4.5 Fix data starvation at the source

10–12 voided months/city is the real quality ceiling. Fuse sensors:

| Source | Role | Notes |
|---|---|---|
| **Landsat 8 + 9** | High-res spatial texture (30–100 m thermal) | Fusing both ≈ 8-day revisit vs 16. Daytime (~10:30) only. |
| **MODIS (MOD11/MYD11)** | Daily **day + night** LST, ~1 km | Temporal backbone; gives the crucial *nighttime* signal. |
| **VIIRS LST** | Daily day+night, ~750 m | Continuity/cross-check for MODIS. |
| **ERA5-Land** | Hourly 2 m **air temperature** + humidity, ~9 km | The actual lived-heat variable; downscale with LST/land cover. |
| **Ground stations / low-cost sensor nets** | Validation & bias-correction anchor | Sparse but truth-ish; use to calibrate. |
| **ESA WorldCover / tree-canopy / WorldPop / impervious** | Exposure & vulnerability modifiers | For the index in §4.1. |

Pattern: **MODIS/VIIRS/ERA5 for temporal density & night + air temp; Landsat for spatial detail;
downscale/fuse** so you're never hostage to one clear Landsat pass.

### 4.6 Engineering principles

- **Store atoms (long-format Parquet), derive views.** Aggregations are queries, never baked files.
- **Provenance on every estimate:** scene IDs, QC flags, pixel counts — so any number is auditable
  (this whole audit was only possible because area-level values were retained; keep going finer).
- **Modular, independently testable stages:** `retrieve` (pixels→temperature, with real emissivity)
  → `reduce` (pixels→unit stats) → `reference` (baseline model) → `aggregate` (views) → `index`.
  The old monolithic Earth Engine graph is hard to unit-test; split it.
- **Golden-scene + property tests:** e.g. "adding one masked pixel barely moves the unit median,"
  "a corrupted reference month raises a flag."
- **Incremental & idempotent:** add a scene → recompute only affected views.

---

## 5. Decisions to make before writing code

1. **Estimand:** heat-exposure index? air-temp UHI? heat-stress (UTCI/WBGT)? night-weighted how much?
2. **Reference frame:** rural ring (like now), metro-wide mean, or a fixed climatological normal?
3. **Spatial unit:** keep OSM neighborhoods (recognizable but uneven) or a consistent grid/ward?
4. **Temporal resolution & how to handle gaps:** monthly with uncertainty is a good default.
5. **Validation target:** what ground truth (stations, health outcomes, a hot-day case study) tells
   you the map is *right*, not just internally consistent?

---

## 6. Reference — key numbers from the audit (for sanity-checking the rebuild)

| Finding | Value |
|---|---|
| Weather noise removed by rural-baseline contrast | **53–70%** |
| City↔rural interannual correlation (non-monsoon) | **0.89–0.96** |
| Interannual σ: raw city LST → contrast | **1.4–2.7 °C → 0.6–1.1 °C** |
| Seasonal-sign stability | **12 / 15** city-season signs repeat every year |
| Fully-voided months per city (of 60) | **1 (Delhi) – 12 (Bengaluru)** |
| Cold-outlier contamination, non-monsoon readings | **0.00–0.08%** (worst gap 26.6 °C) |
| Rural-baseline swing, same non-monsoon month across years | **3–11 °C** |
| Rural-baseline in monsoon | physically impossible (Mumbai **−14.7 °C**) |
| Worked artifacts | Bengaluru "fade" (coverage/composition); Chennai +5.35 (contaminated baseline) |

---

## 7. One-paragraph verdict

The old pipeline is a competent implementation of the wrong measurement. Its relative-contrast idea
and its robustness instincts are worth carrying forward; its single-scalar baseline, count-based
gating, and bake-the-aggregate storage are worth discarding. But the decisive change is conceptual,
not technical: **daytime surface temperature is not lived heat.** Build the next version around
nighttime warmth and air-temperature-based heat stress, keep the contrast framing, make per-scene
observations the atomic unit, and carry uncertainty end-to-end. Everything in §3 becomes impossible
by construction, and the product finally measures what you set out to measure.
