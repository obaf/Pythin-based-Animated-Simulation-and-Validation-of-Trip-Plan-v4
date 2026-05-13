"""Bake the v4 ride-pooling DRL execution plan into the per-action row
schema consumed by ``trip_simulation.py`` (this folder).

Single-file workflow
--------------------
Copy ``trip_execution_plan_v4.xlsx`` from the sibling trip_simulation4
project into this folder (``C:\\PHD_RESEARCH3\\trip_simulation_opus``).
The plan file is now self-contained: starting with the
``agent.inference`` revision that adds the per-decision coordinate and
pickup-datetime columns, no further lookup against test.xlsx is needed.

For every decision row in the plan:

* **solo / fallback_solo / vmt_reject_solo** -> 2 rows (pickup_i,
  dropoff_i) under one ``vehicle_id``. The partner of a rejected
  pairing is *not* on this vehicle; it shows up as its own decision
  elsewhere in the plan.
* **shared** -> 4 rows interleaved in the order the v4 agent actually
  drove. The drop-order rule is identical to ``agent/inference.py``
  lines 72-96: drop rider *i* first when
  ``haversine(p_j -> d_i) < haversine(p_j -> d_j)``, otherwise drop
  rider *j* first. Getting this right is what makes the animated HTML
  show two stickmen on the roof between the pickups and a single
  stickman through to the final dropoff.

Dispatcher-side ``actual_pickup_datetime`` / ``actual_dropoff_datetime``
columns are computed at 20 mph (same constant the v4 agent used
internally and the same default ``VEHICLE_SPEED_MPH`` the opus
validator uses), so the validator's ``actual_time_in_vehicle`` mirrors
the v4 plan's claim exactly and only the OSM road network introduces a
delta in ``validated_time_in_vehicle``.

Speed justification: 20 mph sits inside the 15-28 mph observed band
that Moniot, Ge & Wood (2022) report for US ride-hailing fleets across
384 CBSAs (NREL/IEEE), and just below NYC's 25 mph citywide default
speed limit (NYC DOT, 2014, Vision Zero).

Usage
-----
    .\\.venv\\Scripts\\python.exe prepare_v4_plan.py [--plan PATH] [--out PATH]

Defaults expect ``trip_execution_plan_v4.xlsx`` next to this script
(and write ``trip_execution_plan_v4_to_validate.xlsx`` next to it too),
so the script works without arguments after a one-file copy.
"""

from __future__ import annotations

import argparse
import math
from datetime import timedelta
from pathlib import Path

import pandas as pd


# --- inlined so this script is self-contained inside the opus venv,
#     i.e. no dependency on trip_simulation4's ``env`` package -----------

TRAVEL_SPEED_MPH = 20.0
MIN_PER_MILE = 60.0 / TRAVEL_SPEED_MPH  # 3.0 min/mile at 20 mph


def haversine(lat1, lon1, lat2, lon2):
    """Great-circle distance in miles between two WGS84 points.

    Identical formula to ``trip_simulation4/env/ride_pool_env.py``
    (lines 49-59); copied here to keep this script standalone.
    """
    earth_radius_miles = 3958.8
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2)
    return 2 * earth_radius_miles * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# --- paths --------------------------------------------------------------

ROOT = Path(__file__).resolve().parent
DEFAULT_PLAN = ROOT / "trip_execution_plan_v4.xlsx"
DEFAULT_OUT = ROOT / "trip_execution_plan_v4_to_validate.xlsx"


REQUIRED_COLUMNS = (
    "decision_id", "trip_i_idx", "partner_idx", "outcome",
    "dist_i_mi", "dist_j_mi",
    "pickup_lat_i", "pickup_lon_i", "dropoff_lat_i", "dropoff_lon_i",
    "pickup_lat_j", "pickup_lon_j", "dropoff_lat_j", "dropoff_lon_j",
    "pickup_datetime_i",
)


def _check_schema(plan: pd.DataFrame, path: Path) -> None:
    """Fail fast with a clear message if the plan is from an older
    inference run that did not embed coordinates and pickup datetime."""
    missing = [c for c in REQUIRED_COLUMNS if c not in plan.columns]
    if missing:
        raise SystemExit(
            f"Plan at {path} is missing required columns: {missing}.\n"
            "This usually means it was produced by an older revision of "
            "trip_simulation4/agent/inference.py that did not embed the "
            "per-decision coordinates and pickup datetime. Re-run "
            "`python -m agent.inference` in trip_simulation4 to regenerate."
        )


