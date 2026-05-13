#!/usr/bin/env python3
"""
trip_simulation.py
==================

Simulator for shared-ride vehicle trips. Vehicles move along
real OpenStreetMap road networks (via OSRM) on an interactive Leaflet map, with
runtime Speed Up / Slow Down / Pause / Restart controls and a time slider.

Design highlights
-----------------
* Configurable input Excel file and base vehicle speed at the top of the script.
* Strict per-vehicle row ordering from the Excel file.
* Time-based vehicle activation (``actual_pickup_datetime`` of the first row).
* Shared trips supported (multiple passengers onboard concurrently).
* Real road-network routing via OSRM (public server by default, or a local
  Docker instance). Routes are cached on disk.
* Travel durations are computed from ``VEHICLE_SPEED_MPH`` and route distance
  (never from OSRM's own ``duration`` field).
* Robust fallback to a straight-line segment if routing fails for any pair.
* Self-contained animated HTML output with UI controls.

Usage
-----
    python trip_simulation.py

Environment overrides (optional):
    INPUT_FILE=my_plan.xlsx VEHICLE_SPEED_MPH=25 python trip_simulation.py
"""

from __future__ import annotations

import json
import logging
import math
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests
import folium

# ---------------------------------------------------------------------------
# Configuration (edit these or override via environment variables)
# ---------------------------------------------------------------------------

INPUT_FILE = os.environ.get("INPUT_FILE", "sample_trip_execution_plan.xlsx")
VEHICLE_SPEED_MPH = float(os.environ.get("VEHICLE_SPEED_MPH", "20"))

# OSRM endpoint. Point this to your local Docker instance for production use.
#   Public demo server: "http://router.project-osrm.org"
#   Local Docker:       "http://localhost:5000"
OSRM_BASE_URL = os.environ.get("OSRM_BASE_URL", "http://router.project-osrm.org")

# Routing backend:
#   "osrm"  - use OSRM only (default)
#   "osmnx" - use OSMnx (pure Python, downloads a local OSM graph via Overpass)
#   "auto"  - try OSRM first, fall back to OSMnx, then straight-line
ROUTING_BACKEND = os.environ.get("ROUTING_BACKEND", "osrm").lower()

OUTPUT_HTML = os.environ.get("OUTPUT_HTML", "trip_simulation.html")
# Excel file (mirrors the input plan) augmented with simulator-derived
# validated_* columns. Set to "" to skip writing it.
OUTPUT_PLAN_FILE = os.environ.get(
    "OUTPUT_PLAN_FILE", "validated_trip_execution_plan.xlsx"
)
ROUTE_CACHE_FILE = os.environ.get("ROUTE_CACHE_FILE", ".route_cache.json")

# Polite delay between OSRM calls (seconds) to avoid hammering the public server.
OSRM_REQUEST_DELAY = float(os.environ.get("OSRM_REQUEST_DELAY", "0.2"))
OSRM_TIMEOUT = float(os.environ.get("OSRM_TIMEOUT", "15"))

# OSMnx graph buffer (metres) added around the bounding box of all waypoints.
OSMNX_BUFFER_M = float(os.environ.get("OSMNX_BUFFER_M", "1500"))
OSMNX_NETWORK_TYPE = os.environ.get("OSMNX_NETWORK_TYPE", "drive")

# Optional cap on number of vehicles (useful for huge plans whose full render
# would overwhelm the browser). ``0`` or unset = no cap.
MAX_VEHICLES = int(os.environ.get("MAX_VEHICLES", "0"))

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MPH_TO_MPS = 0.44704
EARTH_RADIUS_M = 6_371_000.0

# Distinct, colour-blind friendly palette (cycled if more vehicles than colours).
VEHICLE_PALETTE = [
    "#e6194B", "#3cb44b", "#4363d8", "#f58231", "#911eb4",
    "#42d4f4", "#f032e6", "#9A6324", "#800000", "#808000",
    "#000075", "#469990", "#bfef45", "#fabed4", "#aaffc3",
]

ACTION_PICKUP = 1
ACTION_DROPOFF = 2

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("trip_sim")


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def haversine_m(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    """Great-circle distance in metres. Points are ``(lat, lon)``."""
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(h))


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Action:
    """A single row from the input plan, resolved to a target location."""
    row_index: int
    trip_id: int
    kind: str                       # "pickup" or "dropoff"
    lat: float
    lon: float
    scheduled_time: Optional[pd.Timestamp]  # actual_pickup / actual_dropoff datetime


@dataclass
class Segment:
    """One traversed segment of a vehicle's schedule (travel + arrival event)."""
    start_t: float                  # simulation seconds
    end_t: float                    # simulation seconds
    total_distance_m: float
    coords: List[List[float]]       # polyline as [[lat, lon], ...]
    cum_dist_m: List[float]         # cumulative distance per vertex (metres)
    event_kind: str                 # "pickup" | "dropoff"
    event_trip_id: int
    used_fallback: bool


@dataclass
class Vehicle:
    vehicle_id: int
    color: str
    actions: List[Action]
    activation_time: float = 0.0                     # simulation seconds
    spawn_lat: float = 0.0
    spawn_lon: float = 0.0
    first_trip_id: int = 0
    segments: List[Segment] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

