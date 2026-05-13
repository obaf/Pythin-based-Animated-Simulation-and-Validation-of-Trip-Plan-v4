# Shared-Ride Trip Plan Validator (Python + OpenStreetMap)

A standalone, planner-agnostic **validation harness** for shared-ride
trip execution plans. It takes a tabular plan produced by *any*
dispatcher — a heuristic baseline, a Deep Reinforcement Learning
policy, a MILP optimum, anything that can be expressed as an ordered
list of vehicle pickup/dropoff actions — and answers two questions:

1. **Is the plan physically achievable on the real road network?** The
   validator re-routes every leg on OpenStreetMap and re-derives every
   pickup and dropoff datetime from cumulative route distance and a
   single configurable speed scalar (`VEHICLE_SPEED_MPH`). The dispatcher's
   own claimed times sit side-by-side with the validator's re-derived
   times in the output Excel, so any divergence is directly auditable.
2. **What does each vehicle actually look like in motion?** A self-
   contained animated HTML map shows every vehicle, every passenger,
   and every shared-ride moment in real time, with grey stickmen
   waiting at pickup points, riding on top of colour-coded cars while
   en route, and turning green at the dropoff.

The validator is **planner-blind by design**. A heuristic baseline,
an RL agent, and a MILP optimum scored on the same validator are
scored on a common road-network axis, which puts them on a
like-for-like comparison footing.


---

## Contents