def _row(vehicle_id, trip_id, vehicle_action,
         pickup_lat, pickup_lon, dropoff_lat, dropoff_lon,
         actual_pickup, actual_dropoff, trip_distance,
         decision_id, outcome):
    """One validator-schema row."""
    return {
        "vehicle_id": vehicle_id,
        "trip_id": trip_id,
        "vehicle_action": vehicle_action,
        "pickup_lat":  pickup_lat,
        "pickup_lon":  pickup_lon,
        "dropoff_lat": dropoff_lat,
        "dropoff_lon": dropoff_lon,
        "actual_pickup_datetime":  actual_pickup,
        "actual_dropoff_datetime": actual_dropoff,
        "trip_distance": trip_distance,
        "decision_id": decision_id,
        "outcome": outcome,
    }


def _build_rows(plan: pd.DataFrame) -> pd.DataFrame:
    """Expand every decision row into 2 (solo) or 4 (shared) action rows.

    Vehicle IDs are 1-indexed so they read naturally in Excel
    (``vehicle_id = decision_id + 1``). Trip IDs are the positional
    indices into the original test set; the v4 environment guarantees
    each trip is assigned at most once across the whole episode, so
    these are globally unique without further bookkeeping.
    """
    out: list[dict] = []

    for _, dec in plan.iterrows():
        decision_id = int(dec["decision_id"])
        vehicle_id = decision_id + 1
        outcome = dec["outcome"]
        i_idx = int(dec["trip_i_idx"])

        # Leader's coords + activation time live on the plan row itself.
        i_p_lat = float(dec["pickup_lat_i"])
        i_p_lon = float(dec["pickup_lon_i"])
        i_d_lat = float(dec["dropoff_lat_i"])
        i_d_lon = float(dec["dropoff_lon_i"])
        p_i_time = pd.to_datetime(dec["pickup_datetime_i"])

        # --------------------------------------------------------------
        # Solo / fallback_solo / vmt_reject_solo: only the leader rides.
        # --------------------------------------------------------------
        if outcome != "shared":
            dist_i = float(dec["dist_i_mi"])
            d_i_time = p_i_time + timedelta(minutes=dist_i * MIN_PER_MILE)
            out.append(_row(
                vehicle_id, i_idx, 1,
                i_p_lat, i_p_lon, i_d_lat, i_d_lon,
                p_i_time, None, dist_i, decision_id, outcome,
            ))
            out.append(_row(
                vehicle_id, i_idx, 2,
                i_p_lat, i_p_lon, i_d_lat, i_d_lon,
                None, d_i_time, dist_i, decision_id, outcome,
            ))
            continue

        # --------------------------------------------------------------
        # Shared: pickup_i -> pickup_j -> [dropoff_i, dropoff_j] or
        # [dropoff_j, dropoff_i] depending on the drop-order rule.
        # --------------------------------------------------------------
        j_idx = int(dec["partner_idx"])
        j_p_lat = float(dec["pickup_lat_j"])
        j_p_lon = float(dec["pickup_lon_j"])
        j_d_lat = float(dec["dropoff_lat_j"])
        j_d_lon = float(dec["dropoff_lon_j"])

        p1p2 = haversine(i_p_lat, i_p_lon, j_p_lat, j_p_lon)
        p2d1 = haversine(j_p_lat, j_p_lon, i_d_lat, i_d_lon)
        p2d2 = haversine(j_p_lat, j_p_lon, j_d_lat, j_d_lon)
        dist_i = float(dec["dist_i_mi"])
        dist_j = float(dec["dist_j_mi"])

        if p2d1 < p2d2:
            # Drop rider i first: p_i -> p_j -> d_i -> d_j.
            d1d2 = haversine(i_d_lat, i_d_lon, j_d_lat, j_d_lon)
            p_j_time = p_i_time + timedelta(minutes=p1p2 * MIN_PER_MILE)
            d_i_time = p_j_time + timedelta(minutes=p2d1 * MIN_PER_MILE)
            d_j_time = d_i_time + timedelta(minutes=d1d2 * MIN_PER_MILE)
            sequence = [
                (i_idx, 1, i_p_lat, i_p_lon, i_d_lat, i_d_lon,
                 p_i_time, None,     dist_i),
                (j_idx, 1, j_p_lat, j_p_lon, j_d_lat, j_d_lon,
                 p_j_time, None,     dist_j),
                (i_idx, 2, i_p_lat, i_p_lon, i_d_lat, i_d_lon,
                 None,     d_i_time, dist_i),
                (j_idx, 2, j_p_lat, j_p_lon, j_d_lat, j_d_lon,
                 None,     d_j_time, dist_j),
            ]
        else:
            # Drop rider j first: p_i -> p_j -> d_j -> d_i.
            d2d1 = haversine(j_d_lat, j_d_lon, i_d_lat, i_d_lon)
            p_j_time = p_i_time + timedelta(minutes=p1p2 * MIN_PER_MILE)
            d_j_time = p_j_time + timedelta(minutes=p2d2 * MIN_PER_MILE)
            d_i_time = d_j_time + timedelta(minutes=d2d1 * MIN_PER_MILE)
            sequence = [
                (i_idx, 1, i_p_lat, i_p_lon, i_d_lat, i_d_lon,
                 p_i_time, None,     dist_i),
                (j_idx, 1, j_p_lat, j_p_lon, j_d_lat, j_d_lon,
                 p_j_time, None,     dist_j),
                (j_idx, 2, j_p_lat, j_p_lon, j_d_lat, j_d_lon,
                 None,     d_j_time, dist_j),
                (i_idx, 2, i_p_lat, i_p_lon, i_d_lat, i_d_lon,
                 None,     d_i_time, dist_i),
            ]

        for (trip_id, action,
             p_lat, p_lon, d_lat, d_lon,
             p_t, d_t, dist_mi) in sequence:
            out.append(_row(
                vehicle_id, trip_id, action,
                p_lat, p_lon, d_lat, d_lon,
                p_t, d_t, dist_mi, decision_id, outcome,
            ))

    return pd.DataFrame(out)