class RouteManager:
    """Fetches routes from OSRM and/or OSMnx with on-disk caching and graceful fallback.

    Three backends are selectable via the ``backend`` argument:

    * ``"osrm"``  - OSRM HTTP API (default)
    * ``"osmnx"`` - local OSMnx/NetworkX graph (downloaded once via Overpass)
    * ``"auto"``  - try OSRM first, then OSMnx, then a straight-line fallback
    """

    def __init__(
        self,
        base_url: str = OSRM_BASE_URL,
        cache_file: str = ROUTE_CACHE_FILE,
        timeout: float = OSRM_TIMEOUT,
        request_delay: float = OSRM_REQUEST_DELAY,
        backend: str = ROUTING_BACKEND,
        waypoints: Optional[List[Tuple[float, float]]] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.cache_path = Path(cache_file)
        self.timeout = timeout
        self.request_delay = request_delay
        self.backend = backend if backend in {"osrm", "osmnx", "auto"} else "osrm"
        self.cache: Dict[str, Dict[str, Any]] = self._load_cache()
        self.session = requests.Session()
        self._last_call = 0.0
        self.api_calls = 0
        self.cache_hits = 0
        self._pending_saves = 0
        self._save_every = int(os.environ.get("ROUTE_CACHE_SAVE_EVERY", "200"))

        # Lazy-loaded OSMnx graph + helpers.
        self._osmnx_waypoints = waypoints or []
        self._osmnx_graph = None
        self._osmnx_node_xy = None       # dict: node -> (lon, lat)
        self._osmnx_node_ids = None      # list[node_id] in tree order
        self._osmnx_node_tree = None     # scipy cKDTree over node coordinates
        self._osmnx_ready = False

    # ------------------------------------------------------------------
    # Cache I/O
    # ------------------------------------------------------------------
    def _load_cache(self) -> Dict[str, Dict[str, Any]]:
        if self.cache_path.exists():
            try:
                with self.cache_path.open("r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:  # pragma: no cover - corrupt cache
                log.warning("Could not read route cache (%s); starting fresh.", e)
        return {}

    def _save_cache(self) -> None:
        try:
            with self.cache_path.open("w", encoding="utf-8") as f:
                json.dump(self.cache, f)
        except Exception as e:  # pragma: no cover
            log.warning("Could not write route cache: %s", e)

    @staticmethod
    def _key(start_lonlat: Tuple[float, float], end_lonlat: Tuple[float, float]) -> str:
        return (
            f"{round(start_lonlat[0], 6)},{round(start_lonlat[1], 6)}"
            f"|{round(end_lonlat[0], 6)},{round(end_lonlat[1], 6)}"
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def get_route(
        self,
        start_lonlat: Tuple[float, float],
        end_lonlat: Tuple[float, float],
    ) -> Dict[str, Any]:
        """Return a dict with ``coords`` (``[[lat, lon], ...]``), ``distance`` (m),
        ``cum_dist`` (list of metres), and ``fallback`` flag.

        Coordinates passed in are ``(lon, lat)`` to match OSRM's convention.
        """
        key = self._key(start_lonlat, end_lonlat)
        if key in self.cache:
            self.cache_hits += 1
            return self.cache[key]

        result: Optional[Dict[str, Any]] = None
        if self.backend == "osrm":
            result = self._query_osrm(start_lonlat, end_lonlat)
        elif self.backend == "osmnx":
            result = self._query_osmnx(start_lonlat, end_lonlat)
        else:  # "auto"
            result = self._query_osrm(start_lonlat, end_lonlat)
            if result.get("fallback"):
                osmnx_result = self._query_osmnx(start_lonlat, end_lonlat)
                if not osmnx_result.get("fallback"):
                    result = osmnx_result

        self.cache[key] = result
        self._pending_saves += 1
        # Persist periodically so long runs are resilient to interrupts, but
        # not after every call (disk I/O dominates for thousands of routes).
        if self._pending_saves >= self._save_every:
            self._save_cache()
            self._pending_saves = 0
        return result

    def flush(self) -> None:
        """Force-persist the cache to disk. Call once at the end of a run."""
        if self._pending_saves > 0:
            self._save_cache()
            self._pending_saves = 0

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _query_osrm(
        self,
        start_lonlat: Tuple[float, float],
        end_lonlat: Tuple[float, float],
    ) -> Dict[str, Any]:
        self._respect_rate_limit()
        url = (
            f"{self.base_url}/route/v1/driving/"
            f"{start_lonlat[0]},{start_lonlat[1]};{end_lonlat[0]},{end_lonlat[1]}"
        )
        params = {"overview": "full", "geometries": "geojson", "steps": "false"}
        try:
            log.debug("OSRM route %s -> %s", start_lonlat, end_lonlat)
            resp = self.session.get(url, params=params, timeout=self.timeout)
            self.api_calls += 1
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") != "Ok" or not data.get("routes"):
                raise ValueError(f"OSRM returned code={data.get('code')!r}")

            route = data["routes"][0]
            geom_lonlat = route["geometry"]["coordinates"]  # [[lon, lat], ...]
            coords = [[lat, lon] for lon, lat in geom_lonlat]
            distance_m = float(route["distance"])
            cum = self._cumulative_distances(coords, target_total=distance_m)
            return {
                "coords": coords,
                "distance": distance_m,
                "cum_dist": cum,
                "fallback": False,
            }
        except Exception as e:
            log.warning("OSRM failure (%s); using straight-line fallback.", e)
            return self._straight_line_fallback(start_lonlat, end_lonlat)

    # ------------------------------------------------------------------
    # OSMnx backend
    # ------------------------------------------------------------------
    def _ensure_osmnx_graph(self) -> bool:
        """Lazy-load the OSMnx graph covering all known waypoints. Returns True on success."""
        if self._osmnx_ready:
            return self._osmnx_graph is not None
        self._osmnx_ready = True  # only try once

        if not self._osmnx_waypoints:
            log.warning("OSMnx backend requested but no waypoints provided.")
            return False

        try:
            import osmnx as ox  # noqa: F401 - imported for side-effect/availability
        except Exception as e:
            log.warning("OSMnx not installed (%s). Run: pip install osmnx networkx", e)
            return False

        try:
            import osmnx as ox
            lats = [p[1] for p in self._osmnx_waypoints]
            lons = [p[0] for p in self._osmnx_waypoints]
            north, south = max(lats), min(lats)
            east, west = max(lons), min(lons)

            # Convert buffer metres to degrees (~111 km / degree latitude).
            buf_lat = OSMNX_BUFFER_M / 111_320.0
            # Longitude degree shrinks with latitude.
            mean_lat_rad = math.radians((north + south) / 2)
            buf_lon = OSMNX_BUFFER_M / (111_320.0 * max(math.cos(mean_lat_rad), 1e-6))
            bbox = (
                west - buf_lon,
                south - buf_lat,
                east + buf_lon,
                north + buf_lat,
            )  # (left, bottom, right, top) per OSMnx 2.x

            log.info("OSMnx: downloading drive network for bbox %s (buffer %.0f m)...",
                     bbox, OSMNX_BUFFER_M)
            t0 = time.monotonic()
            # OSMnx 2.x takes bbox as a single tuple kwarg.
            try:
                graph = ox.graph_from_bbox(bbox=bbox, network_type=OSMNX_NETWORK_TYPE)
            except TypeError:
                # OSMnx 1.x positional signature (north, south, east, west)
                graph = ox.graph_from_bbox(
                    north=bbox[3], south=bbox[1], east=bbox[2], west=bbox[0],
                    network_type=OSMNX_NETWORK_TYPE,
                )
            dt = time.monotonic() - t0
            log.info("OSMnx: graph loaded in %.1fs (%d nodes, %d edges).",
                     dt, graph.number_of_nodes(), graph.number_of_edges())
            self._osmnx_graph = graph
            self._osmnx_node_xy = {
                n: (data["x"], data["y"]) for n, data in graph.nodes(data=True)
            }
            # Build a scipy cKDTree over node (lon, lat) for fast nearest-node
            # lookup. This avoids ``ox.distance.nearest_nodes`` rebuilding its
            # tree on every call - which is catastrophic for 80k+ nodes.
            try:
                from scipy.spatial import cKDTree
                import numpy as np
                self._osmnx_node_ids = list(self._osmnx_node_xy.keys())
                coords = np.array(
                    [self._osmnx_node_xy[n] for n in self._osmnx_node_ids],
                    dtype=float,
                )
                self._osmnx_node_tree = cKDTree(coords)
                log.info("OSMnx: cKDTree built over %d nodes.", len(coords))
            except Exception as e:
                log.warning("OSMnx: could not build spatial index (%s); "
                            "falling back to slow per-call nearest_nodes.", e)
                self._osmnx_node_tree = None
            return True
        except Exception as e:
            log.warning("OSMnx graph download failed: %s", e)
            self._osmnx_graph = None
            return False

    def _nearest_node(self, lonlat: Tuple[float, float]):
        """Fast nearest-node lookup using the cached cKDTree. Falls back to
        ``ox.distance.nearest_nodes`` if the tree isn't available."""
        if self._osmnx_node_tree is not None:
            _, idx = self._osmnx_node_tree.query([lonlat[0], lonlat[1]])
            return self._osmnx_node_ids[idx]
        import osmnx as ox
        return ox.distance.nearest_nodes(
            self._osmnx_graph, X=lonlat[0], Y=lonlat[1],
        )

    def _query_osmnx(
        self,
        start_lonlat: Tuple[float, float],
        end_lonlat: Tuple[float, float],
    ) -> Dict[str, Any]:
        if not self._ensure_osmnx_graph():
            return self._straight_line_fallback(start_lonlat, end_lonlat)
        try:
            import networkx as nx
            graph = self._osmnx_graph
            orig = self._nearest_node(start_lonlat)
            dest = self._nearest_node(end_lonlat)
            path = nx.shortest_path(graph, orig, dest, weight="length")

            coords: List[List[float]] = []
            total_dist = 0.0
            for u, v in zip(path[:-1], path[1:]):
                edge_data = graph.get_edge_data(u, v)
                if not edge_data:
                    continue
                # Pick the shortest parallel edge.
                edge = min(edge_data.values(), key=lambda d: d.get("length", float("inf")))
                geom = edge.get("geometry")
                if geom is not None:
                    pts = list(geom.coords)  # [(lon, lat), ...]
                else:
                    pts = [self._osmnx_node_xy[u], self._osmnx_node_xy[v]]
                for lon, lat in pts:
                    if not coords or coords[-1] != [lat, lon]:
                        coords.append([lat, lon])
                total_dist += float(edge.get("length", 0.0))

            if len(coords) < 2:
                return self._straight_line_fallback(start_lonlat, end_lonlat)

            # Anchor route to the requested start/end points, then (re)compute the
            # cumulative distance from the actual polyline we will animate along.
            if coords[0] != [start_lonlat[1], start_lonlat[0]]:
                coords.insert(0, [start_lonlat[1], start_lonlat[0]])
            if coords[-1] != [end_lonlat[1], end_lonlat[0]]:
                coords.append([end_lonlat[1], end_lonlat[0]])
            cum = self._cumulative_distances(coords, target_total=0.0)
            distance_m = cum[-1]
            self.api_calls += 1
            return {
                "coords": coords,
                "distance": distance_m,
                "cum_dist": cum,
                "fallback": False,
            }
        except Exception as e:
            log.warning("OSMnx routing failed (%s); using straight-line fallback.", e)
            return self._straight_line_fallback(start_lonlat, end_lonlat)

    @staticmethod
    def _cumulative_distances(coords: List[List[float]], target_total: float) -> List[float]:
        cum = [0.0]
        for i in range(1, len(coords)):
            cum.append(cum[-1] + haversine_m(coords[i - 1], coords[i]))
        # Rescale so the final cumulative distance matches the OSRM-reported total.
        if cum[-1] > 0 and target_total > 0 and abs(cum[-1] - target_total) > 0.5:
            scale = target_total / cum[-1]
            cum = [c * scale for c in cum]
        return cum

    @staticmethod
    def _straight_line_fallback(
        start_lonlat: Tuple[float, float],
        end_lonlat: Tuple[float, float],
    ) -> Dict[str, Any]:
        coords = [
            [start_lonlat[1], start_lonlat[0]],
            [end_lonlat[1], end_lonlat[0]],
        ]
        d = haversine_m(coords[0], coords[1])
        return {
            "coords": coords,
            "distance": d,
            "cum_dist": [0.0, d],
            "fallback": True,
        }

    def _respect_rate_limit(self) -> None:
        if self.request_delay <= 0:
            return
        dt = time.monotonic() - self._last_call
        if dt < self.request_delay:
            time.sleep(self.request_delay - dt)
        self._last_call = time.monotonic()


# ---------------------------------------------------------------------------
# Data loading and vehicle construction
# ---------------------------------------------------------------------------

REQUIRED_COLUMNS = [
    "vehicle_id", "trip_id",
    "pickup_lon", "pickup_lat", "dropoff_lon", "dropoff_lat",
    "vehicle_action", "actual_pickup_datetime",
]


def load_data(path: str) -> pd.DataFrame:
    """Read the Excel plan and validate required columns."""
    log.info("Loading plan: %s", path)
    df = pd.read_excel(path)
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"Input file is missing required columns: {missing}")
    # Preserve original row ordering (this is the authoritative per-vehicle order).
    df = df.reset_index(drop=True)
    df["_row"] = df.index
    # Normalise datetime columns
    for col in ("actual_pickup_datetime", "actual_dropoff_datetime"):
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")
    log.info("Loaded %d rows, %d vehicle(s).", len(df), df["vehicle_id"].nunique())
    return df


def _row_to_action(row: pd.Series) -> Action:
    action_code = int(row["vehicle_action"])
    if action_code == ACTION_PICKUP:
        kind = "pickup"
        lat, lon = float(row["pickup_lat"]), float(row["pickup_lon"])
        sched = row.get("actual_pickup_datetime")
    elif action_code == ACTION_DROPOFF:
        kind = "dropoff"
        lat, lon = float(row["dropoff_lat"]), float(row["dropoff_lon"])
        sched = row.get("actual_dropoff_datetime")
    else:
        raise ValueError(f"Unknown vehicle_action={action_code!r} at row {row['_row']}")
    return Action(
        row_index=int(row["_row"]),
        trip_id=int(row["trip_id"]),
        kind=kind,
        lat=lat,
        lon=lon,
        scheduled_time=sched if pd.notna(sched) else None,
    )


def initialize_vehicles(df: pd.DataFrame) -> List[Vehicle]:
    """Build ``Vehicle`` objects from the dataframe, preserving row order per vehicle."""
    vehicles: List[Vehicle] = []
    # Group by vehicle_id but preserve each vehicle's first-appearance order.
    vehicle_order = list(dict.fromkeys(df["vehicle_id"].tolist()))
    for idx, vid in enumerate(vehicle_order):
        sub = df[df["vehicle_id"] == vid].sort_values("_row")
        actions = [_row_to_action(r) for _, r in sub.iterrows()]
        if not actions:
            continue
        if actions[0].kind != "pickup":
            log.warning(
                "Vehicle %s first action is not a pickup; the simulator will still "
                "honour the row order but this is unusual.", vid,
            )
        vehicles.append(
            Vehicle(
                vehicle_id=int(vid),
                color=VEHICLE_PALETTE[idx % len(VEHICLE_PALETTE)],
                actions=actions,
                spawn_lat=actions[0].lat,
                spawn_lon=actions[0].lon,
                first_trip_id=actions[0].trip_id,
            )
        )
    if MAX_VEHICLES and len(vehicles) > MAX_VEHICLES:
        # Keep the earliest-activating vehicles (so the subset is temporally
        # contiguous and likely to overlap on the map). Ties broken by first
        # appearance order.
        def _sort_key(v: Vehicle) -> Tuple[float, int]:
            first = v.actions[0].scheduled_time
            ts = first.timestamp() if first is not None else float("inf")
            return (ts, v.vehicle_id)
        vehicles = sorted(vehicles, key=_sort_key)[:MAX_VEHICLES]
        log.info("MAX_VEHICLES=%d in effect: keeping %d earliest-activating vehicles.",
                 MAX_VEHICLES, len(vehicles))
    log.info("Built %d vehicle(s).", len(vehicles))
    return vehicles


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------

def _determine_activation_times(vehicles: List[Vehicle]) -> pd.Timestamp:
    """Set each vehicle's ``activation_time`` in seconds relative to global t=0.

    Global t=0 is the earliest ``actual_pickup_datetime`` across all first actions.
    Vehicles whose first action has no timestamp are activated at t=0.
    Returns the global start timestamp (for display).
    """
    firsts: List[pd.Timestamp] = [
        v.actions[0].scheduled_time for v in vehicles
        if v.actions[0].scheduled_time is not None
    ]
    if not firsts:
        global_start = pd.Timestamp("1970-01-01")
        for v in vehicles:
            v.activation_time = 0.0
        return global_start

    global_start = min(firsts)
    for v in vehicles:
        t0 = v.actions[0].scheduled_time
        if t0 is None:
            v.activation_time = 0.0
        else:
            v.activation_time = (t0 - global_start).total_seconds()
    return global_start


def simulate_vehicle_movements(
    vehicles: List[Vehicle],
    route_mgr: RouteManager,
    speed_mph: float = VEHICLE_SPEED_MPH,
) -> pd.Timestamp:
    """Compute per-vehicle segments (routes, durations, events)."""
    global_start = _determine_activation_times(vehicles)
    speed_mps = speed_mph * MPH_TO_MPS
    if speed_mps <= 0:
        raise ValueError("VEHICLE_SPEED_MPH must be > 0")

    total_segments_needed = sum(max(0, len(v.actions) - 1) for v in vehicles)
    verbose_per_vehicle = len(vehicles) <= 10
    segments_done = 0
    t_start = time.monotonic()

    for v in vehicles:
        if verbose_per_vehicle:
            log.info("Routing for vehicle %s (%d actions)...",
                     v.vehicle_id, len(v.actions))
        current_t = v.activation_time
        for i in range(1, len(v.actions)):
            prev = v.actions[i - 1]
            nxt = v.actions[i]
            route = route_mgr.get_route(
                (prev.lon, prev.lat), (nxt.lon, nxt.lat),
            )
            distance_m = float(route["distance"])
            duration_s = distance_m / speed_mps if distance_m > 0 else 0.0
            seg = Segment(
                start_t=current_t,
                end_t=current_t + duration_s,
                total_distance_m=distance_m,
                coords=route["coords"],
                cum_dist_m=route["cum_dist"],
                event_kind=nxt.kind,
                event_trip_id=nxt.trip_id,
                used_fallback=bool(route.get("fallback", False)),
            )
            v.segments.append(seg)
            current_t = seg.end_t
            segments_done += 1
            if not verbose_per_vehicle and segments_done % 200 == 0:
                elapsed = time.monotonic() - t_start
                rate = segments_done / max(elapsed, 1e-6)
                eta = (total_segments_needed - segments_done) / max(rate, 1e-6)
                log.info(
                    "Routed %d/%d segments (%.0f/s, elapsed %.0fs, ETA %.0fs)",
                    segments_done, total_segments_needed, rate, elapsed, eta,
                )

    route_mgr.flush()
    log.info(
        "Simulation computed in %.1fs. API calls=%d, cache hits=%d.",
        time.monotonic() - t_start, route_mgr.api_calls, route_mgr.cache_hits,
    )
    return global_start


# ---------------------------------------------------------------------------
# Validated plan export
# ---------------------------------------------------------------------------

def export_validated_plan(
    df: pd.DataFrame,
    vehicles: List["Vehicle"],
    global_start: pd.Timestamp,
    output_path: str,
) -> Optional[pd.DataFrame]:
    """Write an Excel file mirroring the input plan, with four extra columns:

    * ``validated_pickup_datetime``  - simulator-derived pickup datetime,
      populated on pickup rows only (``vehicle_action == 1``). For the first
      action of each vehicle this equals ``actual_pickup_datetime`` (the
      vehicle activates at its scheduled start). For every later pickup it is
      the arrival time at the pickup location, computed from cumulative
      route distance and ``VEHICLE_SPEED_MPH``.
    * ``validated_dropoff_datetime`` - simulator-derived dropoff datetime,
      populated on dropoff rows only (``vehicle_action == 2``). Always
      derived from cumulative route distance and ``VEHICLE_SPEED_MPH``.
    * ``actual_time_in_vehicle``    - per ``trip_id`` duration computed as
      ``actual_dropoff_datetime - actual_pickup_datetime``. Stamped on both
      the pickup and dropoff rows of each trip.
    * ``validated_time_in_vehicle`` - per ``trip_id`` duration computed as
      ``validated_dropoff_datetime - validated_pickup_datetime``. Stamped on
      both the pickup and dropoff rows of each trip.

    Rows that belong to vehicles not included in the simulation (e.g. when
    ``MAX_VEHICLES`` is in effect) keep NaT in the validated columns.

    Returns the augmented DataFrame (also written to ``output_path`` if a
    non-empty path is provided), or ``None`` if no path was given.
    """
    if not output_path:
        return None

    out = df.copy()
    out["validated_pickup_datetime"] = pd.NaT
    out["validated_dropoff_datetime"] = pd.NaT

    # 1) Per-action arrival datetimes from the computed segments.
    for v in vehicles:
        for i, action in enumerate(v.actions):
            if i == 0:
                arrival_s = v.activation_time
            else:
                arrival_s = v.segments[i - 1].end_t
            arrival_dt = global_start + pd.Timedelta(seconds=arrival_s)
            col = (
                "validated_pickup_datetime"
                if action.kind == "pickup"
                else "validated_dropoff_datetime"
            )
            out.loc[action.row_index, col] = arrival_dt

    # 2) Per-trip durations (stamped on both the pickup and dropoff rows).
    out["actual_time_in_vehicle"] = pd.Series(
        pd.NaT, index=out.index, dtype="timedelta64[ns]"
    )
    out["validated_time_in_vehicle"] = pd.Series(
        pd.NaT, index=out.index, dtype="timedelta64[ns]"
    )
    actual_dropoff = (
        out["actual_dropoff_datetime"] if "actual_dropoff_datetime" in out.columns
        else pd.Series(pd.NaT, index=out.index)
    )
    for (vid, tid), group in out.groupby(["vehicle_id", "trip_id"], sort=False):
        pickup_rows = group[group["vehicle_action"] == ACTION_PICKUP]
        dropoff_rows = group[group["vehicle_action"] == ACTION_DROPOFF]
        if pickup_rows.empty or dropoff_rows.empty:
            continue
        p_idx = pickup_rows.index[0]
        d_idx = dropoff_rows.index[0]

        a_pick = out.at[p_idx, "actual_pickup_datetime"]
        a_drop = actual_dropoff.at[d_idx]
        if pd.notna(a_pick) and pd.notna(a_drop):
            actual_dur = a_drop - a_pick
            out.at[p_idx, "actual_time_in_vehicle"] = actual_dur
            out.at[d_idx, "actual_time_in_vehicle"] = actual_dur

        v_pick = out.at[p_idx, "validated_pickup_datetime"]
        v_drop = out.at[d_idx, "validated_dropoff_datetime"]
        if pd.notna(v_pick) and pd.notna(v_drop):
            valid_dur = v_drop - v_pick
            out.at[p_idx, "validated_time_in_vehicle"] = valid_dur
            out.at[d_idx, "validated_time_in_vehicle"] = valid_dur

    # 3) Drop the helper column added by load_data and write to disk.
    if "_row" in out.columns:
        out = out.drop(columns=["_row"])

    # Excel can't store true Python timedeltas directly; stringify durations
    # as HH:MM:SS for readability while keeping the datetime columns intact.
    def _fmt_td(td: Any) -> Any:
        if pd.isna(td):
            return pd.NaT
        total = int(pd.Timedelta(td).total_seconds())
        sign = "-" if total < 0 else ""
        total = abs(total)
        h, rem = divmod(total, 3600)
        m, s = divmod(rem, 60)
        return f"{sign}{h:02d}:{m:02d}:{s:02d}"

    for col in ("actual_time_in_vehicle", "validated_time_in_vehicle"):
        out[col] = out[col].map(_fmt_td)

    try:
        out.to_excel(output_path, index=False)
        log.info("Wrote validated plan: %s (%d rows)", output_path, len(out))
    except PermissionError as e:
        log.error("Could not write %s (file open in Excel?). %s", output_path, e)
    except Exception as e:  # pragma: no cover
        log.error("Could not write %s: %s", output_path, e)
    return out


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _map_center(vehicles: List[Vehicle]) -> Tuple[float, float]:
    pts: List[Tuple[float, float]] = []
    for v in vehicles:
        for a in v.actions:
            pts.append((a.lat, a.lon))
    if not pts:
        return (40.75, -73.98)
    lat = sum(p[0] for p in pts) / len(pts)
    lon = sum(p[1] for p in pts) / len(pts)
    return lat, lon


def _simulation_duration(vehicles: List[Vehicle]) -> float:
    end = 0.0
    for v in vehicles:
        if v.segments:
            end = max(end, v.segments[-1].end_t)
        else:
            end = max(end, v.activation_time)
    return end


def _vehicles_to_json(vehicles: List[Vehicle]) -> List[Dict[str, Any]]:
    payload = []
    for v in vehicles:
        payload.append({
            "id": v.vehicle_id,
            "color": v.color,
            "activation_time": v.activation_time,
            "spawn": [v.spawn_lat, v.spawn_lon],
            "first_trip_id": v.first_trip_id,
            "segments": [
                {
                    "start_t": s.start_t,
                    "end_t": s.end_t,
                    "total_distance_m": s.total_distance_m,
                    "coords": s.coords,
                    "cum_dist_m": s.cum_dist_m,
                    "event_kind": s.event_kind,
                    "event_trip_id": s.event_trip_id,
                    "fallback": s.used_fallback,
                }
                for s in v.segments
            ],
        })
    return payload


def _build_trips(vehicles: List[Vehicle]) -> List[Dict[str, Any]]:
    """Collect per-passenger data for the passenger-stickman layer.

    Each entry describes a single ``trip_id`` ridden on a single ``vehicle_id``
    and carries:

    * ``pickup_t`` / ``pickup_pos`` - when/where the passenger starts waiting
      and becomes "onboard". The first action of a vehicle is a pickup at its
      activation_time; subsequent pickups are the arrival time at that action
      (equal to the end_t of the segment that travels to it).
    * ``dropoff_t`` / ``dropoff_pos`` - when/where the passenger is dropped off.
      ``None`` when the plan has no dropoff row for that trip.
    """
    trips: Dict[Tuple[int, int], Dict[str, Any]] = {}
    for v in vehicles:
        # Arrival time of action[i]: activation_time for i=0, segments[i-1].end_t otherwise.
        arrival_times: List[float] = [v.activation_time]
        for s in v.segments:
            arrival_times.append(s.end_t)

        for action, arr_t in zip(v.actions, arrival_times):
            key = (v.vehicle_id, action.trip_id)
            entry = trips.setdefault(key, {
                "trip_id": action.trip_id,
                "vehicle_id": v.vehicle_id,
                "vehicle_color": v.color,
                "pickup_t": None,
                "pickup_pos": None,
                "dropoff_t": None,
                "dropoff_pos": None,
            })
            if action.kind == "pickup":
                entry["pickup_t"] = arr_t
                entry["pickup_pos"] = [action.lat, action.lon]
            else:
                entry["dropoff_t"] = arr_t
                entry["dropoff_pos"] = [action.lat, action.lon]
    return list(trips.values())


def render_map(
    vehicles: List[Vehicle],
    global_start: pd.Timestamp,
    speed_mph: float,
    output_path: str,
) -> None:
    """Build and save the animated HTML map."""
    center = _map_center(vehicles)
    sim_duration = _simulation_duration(vehicles)

    m = folium.Map(
        location=center,
        zoom_start=13,
        tiles="OpenStreetMap",
        control_scale=True,
    )

    # --- Static layers: pickup/dropoff markers + full route polylines --------
    for v in vehicles:
        # Draw the vehicle's full planned route as a faint polyline.
        for seg in v.segments:
            folium.PolyLine(
                locations=seg.coords,
                color=v.color,
                weight=3,
                opacity=0.35,
                dash_array="4,6" if seg.used_fallback else None,
            ).add_to(m)

        for a in v.actions:
            icon_color = "green" if a.kind == "pickup" else "red"
            icon_symbol = "arrow-up" if a.kind == "pickup" else "arrow-down"
            folium.Marker(
                location=[a.lat, a.lon],
                popup=folium.Popup(
                    html=(
                        f"<b>{a.kind.title()}</b><br>"
                        f"Vehicle: {v.vehicle_id}<br>"
                        f"Trip: {a.trip_id}<br>"
                        f"Scheduled: {a.scheduled_time}"
                    ),
                    max_width=260,
                ),
                icon=folium.Icon(color=icon_color, icon=icon_symbol, prefix="fa"),
            ).add_to(m)

    # --- Legend --------------------------------------------------------------
    legend_rows = "".join(
        f"<div style='display:flex;align-items:center;margin:2px 0;'>"
        f"<span style='display:inline-block;width:14px;height:14px;background:{v.color};"
        f"border-radius:50%;margin-right:6px;border:1px solid #333;'></span>"
        f"Vehicle {v.vehicle_id}</div>"
        for v in vehicles
    )
    legend_html = f"""
    <div id="sim-legend" style="position:fixed; bottom:24px; left:12px; z-index:9998;
        background:white; padding:10px 12px; border-radius:8px;
        box-shadow:0 2px 6px rgba(0,0,0,0.3); font-family:sans-serif; font-size:13px;">
      <div style="font-weight:600;margin-bottom:4px;">Legend</div>
      <div><span style="color:green;">&#9679;</span> Pickup location</div>
      <div><span style="color:red;">&#9679;</span> Dropoff location</div>
      <div style="margin-top:4px;">
        <span style="display:inline-block;width:10px;height:10px;background:#6b7280;border-radius:50%;
          border:1px solid #111;margin-right:4px;vertical-align:middle;"></span>
        Passenger waiting / onboard
      </div>
      <div>
        <span style="display:inline-block;width:10px;height:10px;background:#16a34a;border-radius:50%;
          border:1px solid #111;margin-right:4px;vertical-align:middle;"></span>
        Passenger dropped off
      </div>
      <hr style="margin:6px 0;">
      {legend_rows}
    </div>
    """
    m.get_root().html.add_child(folium.Element(legend_html))

    # --- Control panel (Speed Up / Slow Down / Pause / Restart + slider) ----
    controls_html = """
    <div id="sim-controls" style="position:fixed; top:12px; right:12px; z-index:9999;
        background:white; padding:12px 14px; border-radius:8px;
        box-shadow:0 2px 6px rgba(0,0,0,0.3); font-family:sans-serif; font-size:13px;
        min-width:260px;">
      <div style="font-weight:600;margin-bottom:6px;">Simulation Controls</div>
      <div style="display:flex;gap:6px;margin-bottom:6px;flex-wrap:wrap;">
        <button id="btn-slow"  style="cursor:pointer;">&laquo; Slow Down</button>
        <button id="btn-play"  style="cursor:pointer;">Pause</button>
        <button id="btn-fast"  style="cursor:pointer;">Speed Up &raquo;</button>
        <button id="btn-reset" style="cursor:pointer;">Restart</button>
      </div>
      <div>Multiplier: <span id="speed-val">1.00x</span>
        &nbsp; Base: <span id="base-speed"></span> mph</div>
      <div>Sim time: <span id="time-val">--:--:--</span></div>
      <div>Wall clock: <span id="clock-val">--:--:--</span></div>
      <div style="margin-top:6px;">
        <input type="range" id="time-slider" min="0" max="1000" value="0"
               style="width:100%;">
      </div>
      <div style="margin-top:4px;font-size:11px;color:#555;">
        Dashed lines = routing fallback (straight line).
      </div>
    </div>
    """
    m.get_root().html.add_child(folium.Element(controls_html))

    # --- Animation JS --------------------------------------------------------
    payload = {
        "vehicles": _vehicles_to_json(vehicles),
        "trips": _build_trips(vehicles),
        "sim_duration": sim_duration,
        "sim_start_iso": (global_start.isoformat() if pd.notna(global_start) else None),
        "base_speed_mph": speed_mph,
    }
    payload_json = json.dumps(payload)
    map_var = m.get_name()

    animation_js = f"""
    <script>
    (function () {{
      function start() {{
        if (typeof {map_var} === 'undefined') {{
          // Map not ready yet - retry shortly.
          return setTimeout(start, 50);
        }}
        const map = {map_var};
        const DATA = {payload_json};

        document.getElementById('base-speed').textContent =
          DATA.base_speed_mph.toFixed(1);

        // ---- Icon builders ---------------------------------------------
        function carIconHtml(color) {{
          // Top-down car SVG, body tinted to the vehicle color.
          return (
            '<svg xmlns="http://www.w3.org/2000/svg" width="34" height="18" ' +
            'viewBox="0 0 34 18" style="overflow:visible;">' +
              '<rect x="1" y="1" width="32" height="16" rx="3.5" ry="3.5" ' +
                'fill="' + color + '" stroke="#111" stroke-width="1"/>' +
              // Windshield (front, left side) + rear window (right side)
              '<rect x="4" y="3" width="7" height="12" rx="1.2" ry="1.2" ' +
                'fill="rgba(200,230,255,0.9)" stroke="#111" stroke-width="0.6"/>' +
              '<rect x="23" y="3" width="7" height="12" rx="1.2" ry="1.2" ' +
                'fill="rgba(200,230,255,0.9)" stroke="#111" stroke-width="0.6"/>' +
              // Headlights on front bumper (left edge)
              '<rect x="0.2" y="2.5" width="1.8" height="2.5" fill="#ffe066" stroke="#111" stroke-width="0.3"/>' +
              '<rect x="0.2" y="13" width="1.8" height="2.5" fill="#ffe066" stroke="#111" stroke-width="0.3"/>' +
              // Tail lights
              '<rect x="32" y="2.5" width="1.8" height="2.5" fill="#c0392b" stroke="#111" stroke-width="0.3"/>' +
              '<rect x="32" y="13" width="1.8" height="2.5" fill="#c0392b" stroke="#111" stroke-width="0.3"/>' +
            '</svg>'
          );
        }}

        function stickmanIconHtml(color) {{
          return (
            '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="24" ' +
            'viewBox="0 0 14 24" style="overflow:visible;">' +
              '<circle cx="7" cy="4" r="3.2" fill="' + color + '" stroke="#111" stroke-width="0.9"/>' +
              '<line x1="7"  y1="7.2" x2="7"  y2="15" stroke="' + color + '" stroke-width="2.2" stroke-linecap="round"/>' +
              '<line x1="7"  y1="9"   x2="2"  y2="13" stroke="' + color + '" stroke-width="1.9" stroke-linecap="round"/>' +
              '<line x1="7"  y1="9"   x2="12" y2="13" stroke="' + color + '" stroke-width="1.9" stroke-linecap="round"/>' +
              '<line x1="7"  y1="15"  x2="2"  y2="22" stroke="' + color + '" stroke-width="1.9" stroke-linecap="round"/>' +
              '<line x1="7"  y1="15"  x2="12" y2="22" stroke="' + color + '" stroke-width="1.9" stroke-linecap="round"/>' +
            '</svg>'
          );
        }}

        const STICK_GREY  = '#6b7280';
        const STICK_GREEN = '#16a34a';

        function makeStickIcon(state, idx) {{
          // state: 'waiting' | 'onboard' | 'dropped'
          const color = state === 'dropped' ? STICK_GREEN : STICK_GREY;
          // When onboard, anchor the stickman so it renders ABOVE the car
          // (iconSize=[14,24], so setting anchorY beyond 24 pushes the icon up).
          // Stagger multiple passengers horizontally by their onboard index.
          let anchorX, anchorY;
          if (state === 'onboard') {{
            anchorX = 7 - (idx * 10);   // 0 -> centered, 1 -> shifted right, etc.
            anchorY = 34;               // 10px above car center
          }} else if (state === 'dropped') {{
            anchorX = -6;               // just right of the dropoff marker
            anchorY = 24;
          }} else {{ // waiting
            anchorX = -6;               // just right of the pickup marker
            anchorY = 24;
          }}
          return L.divIcon({{
            className: 'stick-marker',
            html: stickmanIconHtml(color),
            iconSize: [14, 24],
            iconAnchor: [anchorX, anchorY],
          }});
        }}

        // ---- Create per-vehicle moving marker + trail polyline ----------
        const markers = {{}};
        const trails  = {{}};
        DATA.vehicles.forEach(function (v) {{
          const icon = L.divIcon({{
            className: 'veh-marker',
            html: carIconHtml(v.color),
            iconSize: [34, 18],
            iconAnchor: [17, 9],
          }});
          const mk = L.marker(v.spawn, {{icon: icon, zIndexOffset: 1000}});
          mk.bindPopup('Vehicle ' + v.id);
          markers[v.id] = mk;
          trails[v.id]  = L.polyline([], {{
            color: v.color, weight: 5, opacity: 0.85,
          }});
        }});

        // ---- Create per-passenger stickman marker -----------------------
        // Each entry: {{marker, data, lastState, lastIdx}}
        const passengers = {{}};
        function paxKey(vehicleId, tripId) {{ return vehicleId + ':' + tripId; }}
        DATA.trips.forEach(function (t) {{
          const startPos = t.pickup_pos || t.dropoff_pos;
          if (!startPos) return;
          const mk = L.marker(startPos, {{
            icon: makeStickIcon('waiting', 0),
            zIndexOffset: 1500,
          }});
          mk.bindPopup('Trip ' + t.trip_id + '<br>Vehicle ' + t.vehicle_id);
          mk.addTo(map);
          passengers[paxKey(t.vehicle_id, t.trip_id)] = {{
            marker: mk, data: t, lastState: 'waiting', lastIdx: -1,
          }};
        }});

        // ---- Interpolate a vehicle state at a given simulation time t ----
        function stateAt(v, t) {{
          if (t < v.activation_time) {{
            return {{visible: false, onboard: []}};
          }}
          // Walk segments, updating onboard passengers as events fire.
          const onboard = [v.first_trip_id];
          let pos = v.spawn.slice();
          let trail = [pos.slice()];

          for (let i = 0; i < v.segments.length; i++) {{
            const seg = v.segments[i];
            if (t < seg.start_t) {{
              // Idle before this segment - stay at current pos.
              break;
            }}
            if (t >= seg.end_t) {{
              // Entire segment completed: traverse fully & apply event.
              trail = trail.concat(seg.coords.slice(1));
              pos = seg.coords[seg.coords.length - 1].slice();
              if (seg.event_kind === 'pickup') {{
                onboard.push(seg.event_trip_id);
              }} else {{
                const idx = onboard.indexOf(seg.event_trip_id);
                if (idx >= 0) onboard.splice(idx, 1);
              }}
              continue;
            }}
            // Mid-segment - interpolate by distance along the polyline.
            const segDur = Math.max(seg.end_t - seg.start_t, 1e-9);
            const frac = (t - seg.start_t) / segDur;
            const target = frac * seg.total_distance_m;
            const cum = seg.cum_dist_m;
            let lo = 0, hi = cum.length - 1;
            while (lo < hi) {{
              const mid = (lo + hi) >> 1;
              if (cum[mid] < target) lo = mid + 1; else hi = mid;
            }}
            const idx = lo;
            let here;
            if (idx === 0) {{
              here = seg.coords[0].slice();
            }} else {{
              const d0 = cum[idx - 1], d1 = cum[idx];
              const a = (target - d0) / Math.max(d1 - d0, 1e-9);
              const p0 = seg.coords[idx - 1], p1 = seg.coords[idx];
              here = [p0[0] + (p1[0] - p0[0]) * a,
                      p0[1] + (p1[1] - p0[1]) * a];
            }}
            trail = trail.concat(seg.coords.slice(1, idx + 1));
            trail.push(here);
            pos = here;
            return {{visible: true, pos: pos, onboard: onboard, trail: trail}};
          }}
          return {{visible: true, pos: pos, onboard: onboard, trail: trail}};
        }}

        // ---- Animation state --------------------------------------------
        const simDuration = Math.max(DATA.sim_duration, 1);
        let simT = 0;
        let speedMult = 1.0;
        let paused = false;
        let lastFrame = performance.now();
        const simStart = DATA.sim_start_iso ? new Date(DATA.sim_start_iso) : null;

        const speedVal  = document.getElementById('speed-val');
        const timeVal   = document.getElementById('time-val');
        const clockVal  = document.getElementById('clock-val');
        const slider    = document.getElementById('time-slider');
        const btnPlay   = document.getElementById('btn-play');
        const btnFast   = document.getElementById('btn-fast');
        const btnSlow   = document.getElementById('btn-slow');
        const btnReset  = document.getElementById('btn-reset');

        function fmt(sec) {{
          sec = Math.max(0, Math.floor(sec));
          const h = Math.floor(sec / 3600);
          const m = Math.floor((sec % 3600) / 60);
          const s = sec % 60;
          return String(h).padStart(2,'0') + ':' +
                 String(m).padStart(2,'0') + ':' +
                 String(s).padStart(2,'0');
        }}

        function updateUI() {{
          speedVal.textContent = speedMult.toFixed(2) + 'x';
          timeVal.textContent  = fmt(simT);
          if (simStart) {{
            const d = new Date(simStart.getTime() + simT * 1000);
            clockVal.textContent = d.toISOString().substring(11, 19);
          }} else {{
            clockVal.textContent = '--:--:--';
          }}
          slider.value = String(Math.round(1000 * simT / simDuration));
        }}

        function render() {{
          // 1. Vehicles: compute state and update car marker + trail.
          const vehicleStates = {{}};
          DATA.vehicles.forEach(function (v) {{
            const st = stateAt(v, simT);
            vehicleStates[v.id] = st;
            const mk = markers[v.id];
            const tr = trails[v.id];
            if (!st.visible) {{
              if (map.hasLayer(mk)) map.removeLayer(mk);
              if (map.hasLayer(tr)) map.removeLayer(tr);
              return;
            }}
            if (!map.hasLayer(mk)) mk.addTo(map);
            if (!map.hasLayer(tr)) tr.addTo(map);
            mk.setLatLng(st.pos);
            tr.setLatLngs(st.trail);
            const names = st.onboard.length
              ? st.onboard.join(', ') : '(empty)';
            mk.setPopupContent(
              '<b>Vehicle ' + v.id + '</b><br>' +
              'Onboard trips: ' + names + '<br>' +
              'Count: ' + st.onboard.length
            );
          }});

          // 2. Passengers: grey waiting -> onboard on car -> green at dropoff.
          Object.keys(passengers).forEach(function (tripId) {{
            const p = passengers[tripId];
            const t = p.data;
            let state, pos, idx = 0;

            if (t.dropoff_t != null && simT >= t.dropoff_t && t.dropoff_pos) {{
              state = 'dropped';
              pos = t.dropoff_pos;
            }} else if (t.pickup_t != null && simT >= t.pickup_t) {{
              const vst = vehicleStates[t.vehicle_id];
              if (vst && vst.visible) {{
                state = 'onboard';
                pos = vst.pos;
                const i = vst.onboard.indexOf(t.trip_id);
                idx = i < 0 ? 0 : i;
              }} else {{
                state = 'waiting';
                pos = t.pickup_pos || t.dropoff_pos;
              }}
            }} else {{
              state = 'waiting';
              pos = t.pickup_pos || t.dropoff_pos;
            }}

            if (state !== p.lastState || (state === 'onboard' && idx !== p.lastIdx)) {{
              p.marker.setIcon(makeStickIcon(state, idx));
              p.lastState = state;
              p.lastIdx = idx;
            }}
            if (pos) p.marker.setLatLng(pos);
          }});
        }}

        function loop(now) {{
          const dtReal = (now - lastFrame) / 1000;
          lastFrame = now;
          if (!paused) {{
            simT += dtReal * speedMult;
            if (simT >= simDuration) simT = simDuration;
          }}
          updateUI();
          render();
          requestAnimationFrame(loop);
        }}

        // ---- Wire up controls -------------------------------------------
        btnFast.addEventListener('click', function () {{
          speedMult = Math.min(speedMult * 1.5, 64);
        }});
        btnSlow.addEventListener('click', function () {{
          speedMult = Math.max(speedMult / 1.5, 0.0625);
        }});
        btnPlay.addEventListener('click', function () {{
          paused = !paused;
          btnPlay.textContent = paused ? 'Play' : 'Pause';
        }});
        btnReset.addEventListener('click', function () {{
          simT = 0;
          paused = false;
          btnPlay.textContent = 'Pause';
        }});
        slider.addEventListener('input', function () {{
          simT = (parseInt(slider.value, 10) / 1000) * simDuration;
        }});

        // Kick off animation.
        lastFrame = performance.now();
        requestAnimationFrame(loop);
      }}

      if (document.readyState === 'loading') {{
        document.addEventListener('DOMContentLoaded', start);
      }} else {{
        start();
      }}
    }})();
    </script>
    """
    m.get_root().html.add_child(folium.Element(animation_js))

    m.save(output_path)
    log.info("Wrote animated map: %s", output_path)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    log.info("INPUT_FILE=%s  VEHICLE_SPEED_MPH=%s  BACKEND=%s  OSRM=%s",
             INPUT_FILE, VEHICLE_SPEED_MPH, ROUTING_BACKEND, OSRM_BASE_URL)

    if not Path(INPUT_FILE).exists():
        log.error("Input file not found: %s", INPUT_FILE)
        return 1

    df = load_data(INPUT_FILE)
    vehicles = initialize_vehicles(df)
    if not vehicles:
        log.error("No vehicles found in input file.")
        return 1

    waypoints: List[Tuple[float, float]] = [
        (a.lon, a.lat) for v in vehicles for a in v.actions
    ]
    route_mgr = RouteManager(waypoints=waypoints)
    global_start = simulate_vehicle_movements(vehicles, route_mgr, VEHICLE_SPEED_MPH)
    export_validated_plan(df, vehicles, global_start, OUTPUT_PLAN_FILE)
    render_map(vehicles, global_start, VEHICLE_SPEED_MPH, OUTPUT_HTML)

    log.info("Done. Open %s in your browser.", OUTPUT_HTML)
    return 0


if __name__ == "__main__":
    sys.exit(main())