1. [Why this validator exists](#why-this-validator-exists)
2. [Features](#features)
3. [Project layout](#project-layout)
4. [Input file specification — `INPUT_FILE.xlsx`](#input-file-specification--input_filexlsx)
5. [Output file specification — `validated_*.xlsx`](#output-file-specification--validated_xlsx)
6. [Quickstart](#quickstart)
7. [Optional bridge for decision-per-row plans — `prepare_v4_plan.py`](#optional-bridge-for-decision-per-row-plans--prepare_v4_planpy)
8. [Configuration knobs](#configuration-knobs)
9. [Routing backends — OSRM vs. OSMnx vs. auto](#routing-backends--osrm-vs-osmnx-vs-auto)
10. [Choosing `VEHICLE_SPEED_MPH` for NYC](#choosing-vehicle_speed_mph-for-nyc)
11. [Local OSRM via Docker (recommended for production runs)](#local-osrm-via-docker-recommended-for-production-runs)
12. [Validation workflow for researchers](#validation-workflow-for-researchers)
13. [Architecture](#architecture)
14. [Citation](#citation)

---

## Why this validator exists

The trip plans produced by a dispatcher are only as trustworthy as
the simulator that scored them during training or design. Three
common hidden biases:

- **Optimistic travel times** — the dispatcher assumed free-flow
  speeds or ignored detours, so its claimed `actual_pickup_datetime`
  and `actual_dropoff_datetime` are unachievable on the real road
  network.
- **Off-road coordinates** — pickups or dropoffs that don't sit on a
  routable road segment. The planner's internal model probably
  silently snapped them, but the snap distance and direction never
  surface in the final plan.
- **Mis-allocated time in vehicle** — durations that look good on the
  planner's own numbers but, once recomputed from real distances and
  a defensible speed, exceed a Service Level Agreement (e.g. a
  10-minute pooling cap).

This validator **re-derives every datetime independently** from the
road network and a researcher-controlled `VEHICLE_SPEED_MPH`, and
preserves the planner's own numbers alongside, so the audit is a
side-by-side column comparison. The validated workbook contains:

| Planner-reported (input) | Validator-derived (output, new) |
|---|---|
| `actual_pickup_datetime` | `validated_pickup_datetime` |
| `actual_dropoff_datetime` | `validated_dropoff_datetime` |
| *(implied)* `actual_dropoff − actual_pickup` | `actual_time_in_vehicle` |
| — | `validated_time_in_vehicle` |

A side-by-side comparison of those four columns reveals exactly where
and how much the planner's numbers diverge from physically
reproducible values.

---

## Features

- **One Excel in, one Excel out plus one HTML out.** No databases, no
  bespoke binary formats, no APIs. The input and outputs are
  spreadsheets you can audit by eye.
- **Strict per-vehicle row ordering** — each vehicle executes its
  actions in the exact order they appear in the input file. The
  validator does not sort, regroup, or assume any global "matching
  algorithm" — it trusts the plan as authoritative.
- **Time-based activation** — each vehicle appears at its first
  pickup at the `actual_pickup_datetime` of its first row in the
  input. Vehicles that activate later in the day sit dormant until
  their slot.
- **Shared rides supported** — multiple `trip_id`s can be onboard
  concurrently. Vehicle popups list the current passengers, and the
  animated stickmen visibly stack on the roof of the car between
  pickups.
- **Real road-network routing** via OSRM (default) or OSMnx (pure-
  Python fallback). Routes are cached on disk (`.route_cache.json`)
  so warm runs are routing-free.
- **Speed is the researcher's choice, not OSRM's.** Travel duration
  is always computed as `route_distance ÷ (VEHICLE_SPEED_MPH × 0.44704)`.
  OSRM's own duration estimate is intentionally ignored — every
  validated datetime traces back to a single, citable speed scalar.
- **Robust fallback** to a dashed straight-line segment if routing
  fails for any pair, so the animation never aborts.
- **Self-contained animated HTML** with Speed Up / Slow Down / Pause
  / Restart buttons, a time slider, a live multiplier display, and a
  per-vehicle legend. Pure Folium + JS; opens directly in any modern
  browser; no server needed.
- **Animated SVG car icons and stickman passengers.** Grey while
  waiting at pickup, on the roof of the car while onboard
  (horizontally staggered when more than one passenger is on board),
  green at the dropoff. Designed to make detour anomalies visible to
  reviewers who can't read code.

---

## Project layout

```text
trip_simulation_opus/
├── trip_simulation.py                    # The validator (~1,360 lines, single file)
├── prepare_v4_plan.py                    # Optional bridge for decision-per-row plans
├── requirements.txt                      # Python dependencies (pandas, folium, osmnx, …)
├── README.md                             # This file
├── README-AnyLogic.md                    # Companion AnyLogic-based validator
├── README_OSMnx.md                       # Deep dive on OSRM vs. OSMnx failover
│
├── sample_trip_execution_plan.xlsx       # Bundled tiny sample: 4 rows / 1 vehicle / 1 share
├── sample_trip_execution_plan_v2.xlsx    # Bundled larger sample: 7,676 rows / 2,016 vehicles
│
├── validated_trip_execution_plan.xlsx    # Validator output for the tiny sample (example)
└── trip_simulation.html                  # Last generated animation (example)
```

---

## Input file specification — `INPUT_FILE.xlsx`

The input is a single Excel sheet whose rows represent **vehicle
actions in chronological execution order, grouped by vehicle**. One
row per pickup or dropoff event.

### Required columns

| Column | Type | Description |
|---|---|---|
| `vehicle_id` | int | Stable identifier per vehicle. All rows for one vehicle must be contiguous and in execution order. |
| `trip_id` | int | Identifier of the passenger / trip. Each `trip_id` appears on exactly two rows per vehicle: one pickup, one dropoff. |
| `pickup_lon` | float | Longitude of the pickup point (WGS84). |
| `pickup_lat` | float | Latitude of the pickup point (WGS84). |
| `dropoff_lon` | float | Longitude of the dropoff point (WGS84). |
| `dropoff_lat` | float | Latitude of the dropoff point (WGS84). |
| `vehicle_action` | int | `1` for pickup rows, `2` for dropoff rows. |
| `actual_pickup_datetime` | datetime | Planner's claimed pickup datetime. The validator uses this only on the very first row of each vehicle (to set its activation moment); on later pickup rows it is preserved for side-by-side comparison with `validated_pickup_datetime`. |

### Optional but recommended

| Column | Type | Description |
|---|---|---|
| `actual_dropoff_datetime` | datetime | Planner's claimed dropoff datetime. If present, preserved for comparison with `validated_dropoff_datetime` and used to compute `actual_time_in_vehicle`. |
| `passenger_count` | int | Carried along untouched. Useful for downstream analysis. |
| `trip_distance` | float | Planner-claimed trip distance. |
| Any other column | mixed | Preserved verbatim in the validated output, so existing TLC-style schemas pass through unchanged. |

### Authoritative row-ordering rules

1. **All rows for a given `vehicle_id` must be contiguous** in the
   sheet. The validator does not sort or regroup — it trusts the file
   as authoritative.
2. **Within a vehicle, rows must be in execution order.** The first
   row is the vehicle's first action; the last row is its final
   action. The validator activates the vehicle at the
   `actual_pickup_datetime` of its first row.
3. **Each `trip_id` must appear exactly twice on its vehicle**: once
   with `vehicle_action == 1` (pickup) and once with `vehicle_action
   == 2` (dropoff). Shared rides are expressed by interleaving
   multiple `trip_id`s between their pickup and dropoff rows (e.g.
   `pickup-1 → pickup-88 → dropoff-88 → dropoff-1` means trip 88 is
   pooled inside trip 1).
4. **Datetimes** should be timezone-naive (or all in the same
   timezone). The validator parses with `pandas.to_datetime(errors="coerce")`.
5. **Coordinates** are decimal degrees in WGS84. Each point must fall
   on a routable road segment of the chosen routing backend — this
   matters most if you self-host an OSRM extract for a specific
   region.

### Minimal worked example (4 rows, 1 vehicle, one shared ride)

| vehicle_id | trip_id | pickup_lon | pickup_lat | dropoff_lon | dropoff_lat | vehicle_action | actual_pickup_datetime | actual_dropoff_datetime |
|---:|---:|---:|---:|---:|---:|---:|---|---|
| 1 | 1  | -73.9876 | 40.7760 | -73.9999 | 40.7484 | 1 | 2020-01-06 08:00:00 |  |
| 1 | 88 | -73.9596 | 40.7669 | -73.9888 | 40.7535 | 1 | 2020-01-06 08:00:00 |  |
| 1 | 88 | -73.9596 | 40.7669 | -73.9888 | 40.7535 | 2 |  | 2020-01-06 08:10:08 |
| 1 |  1 | -73.9876 | 40.7760 | -73.9999 | 40.7484 | 2 |  | 2020-01-06 08:12:11 |

This is exactly the structure of `sample_trip_execution_plan.xlsx`,
which can be opened in Excel as a working template.

To convert an arbitrary planner's output into this schema, expand
every trip into two rows (pickup + dropoff), interleave them per
vehicle in the order the vehicle executes, and fill the `actual_*`
columns from whatever the planner reports. The validator overwrites
none of these columns — it only **adds** `validated_*` columns
alongside.

---

## Output file specification — `validated_*.xlsx`

The validator writes a copy of the input workbook with **four new
columns appended** (and every existing column preserved). The output
filename is controlled by the `OUTPUT_PLAN_FILE` environment variable
(default: `validated_trip_execution_plan.xlsx`; set to an empty
string to skip Excel output entirely).

| New column | Pickup row (`vehicle_action=1`) | Dropoff row (`vehicle_action=2`) | Derivation |
|---|---|---|---|
| `validated_pickup_datetime` | populated | `NaT` | Datetime when the vehicle arrives at the pickup point, computed from cumulative route distance and `VEHICLE_SPEED_MPH`. Equals `actual_pickup_datetime` only for the very first row of each vehicle. |
| `validated_dropoff_datetime` | `NaT` | populated | Datetime when the vehicle arrives at the dropoff point, computed the same way. |
| `actual_time_in_vehicle` | populated (same value) | populated (same value) | `actual_dropoff_datetime − actual_pickup_datetime` per `trip_id`, stamped on **both** the pickup and the dropoff row of that trip. `HH:MM:SS` string. |
| `validated_time_in_vehicle` | populated (same value) | populated (same value) | `validated_dropoff_datetime − validated_pickup_datetime` per `trip_id`, stamped on **both** rows of that trip. `HH:MM:SS` string. |

### Formatting conventions

- Datetime columns are written as **native Excel datetimes**, so Excel
  formulas, pivot tables, and chart axes work directly.
- Duration columns are written as **`HH:MM:SS` strings** for
  readability (Excel cannot natively store a Python `timedelta`).
- Rows belonging to vehicles excluded by `MAX_VEHICLES` keep `NaT` in
  the validated columns — they pass through untouched.

### What to look at first when auditing a plan

1. **`validated_pickup_datetime` vs. `actual_pickup_datetime`** — a
   systematic lag indicates the planner under-estimated congestion or
   route lengths.
2. **`validated_time_in_vehicle` vs. `actual_time_in_vehicle`** —
   large discrepancies on pooled trips often reveal that the planner
   ignored detour time when batching passengers.
3. **`validated_time_in_vehicle` against an SLA** (e.g. 10 minutes
   for a pooling guarantee) — any row that violates the SLA on
   `validated_*` but passes on `actual_*` is a candidate bias case.

---

## Quickstart

### One-time venv setup (Windows)

```powershell
cd C:\path\to\trip_simulation_opus
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

PowerShell's default execution policy blocks `Activate.ps1`, so the
venv's `python.exe` is called directly in every recipe.

### Run it with the bundled tiny sample

```powershell
# Uses defaults: INPUT_FILE=sample_trip_execution_plan.xlsx, public OSRM, 20 mph.
.\.venv\Scripts\python.exe trip_simulation.py

# Open the generated artefacts:
Start-Process trip_simulation.html
Start-Process validated_trip_execution_plan.xlsx
```

### A typical NYC research run at 15 mph

```powershell
$env:ROUTING_BACKEND   = "osmnx"                          # see "Routing backends"
$env:VEHICLE_SPEED_MPH = "15"                             # see "Choosing VEHICLE_SPEED_MPH for NYC"
$env:INPUT_FILE        = "sample_trip_execution_plan_v2.xlsx"
$env:OUTPUT_PLAN_FILE  = "validated_trip_execution_plan_v2_15mph.xlsx"
$env:OUTPUT_HTML       = "trip_simulation_v2_15mph.html"
.\.venv\Scripts\python.exe -u trip_simulation.py *> run_v2_15mph.log
```

### macOS / Linux equivalent

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

ROUTING_BACKEND=osmnx \
VEHICLE_SPEED_MPH=15 \
INPUT_FILE=sample_trip_execution_plan_v2.xlsx \
OUTPUT_PLAN_FILE=validated_trip_execution_plan_v2_15mph.xlsx \
OUTPUT_HTML=trip_simulation_v2_15mph.html \
python -u trip_simulation.py 2>&1 | tee run_v2_15mph.log
```

### Windows `cmd` equivalent

Replace `$env:VAR = "value"` with `set VAR=value` (no spaces around
`=`, no quotes around the value) and `*> log` with `> log 2>&1`:

```cmd
set ROUTING_BACKEND=osmnx
set VEHICLE_SPEED_MPH=15
set INPUT_FILE=sample_trip_execution_plan_v2.xlsx
set OUTPUT_PLAN_FILE=validated_trip_execution_plan_v2_15mph.xlsx
set OUTPUT_HTML=trip_simulation_v2_15mph.html
.venv\Scripts\python.exe -u trip_simulation.py > run_v2_15mph.log 2>&1
```

### Watching the animation

Once `trip_simulation.html` (or whatever you set `OUTPUT_HTML` to)
exists, open it in any modern browser. Three things to look for:

- **Grey stickmen** at the pickup points of trips not yet picked up.
- **Coloured cars** that wake up at their first-row
  `actual_pickup_datetime`, drive to the pickup, and pick up the
  passenger — the stickman hops on top of the car as soon as the
  vehicle reaches them.
- **Two stickmen on the roof** of the same car during the
  pickup-to-dropoff segment of any shared ride. The stickman pops off
  at the dropoff and turns green.

Use the bottom toolbar to scrub time, pause, speed up, or restart.
Click any car or stickman for a popup with the vehicle / trip ID.

---

## Optional bridge for decision-per-row plans — `prepare_v4_plan.py`

Some dispatchers (for example, ride-pooling DRL agents) emit **one
row per dispatcher decision** rather than one row per vehicle
action. The validator can't consume that format directly — it needs
lat/lon on every row and a 2-rows-per-solo, 4-rows-per-shared
expansion. `prepare_v4_plan.py` does that expansion in a single shot.

### What the bridge expects

A workbook (default name: `trip_execution_plan_v4.xlsx`) with one row
per decision and at least the columns below.

| Column | Description |
|---|---|
| `decision_id` | Sequential decision index (0-based). |
| `trip_i_idx` | Positional index of the *leader* trip in the source test set. |
| `partner_idx` | Positional index of the *partner* trip (only for `shared` outcomes; blank otherwise). |
| `outcome` | One of `solo`, `shared`, `fallback_solo`, `vmt_reject_solo`. |
| `dist_i_mi`, `dist_j_mi` | Solo haversine distance (miles) for leader and partner. |
| `pickup_lat_i`, `pickup_lon_i`, `dropoff_lat_i`, `dropoff_lon_i` | Leader's WGS84 coordinates. |
| `pickup_lat_j`, `pickup_lon_j`, `dropoff_lat_j`, `dropoff_lon_j` | Partner's coordinates (blank for solo outcomes). |
| `pickup_datetime_i` | Leader's original pickup datetime — used as the vehicle's activation clock. |

Any other columns the plan carries are simply ignored.

### What the bridge produces

`trip_execution_plan_v4_to_validate.xlsx` in the validator's per-
action schema (the [Input file specification](#input-file-specification--input_filexlsx)
above). Specifically:

- **Solo / fallback_solo / vmt_reject_solo** decision → 2 rows under
  one `vehicle_id` (pickup_i, dropoff_i). The partner of a rejected
  pairing is not on this vehicle; it appears as its own decision
  elsewhere in the plan.
- **Shared** decision → 4 rows under one `vehicle_id`, in the order
  the dispatcher actually drove. The drop-order rule is the standard
  shorter-second-leg heuristic: drop rider *i* first when
  `haversine(p_j → d_i) < haversine(p_j → d_j)`, else drop rider *j*
  first. Getting that order right is what makes the animated HTML
  show *two* stickmen on the roof between the pickups and *one*
  stickman through to the final dropoff.

Dispatcher-side `actual_pickup_datetime` and
`actual_dropoff_datetime` are computed at **20 mph** by the bridge,
which matches the validator's own default `VEHICLE_SPEED_MPH`. So
if the validator is then run at 20 mph, `actual_time_in_vehicle`
mirrors the dispatcher's claim exactly and any delta in
`validated_time_in_vehicle` is pure road-network-vs-haversine bias.

### Running it

```powershell
# 1. Place the decision-per-row plan in this folder:
Copy-Item <path-to>\trip_execution_plan_v4.xlsx . -Force

# 2. Bake the per-action workbook:
.\.venv\Scripts\python.exe prepare_v4_plan.py
```

The script prints a sanity report (decisions, vehicles, row counts,
balance check) and writes `trip_execution_plan_v4_to_validate.xlsx`
next to itself. Hand that workbook to `trip_simulation.py` exactly
like any other input.

### CLI flags (defaults are usually fine)

| Flag | Default | Description |
|---|---|---|
| `--plan` | `./trip_execution_plan_v4.xlsx` | The decision-per-row plan to bridge. |
| `--out` | `./trip_execution_plan_v4_to_validate.xlsx` | Output workbook in the per-action schema. |



### Full validator pipeline against a v4 plan

```powershell
# 1. Bridge the plan (one-time, takes a few seconds):
Copy-Item <path-to>\trip_execution_plan_v4.xlsx . -Force
.\.venv\Scripts\python.exe prepare_v4_plan.py

# 2. Smoke test on the first 20 vehicles (~1 min):
$env:INPUT_FILE        = "trip_execution_plan_v4_to_validate.xlsx"
$env:VEHICLE_SPEED_MPH = "20"
$env:ROUTING_BACKEND   = "osmnx"
$env:MAX_VEHICLES      = "20"
$env:OUTPUT_PLAN_FILE  = "validated_smoketest.xlsx"
$env:OUTPUT_HTML       = "validated_smoketest.html"
.\.venv\Scripts\python.exe -u trip_simulation.py *> run_v4_smoketest.log

# 3. Full run (the larger HTML can hit 200–350 MB at 13k decisions):
$env:MAX_VEHICLES      = "0"
$env:OUTPUT_PLAN_FILE  = "trip_execution_plan_v4_validated.xlsx"
$env:OUTPUT_HTML       = "trip_execution_plan_v4_animated.html"
.\.venv\Scripts\python.exe -u trip_simulation.py *> run_v4_full.log

# 4. Open the animation:
Start-Process trip_execution_plan_v4_animated.html
```

The cmd-syntax equivalent is provided towards the end of this README.

---

## Configuration knobs

All knobs live at the top of `trip_simulation.py` (lines ~50–85) and
can be overridden via environment variables of the same name.

| Variable | Default | Description |
|---|---|---|
| `INPUT_FILE` | `sample_trip_execution_plan.xlsx` | Excel plan to simulate. |
| `VEHICLE_SPEED_MPH` | `20` | Base vehicle speed in miles per hour. **All validated datetimes derive from this.** |
| `ROUTING_BACKEND` | `osrm` | `osrm`, `osmnx`, or `auto` (OSRM first, OSMnx fallback). |
| `OSRM_BASE_URL` | `http://router.project-osrm.org` | OSRM server endpoint. Point at a local Docker instance for production. |
| `OUTPUT_HTML` | `trip_simulation.html` | Path of the generated animated HTML. Set to `""` to skip the HTML and produce only the validated Excel. |
| `OUTPUT_PLAN_FILE` | `validated_trip_execution_plan.xlsx` | Path of the validated Excel output. Set to `""` to skip Excel output. |
| `ROUTE_CACHE_FILE` | `.route_cache.json` | Where to persist the routing cache. Safe to delete (will be rebuilt on the next run). |
| `OSRM_REQUEST_DELAY` | `0.2` | Polite throttle between OSRM HTTP calls, in seconds. |
| `OSRM_TIMEOUT` | `15` | HTTP timeout for OSRM calls, in seconds. |
| `OSMNX_BUFFER_M` | `1500` | Buffer (metres) added around the waypoint bounding box when downloading the OSMnx graph. |
| `OSMNX_NETWORK_TYPE` | `drive` | OSMnx network type (`drive`, `bike`, `walk`, …). |
| `MAX_VEHICLES` | `0` | Cap on the number of vehicles to simulate (`0` = no cap). Useful for keeping the animated HTML browser-friendly on huge plans. |
| `ROUTE_CACHE_SAVE_EVERY` | `200` | Persist the routing cache to disk every N routes. |

---

## Routing backends — OSRM vs. OSMnx vs. auto

| Backend | Pros | Cons |
|---|---|---|
| **`osrm`** *(default)* | Fastest. Production-grade. Identical to what most ride-sharing planners use internally. | Public demo server is rate-limited (~3 s/call) and intermittently down. Self-hosting via Docker is straightforward but adds infrastructure. |
| **`osmnx`** | Pure Python, no external services at runtime after the OSM graph is fetched. Excellent reproducibility. | Initial Overpass-API graph download can take 30–120 s for a large bounding box. Slightly slower per-route than OSRM. |
| **`auto`** | Tries OSRM first, falls back to OSMnx silently, then to a straight line. Best resilience for hands-off runs. | Mixes two backends in one run, so the "speed source of truth" is `VEHICLE_SPEED_MPH` rather than any single graph. |

For a deeper dive into the failover decision tree, cKDTree
nearest-node lookups, graph buffer sizing, etc., see
`README_OSMnx.md`.

---

## Choosing `VEHICLE_SPEED_MPH` for NYC


Various speed choices:

- **15 mph** — for plans dominated by outer-borough trips. Above the
  citywide average; below the 18.56 mph Staten Island ceiling.
- **20 mph** — anchored by Moniot, Ge & Wood (2022) for US ride-
  hailing fleets across 384 CBSAs (15–28 mph observed band) and by
  NYC's 25 mph citywide default speed limit (NYC DOT, 2014, Vision
  Zero). Often used to mirror a dispatcher's internal speed
  assumption — see the [v4 plan workflow](#optional-bridge-for-decision-per-row-plans--prepare_v4_planpy).
- **10 mph** — for CBD-heavy or peak-hour scenarios. Below the
  citywide average; above the 9.22 mph Manhattan floor.
- **9.7 mph** — for explicitly modelling post-congestion-pricing CBD
  conditions, citing NBER working paper *w33584* (2025).

The chosen scalar is the single source of truth behind every
`validated_*` datetime, so it is recorded in the run log alongside
the routing backend and the input file name.

---

## Local OSRM via Docker (recommended for production runs)

The public OSRM demo server is rate-limited and not guaranteed to be
up. For real research runs, host OSRM locally against an OSM extract
that covers your study area.

```bash
# 1. Download an OSM extract (example: New York State)
mkdir osrm && cd osrm
wget https://download.geofabrik.de/north-america/us/new-york-latest.osm.pbf

# 2. Preprocess with the OSRM toolchain (CAR profile).
docker run --rm -t -v "${PWD}:/data" ghcr.io/project-osrm/osrm-backend \
    osrm-extract -p /opt/car.lua /data/new-york-latest.osm.pbf
docker run --rm -t -v "${PWD}:/data" ghcr.io/project-osrm/osrm-backend \
    osrm-partition /data/new-york-latest.osrm
docker run --rm -t -v "${PWD}:/data" ghcr.io/project-osrm/osrm-backend \
    osrm-customize /data/new-york-latest.osrm

# 3. Serve it on http://localhost:5000
docker run -t -i -p 5000:5000 -v "${PWD}:/data" ghcr.io/project-osrm/osrm-backend \
    osrm-routed --algorithm mld /data/new-york-latest.osrm
```

Then point the validator at the local instance:

```powershell
$env:OSRM_BASE_URL = "http://localhost:5000"
.\.venv\Scripts\python.exe trip_simulation.py
```

---

## Validation workflow for researchers

Use this validator as a **planner-agnostic audit step** between your
dispatcher and any downstream evaluation. A reproducible workflow:

1. **Export the plan from your dispatcher** into the input schema
   above. Two rows per trip (pickup, dropoff); contiguous per
   vehicle; in execution order. If the dispatcher emits one row per
   decision instead, use `prepare_v4_plan.py` to bridge.
2. **Pick a defensible `VEHICLE_SPEED_MPH`** with a documented
   source (NYC DOT Mobility Report 2019 is a good default for NYC
   studies — see
   [Choosing `VEHICLE_SPEED_MPH` for NYC](#choosing-vehicle_speed_mph-for-nyc)).
3. **Run the validator twice on the same plan**: once with the
   dispatcher's own speed assumption (e.g. 20 mph for a DRL
   dispatcher trained against haversine routing), once at the
   citywide DOT figure (12 mph). The delta between the two
   `validated_time_in_vehicle` columns quantifies the dispatcher's
   speed bias.
4. **Compute aggregate metrics from the validated output**: mean and
   95th percentile of `validated_time_in_vehicle`, SLA-violation
   rate, mean detour-induced delay (`validated_time_in_vehicle −
   validated_direct_time`, if you derive a direct-trip baseline).
5. **Repeat the same workflow for any competing planner** (heuristic
   baseline, DRL policy, MILP optimum). Because every planner is
   scored on a common `validated_*` axis derived from the same OSM
   road network and the same speed scalar, the comparison is
   **planner-blind** and immune to each dispatcher's internal
   optimism.
6. **Visualise individual cases of disagreement** by opening the
   animated HTML and scrubbing to the time of interest. The trail
   shows exactly which route the validator chose, which makes the
   nature of the disagreement obvious (wrong direction, detour,
   off-road point).

The `validated_*` columns are reproducible from `INPUT_FILE`,
`VEHICLE_SPEED_MPH`, and the road network at the time of the run.

---

## Architecture

```text
                  optional (only for decision-per-row plans)
                  ┌────────────────────┐
trip_execution_   │ prepare_v4_plan.py │  bake decision-per-row plan
plan_v4.xlsx ──▶ │ (decision-per-row  │  into per-action schema
                  │  -> per-action)    │
                  └─────────┬──────────┘
                            │ writes
                            ▼
┌──────────────┐   ┌─────────────────┐   ┌────────────────────────┐   ┌─────────────────────────┐
│ load_data()  │──▶│ initialize_     │──▶│ simulate_vehicle_      │──▶│ export_validated_plan() │──▶ validated_*.xlsx
│ (pandas)     │   │   vehicles()    │   │   movements()          │   └─────────────────────────┘
└──────────────┘   └─────────────────┘   └──────────┬─────────────┘                │
                                                    │ uses                        ▼
                                                    ▼                       ┌──────────────┐
                                             ┌──────────────┐               │ render_map() │──▶ trip_simulation.html
                                             │ RouteManager │──▶ OSRM/      │ (folium+JS)  │
                                             └──────────────┘    OSMnx      └──────────────┘
                                                                + .route_cache.json
```

Key modules:

- **`trip_simulation.py`** — the core validator. Standalone,
  planner-agnostic, ~1,360 lines including the inline JavaScript
  that powers the animated HTML.
- **`prepare_v4_plan.py`** — optional. Bridges a decision-per-row
  plan into the per-action schema this validator consumes. Self-
  contained: haversine and the 20 mph speed constant are inlined,
  so the script has no external project dependencies.

Key classes (all in `trip_simulation.py`):

- **`Action`** — one row resolved to `(lat, lon, kind, trip_id, scheduled_time)`.
- **`Segment`** — one traversed leg (polyline, cumulative distance,
  start / end sim time in seconds, end-of-segment event).
- **`Vehicle`** — ordered list of `Action`s, colour, activation time,
  computed `Segment`s.
- **`RouteManager`** — OSRM + OSMnx client with disk cache, cKDTree-
  accelerated nearest-node lookup on the OSMnx path, batched cache
  saves, and a straight-line fallback for unreachable pairs.

### Performance characteristics

For the bundled 2,016-vehicle / 7,676-row
`sample_trip_execution_plan_v2.xlsx` workbook, a cold OSMnx run
routes a few thousand segments in around a minute, and a warm run
(every route already in `.route_cache.json`) skips routing entirely
and is dominated by Excel writeback and HTML rendering.

For a 10k+ decision plan, expect several minutes cold and an
animated HTML file in the hundreds of MB. Set `OUTPUT_HTML=""` if
only the validated Excel is needed.

---



### cmd-equivalent recipe for the full v4 pipeline

```cmd
:: 1) one-time copy and bridge
copy /Y <path-to>\trip_execution_plan_v4.xlsx .
.venv\Scripts\python.exe prepare_v4_plan.py

:: 2) smoke test on the first 20 vehicles
set INPUT_FILE=trip_execution_plan_v4_to_validate.xlsx
set VEHICLE_SPEED_MPH=20
set ROUTING_BACKEND=osmnx
set MAX_VEHICLES=20
set OUTPUT_PLAN_FILE=validated_smoketest.xlsx
set OUTPUT_HTML=validated_smoketest.html
.venv\Scripts\python.exe -u trip_simulation.py > run_v4_smoketest.log 2>&1

:: 3) full run
set MAX_VEHICLES=0
set OUTPUT_PLAN_FILE=trip_execution_plan_v4_validated.xlsx
set OUTPUT_HTML=trip_execution_plan_v4_animated.html
.venv\Scripts\python.exe -u trip_simulation.py > run_v4_full.log 2>&1

:: 4) open the animated map
start "" trip_execution_plan_v4_animated.html
```

---


##References:

- NYC DOT, *New York City Mobility Report 2019*. <https://www.nyc.gov/html/dot/downloads/pdf/mobility-report-singlepage-2019.pdf>.
- NYC DOT, 2014 Vision Zero rule change (citywide 25 mph default speed limit).
- Moniot, M., Ge, Y., Wood, E. (2022), *Estimating Fast Charging Infrastructure Requirements to Fully Electrify Ride-Hailing Fleets across the United States*, NREL/IEEE.

- Project OSRM. The OSRM routing engine and Docker images.
- Boeing, G. (2017), *OSMnx: New Methods for Acquiring, Constructing,
  Analyzing, and Visualizing Complex Street Networks*, Computers,
  Environment and Urban Systems 65, 126–139.