def _summarise(plan: pd.DataFrame, rows: pd.DataFrame) -> None:
    """Print a short sanity report so you can eyeball the output before
    handing it to the validator."""
    n_decisions = len(plan)
    n_solo = (plan["outcome"] != "shared").sum()
    n_shared = (plan["outcome"] == "shared").sum()
    expected_rows = n_solo * 2 + n_shared * 4

    n_vehicles = rows["vehicle_id"].nunique()
    n_pick = int((rows["vehicle_action"] == 1).sum())
    n_drop = int((rows["vehicle_action"] == 2).sum())

    per_veh = rows.groupby("vehicle_id")["vehicle_action"]
    unbalanced = int((per_veh.apply(
        lambda s: (s == 1).sum() != (s == 2).sum())).sum())

    print(f"decisions     : {n_decisions:>7,}  (solo={n_solo:,}, shared={n_shared:,})")
    print(f"vehicles      : {n_vehicles:>7,}")
    print(f"rows written  : {len(rows):>7,}  (pickups={n_pick:,}, dropoffs={n_drop:,})")
    print(f"expected rows : {expected_rows:>7,}  (solo*2 + shared*4)")
    print(f"unbalanced veh: {unbalanced:>7}  (should be 0)")


def main():
    parser = argparse.ArgumentParser(
        description=("Bake trip_simulation4's trip_execution_plan_v4 into "
                     "the per-action schema consumed by trip_simulation.py."),
    )
    parser.add_argument(
        "--plan", default=str(DEFAULT_PLAN),
        help=("v4 execution plan (.xlsx, one row per decision, with the "
              "per-decision lat/lon and pickup_datetime columns added by "
              "trip_simulation4/agent/inference.py). "
              f"Default: {DEFAULT_PLAN}"),
    )
    parser.add_argument(
        "--out", default=str(DEFAULT_OUT),
        help=("Output workbook in the validator's per-action schema. "
              f"Default: {DEFAULT_OUT}"),
    )
    args = parser.parse_args()

    plan_path = Path(args.plan)
    plan = pd.read_excel(plan_path)
    _check_schema(plan, plan_path)
    rows = _build_rows(plan)

    _summarise(plan, rows)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rows.to_excel(out_path, index=False)
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
