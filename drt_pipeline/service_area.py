"""Release-only geography adapter. Configure before importing the solver.

The solver retains its six stable region IDs. The JSON supplies their local
meaning, boundaries and positions without changing ALNS or choice rules.
"""
from __future__ import annotations

from functools import lru_cache
import json
import math
import os
from pathlib import Path

from .constants import SIX_REGIONS

ENV_NAME = "DRT_SERVICE_AREA_CONFIG"
REGION_KEYS = tuple(region.value for region in SIX_REGIONS)


def finite(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{label} must be a finite number")
    return float(value)


class ServiceArea:
    def __init__(self, data):
        if not isinstance(data, dict) or data.get("schema_version") != 1:
            raise ValueError("service area must have schema_version: 1")
        if data.get("coordinate_system") != "EPSG:4326":
            raise ValueError("Coordinates must use EPSG:4326; all arrays are [latitude, longitude]")
        if not isinstance(data.get("name"), str) or not data["name"].strip():
            raise ValueError("Provide a service-area name")
        centers = data.get("centers", {})
        if not isinstance(centers, dict) or set(centers) != set(REGION_KEYS):
            raise ValueError(f"centers must contain exactly these six stable IDs: {REGION_KEYS}")
        self.centers = {}
        for key, point in centers.items():
            if not isinstance(point, list) or len(point) != 2:
                raise ValueError(f"{key}: center must be [latitude, longitude]")
            lat, lon = (finite(v, f"{key} coordinate") for v in point)
            if not (-90 < lat < 90 and -180 <= lon <= 180):
                raise ValueError(f"{key}: coordinates outside latitude/longitude range")
            self.centers[key] = (lat, lon)
        if len(set(self.centers.values())) != 6:
            raise ValueError("All six region centers must be distinct")
        self.metric = data.get("assignment_metric", "local_km_l1")
        if self.metric not in ("local_km_l1", "legacy_degree_l1"):
            raise ValueError("assignment_metric must be local_km_l1 or legacy_degree_l1")
        self.mean_latitude = finite(data.get("mean_latitude", sum(p[0] for p in self.centers.values()) / 6), "mean_latitude")
        if not -90 < self.mean_latitude < 90:
            raise ValueError("mean_latitude must be between -90 and 90")
        self.groups = []
        groups = data.get("groups")
        if not isinstance(groups, list) or not groups:
            raise ValueError("Provide at least one ordered rectangular group")
        used = []
        for item in groups:
            if not isinstance(item, dict):
                raise ValueError("Each group must be an object")
            bounds = item.get("bounds")
            regions = item.get("regions")
            if not isinstance(bounds, list) or len(bounds) != 4:
                raise ValueError("bounds must be [lat_min, lon_min, lat_max, lon_max]")
            lo_lat, lo_lon, hi_lat, hi_lon = (finite(v, "bound") for v in bounds)
            if not (-90 < lo_lat < hi_lat < 90 and -180 <= lo_lon < hi_lon <= 180):
                raise ValueError("Invalid rectangle bounds; antimeridian rectangles are unsupported")
            if not isinstance(regions, list) or not regions or any(r not in self.centers for r in regions):
                raise ValueError("Each group needs non-empty, recognised region IDs")
            for key in regions:
                lat, lon = self.centers[key]
                if not (lo_lat <= lat <= hi_lat and lo_lon <= lon <= hi_lon):
                    raise ValueError(f"Center {key} must lie inside its group rectangle")
            used.extend(regions)
            self.groups.append(((lo_lat, lo_lon, hi_lat, hi_lon), tuple(regions)))
        if sorted(used) != sorted(REGION_KEYS):
            raise ValueError("Each stable region ID must occur in exactly one group")
        self.data = data
        for key, point in self.centers.items():
            if self.map(*point) != key:
                raise ValueError(f"Center {key} is shadowed by a higher-priority group; adjust overlap")

    def map(self, lat, lon):
        lat, lon = finite(lat, "latitude"), finite(lon, "longitude")
        for bounds, keys in self.groups:
            lo_lat, lo_lon, hi_lat, hi_lon = bounds
            if lo_lat <= lat <= hi_lat and lo_lon <= lon <= hi_lon:
                factor = math.cos(math.radians(self.mean_latitude)) if self.metric == "local_km_l1" else 1.0
                # min preserves declared region order for exact ties.
                return min(keys, key=lambda k: abs(lat - self.centers[k][0]) + factor * abs(lon - self.centers[k][1]))
        return "OUTSIDE"


@lru_cache(maxsize=1)
def configured_area():
    value = os.environ.get(ENV_NAME)
    if not value:
        return None
    with Path(value).open(encoding="utf-8") as handle:
        return ServiceArea(json.load(handle))
