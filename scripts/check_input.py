#!/usr/bin/env python3
"""Read-only OMOSIM/region validation before a daily DRT experiment."""
from __future__ import annotations
import argparse
from collections import Counter
import json
import math
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def numeric(value, label, minimum=0.0, maximum=None):
    if isinstance(value, bool):
        raise ValueError(f"{label}: expected a number")
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label}: missing or invalid number") from None
    if not math.isfinite(result) or result < minimum or (maximum is not None and result > maximum):
        raise ValueError(f"{label}: outside the valid range")
    return result


def integer(value, label):
    if isinstance(value, bool) or not re.fullmatch(r"-?\d+", str(value)):
        raise ValueError(f"{label}: expected an integer or integer string")
    return int(value)


def validate_input(path, threshold=0.6, service_start=6, service_end=22):
    from drt_pipeline.constants import SIX_REGIONS, RegionId
    from drt_pipeline.regions import map_to_region, region_center
    from drt_pipeline.stage0 import stage0_identify_candidates, build_stage0_requests
    from drt_pipeline.travel import make_point_key
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or not isinstance(data.get("agents"), list):
        raise ValueError("Input must contain an agents array")
    seen, days, retained = set(), set(), []
    excluded = 0
    for agent in data["agents"]:
        aid = integer(agent.get("id"), "agent.id")
        if aid in seen:
            raise ValueError(f"Duplicate agent ID: {aid}")
        seen.add(aid)
        demands = agent.get("mobilityDemand")
        if not isinstance(demands, list):
            raise ValueError(f"agent {aid}: mobilityDemand must be an array")
        if any(e.get("type") == "Trip" and str(e.get("mode", "")).upper() == "CAR_PASSENGER"
               for d in demands for e in d.get("plan", [])):
            excluded += 1
            continue
        numeric(agent.get("drtLike"), f"agent {aid}.drtLike", maximum=1.0)
        if not isinstance(agent.get("mobilityGroup"), str) or not agent["mobilityGroup"]:
            raise ValueError(f"agent {aid}: mobilityGroup is required")
        own_days = set()
        for demand in demands:
            day = integer(demand.get("day"), "day")
            if day in own_days:
                raise ValueError(f"agent {aid}: multiple diaries for day {day}")
            own_days.add(day);days.add(day)
            plan = demand.get("plan")
            if not isinstance(plan, list):
                raise ValueError(f"agent {aid}: plan must be an array")
            legs = set()
            for idx, item in enumerate(plan):
                if item.get("type") == "Activity":
                    if not isinstance(item.get("activityType"), str):
                        raise ValueError(f"agent {aid}: missing activityType")
                    numeric(item.get("lat"), "activity.lat", -90, 90)
                    numeric(item.get("lon"), "activity.lon", -180, 180)
                    continue
                if item.get("type") != "Trip":
                    raise ValueError(f"agent {aid}: unknown plan entry type")
                leg = integer(item.get("legID"), "legID")
                if leg in legs:
                    raise ValueError(f"agent {aid}: duplicate legID in day {day}")
                legs.add(leg)
                if item.get("mode") not in {"CAR_DRIVER", "CAR_PASSENGER", "PUBLIC_TRANSIT", "FOOT", "BICYCLE", "WALKING", "BIKING"}:
                    raise ValueError(f"agent {aid}: unsupported/pre-existing DRT selected mode")
                numeric(item.get("timeMinute"), "selected timeMinute")
                numeric(item.get("distanceKilometer"), "selected distanceKilometer")
                if not re.fullmatch(r"(?:[01]?\d|2[0-3]):[0-5]\d", str(item.get("startTime", ""))):
                    raise ValueError(f"agent {aid}: startTime must be HH:MM in one 24-hour day")
                if idx == 0 or idx == len(plan)-1 or plan[idx-1].get("type") != "Activity" or plan[idx+1].get("type") != "Activity":
                    raise ValueError(f"agent {aid}: each Trip must be between two Activities")
                alternatives = item.get("alternativeModes")
                if not isinstance(alternatives, list):
                    raise ValueError(f"agent {aid}: alternativeModes must be an array")
                alt_modes = [a.get("mode") for a in alternatives]
                if len(alt_modes) != len(set(alt_modes)):
                    raise ValueError(f"agent {aid}: duplicate alternative mode")
                if item["mode"] not in alt_modes:
                    raise ValueError(f"agent {aid}: selected mode needs an alternativeModes entry")
                for alt in alternatives:
                    if alt.get("mode") == "DRT":
                        continue
                    # OMOSIM uses negative time for unavailable alternatives.
                    t = numeric(alt.get("timeMinute"), "alternative timeMinute", -1)
                    if t >= 0:
                        numeric(alt.get("distanceKilometer"), "alternative distanceKilometer")
        retained.append(agent)
    if len(days) > 1:
        raise ValueError("Run one simulated day at a time; this solver groups requests by hour, not by day")
    if not retained:
        raise ValueError("No agents remain after CAR_PASSENGER exclusion")
    filtered = {**data, "agents": retained}
    candidates, stats = stage0_identify_candidates(filtered, drt_like_threshold=threshold)
    requests, built = build_stage0_requests(filtered, candidates)
    if built["bad_plan_index"] or built["bad_activity_chain"] or built["pt_missing"]:
        raise ValueError(f"Invalid candidate requests: {built}")
    for request in requests:
        numeric(request.tt_pt_min, "candidate public-transit time")
    inside = [r for r in requests if r.o_region != RegionId.OUTSIDE and r.d_region != RegionId.OUTSIDE]
    in_service = [r for r in inside if service_start <= r.hour <= service_end]
    points = {make_point_key(*region_center(r)) for r in SIX_REGIONS}
    for request in requests:
        points.add(make_point_key(request.pickup_lat, request.pickup_lon))
        points.add(make_point_key(request.drop_lat, request.drop_lon))
    result = {
        "total_agents": len(seen), "excluded_car_passenger_agents": excluded,
        "analysis_agents": len(retained), "simulated_days": sorted(days),
        "candidates": len(candidates), "requests_built": len(requests),
        "requests_inside_service_area": len(inside),
        "requests_outside_service_area": len(requests)-len(inside),
        "requests_inside_service_hours": len(in_service),
        "origin_region_counts": dict(Counter(r.o_region.value for r in requests)),
        "stage0_filters": stats, "unique_cache_points_including_centers": len(points),
        "directed_cache_pairs_including_diagonal": len(points)**2,
    }
    if not in_service:
        raise ValueError("No eligible requests inside the configured area/service hours. Diagnostics: " + json.dumps(result))
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", type=Path, required=True)
    p.add_argument("--service-area", type=Path, required=True)
    p.add_argument("--threshold", type=float, default=0.6)
    p.add_argument("--service-start", type=int, default=6)
    p.add_argument("--service-end", type=int, default=22)
    a = p.parse_args()
    os.environ["DRT_SERVICE_AREA_CONFIG"] = str(a.service_area.resolve())
    print(json.dumps(validate_input(a.input, a.threshold, a.service_start, a.service_end), indent=2))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, TypeError) as exc:
        sys.exit(f"Input validation failed: {exc}")
