"""Stage0 implementation: extract DRT candidate trips/requests and write cache inputs.

Policy notes reflected in this module:
- Stage0 does not exclude candidates by region/activity category.
- Candidate filter:
  (mode not in {bicycle/foot/walking/biking}) AND
  (CAR_DRIVER.distanceKilometer < 15) AND
  (FOOT.distanceKilometer > 2) AND
  (agent.drtLike >= drt_like_threshold)
- When drt_like_threshold is set, agent-level drtLike field is used.
  If drt_like_sub_ratio < 1.0, agents whose drtLike equals exactly the
  threshold value are sub-sampled deterministically via stable hashing.
- Fallback: if drt_like_threshold is None, the legacy hash-based
  like_threshold filter is used (backward compatible).
- latest = call_time + PT time (PUBLIC_TRANSIT.timeMinute)
- Every Trip has DRT altmode; Stage0 resets DRT alt each iteration to
  timeMinute=-1, distanceKilometer=None, selected=False.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .constants import RegionId
from .regions import map_to_region


@dataclass(frozen=True)
class TripKey:
    agent_id: int
    day: int
    plan_index: int
    trip_leg_id: int


@dataclass
class Stage0Request:
    req_id: int
    trip_key: TripKey

    call_time_min: int
    pickup_lat: float
    pickup_lon: float
    drop_lat: float
    drop_lon: float

    o_region: RegionId
    d_region: RegionId
    hour: int

    tt_pt_min: float
    latest_min: float


def summarize_trip_tour_units(output: Dict[str, Any]) -> Dict[str, int]:
    """Return unit-explicit trip/tour counts from an output JSON object.

    Definitions:
    - trip: a plan item with type == "Trip"
    - tour: one (agent, day) mobilityDemand entry that contains >=1 Trip
    """
    agents_agent_cnt = 0
    mobility_demands_day_cnt = 0
    trips_trip_cnt = 0
    tours_tour_cnt = 0

    for agent in output.get("agents", []):
        agents_agent_cnt += 1
        for day_plan in agent.get("mobilityDemand", []):
            mobility_demands_day_cnt += 1
            day_trip_cnt = 0
            for item in day_plan.get("plan", []):
                if item.get("type") == "Trip":
                    day_trip_cnt += 1
            trips_trip_cnt += day_trip_cnt
            if day_trip_cnt > 0:
                tours_tour_cnt += 1

    return {
        "agents_agent_cnt": agents_agent_cnt,
        "mobility_demands_day_cnt": mobility_demands_day_cnt,
        "trips_trip_cnt": trips_trip_cnt,
        "tours_tour_cnt": tours_tour_cnt,
    }


def hhmm_to_min(hhmm: str) -> int:
    hh, mm = hhmm.split(":")
    return 60 * int(hh) + int(mm)


def _get_alt(trip: Dict[str, Any], mode: str) -> Optional[Dict[str, Any]]:
    for alt in trip.get("alternativeModes", []):
        if alt.get("mode") == mode:
            return alt
    return None


def _stable_drt_likelihood(seed: int, key: TripKey) -> int:
    key_str = f"{seed}|{key.agent_id}|{key.day}|{key.plan_index}|{key.trip_leg_id}"
    digest = hashlib.md5(key_str.encode("utf-8")).hexdigest()
    return (int(digest, 16) % 5) + 1


def ensure_drt_altmode_for_all_trips(output: Dict[str, Any]) -> int:
    """Ensure every Trip has DRT alt and reset it to unserved defaults.

    Returns:
        Number of trips where DRT alt entry was newly added.
    """
    added = 0
    for agent in output.get("agents", []):
        for day_plan in agent.get("mobilityDemand", []):
            for item in day_plan.get("plan", []):
                if item.get("type") != "Trip":
                    continue
                trip = item
                drt_alt = None
                for alt in trip.get("alternativeModes", []):
                    if alt.get("mode") == "DRT":
                        drt_alt = alt
                        break
                if drt_alt is None:
                    trip.setdefault("alternativeModes", []).append(
                        {
                            "mode": "DRT",
                            "distanceKilometer": None,
                            "timeMinute": -1,
                            "selected": False,
                        }
                    )
                    added += 1
                else:
                    drt_alt["timeMinute"] = -1
                    drt_alt["distanceKilometer"] = None
                    drt_alt["selected"] = False
    return added


def stage0_identify_candidates(
    output: Dict[str, Any],
    seed: int = 42,
    like_threshold: int = 3,
    drt_like_threshold: Optional[float] = None,
    drt_like_sub_ratio: float = 1.0,
) -> Tuple[List[TripKey], Dict[str, int]]:
    """Extract Stage0 candidate TripKeys.

    Two filtering modes:
    1. drt_like_threshold is set (float): uses agent-level drtLike field.
       Agents with drtLike >= drt_like_threshold are included.
       If drt_like_sub_ratio < 1.0, agents whose drtLike equals exactly
       drt_like_threshold are sub-sampled: a deterministic hash decides
       inclusion (fraction ~ drt_like_sub_ratio).
    2. drt_like_threshold is None (default): legacy hash-based filter
       using like_threshold (int, backward compatible).
    """
    candidates: List[TripKey] = []
    stats = {
        "trips_total": 0,
        "trips_with_alt": 0,
        "missing_car_alt": 0,
        "missing_walk_alt": 0,
        "missing_pt_alt": 0,
        "invalid_leg_id": 0,
        "excluded_mode": 0,
        "excluded_dist": 0,
        "excluded_like": 0,
        "selected": 0,
    }

    use_drt_like = drt_like_threshold is not None

    # Pre-build agent drtLike map if using drtLike mode
    agent_drt_like: Dict[int, float] = {}
    if use_drt_like:
        for agent in output.get("agents", []):
            aid = int(agent.get("id"))
            agent_drt_like[aid] = float(agent.get("drtLike", 0))

    for agent in output.get("agents", []):
        agent_id = int(agent.get("id"))
        for day_plan in agent.get("mobilityDemand", []):
            day = int(day_plan.get("day"))
            plan = day_plan.get("plan", [])
            for plan_index, item in enumerate(plan):
                if item.get("type") != "Trip":
                    continue
                stats["trips_total"] += 1
                trip = item

                try:
                    trip_leg_id = int(trip.get("legID"))
                except (TypeError, ValueError):
                    stats["invalid_leg_id"] += 1
                    continue

                mode_cur = str(trip.get("mode", "")).upper()
                if mode_cur in {"BICYCLE", "FOOT", "WALKING", "BIKING", "CAR_PASSENGER"}:
                    stats["excluded_mode"] += 1
                    continue

                alt_car = _get_alt(trip, "CAR_DRIVER")
                if alt_car is None:
                    stats["missing_car_alt"] += 1
                    continue
                alt_walk = _get_alt(trip, "FOOT")
                if alt_walk is None:
                    stats["missing_walk_alt"] += 1
                    continue
                alt_pt = _get_alt(trip, "PUBLIC_TRANSIT")
                if alt_pt is None:
                    stats["missing_pt_alt"] += 1
                    continue
                stats["trips_with_alt"] += 1

                try:
                    dist_car = float(alt_car.get("distanceKilometer"))
                    dist_walk = float(alt_walk.get("distanceKilometer"))
                except (TypeError, ValueError):
                    stats["excluded_dist"] += 1
                    continue

                if not (dist_car < 15.0 and dist_walk > 2.0):
                    stats["excluded_dist"] += 1
                    continue

                trip_key = TripKey(
                    agent_id=agent_id,
                    day=day,
                    plan_index=plan_index,
                    trip_leg_id=trip_leg_id,
                )

                # ── Likelihood filter ──
                if use_drt_like:
                    dl = agent_drt_like.get(agent_id, 0)
                    if dl < drt_like_threshold:
                        stats["excluded_like"] += 1
                        continue
                    # Sub-sample agents at exactly the boundary value
                    if drt_like_sub_ratio < 1.0 and abs(dl - drt_like_threshold) < 1e-9:
                        h = _stable_drt_likelihood(seed=seed, key=trip_key)
                        if h > int(drt_like_sub_ratio * 5) + 1:
                            stats["excluded_like"] += 1
                            continue
                else:
                    like = _stable_drt_likelihood(seed=seed, key=trip_key)
                    if like <= like_threshold:
                        stats["excluded_like"] += 1
                        continue

                candidates.append(trip_key)
                stats["selected"] += 1

    return candidates, stats


def build_stage0_requests(output: Dict[str, Any], candidates: List[TripKey]) -> Tuple[List[Stage0Request], Dict[str, int]]:
    """Build simulation requests from candidate TripKeys."""

    agents_by_id = {int(a["id"]): a for a in output.get("agents", [])}

    reqs: List[Stage0Request] = []
    stats = {
        "built": 0,
        "bad_plan_index": 0,
        "bad_activity_chain": 0,
        "pt_missing": 0,
    }

    next_req_id = 0
    for key in candidates:
        agent = agents_by_id.get(key.agent_id)
        if agent is None:
            stats["bad_plan_index"] += 1
            continue

        day_plan = None
        for md in agent.get("mobilityDemand", []):
            if int(md.get("day")) == key.day:
                day_plan = md
                break
        if day_plan is None:
            stats["bad_plan_index"] += 1
            continue

        plan = day_plan.get("plan", [])
        if key.plan_index <= 0 or key.plan_index >= len(plan) - 1:
            stats["bad_plan_index"] += 1
            continue
        trip = plan[key.plan_index]
        prev_act = plan[key.plan_index - 1]
        next_act = plan[key.plan_index + 1]
        if prev_act.get("type") != "Activity" or next_act.get("type") != "Activity":
            stats["bad_activity_chain"] += 1
            continue

        if int(trip.get("legID")) != key.trip_leg_id:
            stats["bad_plan_index"] += 1
            continue

        call_time_min = hhmm_to_min(str(trip.get("startTime")))
        pickup_lat, pickup_lon = float(prev_act.get("lat")), float(prev_act.get("lon"))
        drop_lat, drop_lon = float(next_act.get("lat")), float(next_act.get("lon"))

        o_region = map_to_region(pickup_lat, pickup_lon)
        d_region = map_to_region(drop_lat, drop_lon)
        hour = int(call_time_min // 60)

        alt_pt = _get_alt(trip, "PUBLIC_TRANSIT")
        if alt_pt is None:
            stats["pt_missing"] += 1
            continue
        tt_pt = float(alt_pt.get("timeMinute"))
        latest = call_time_min + tt_pt

        reqs.append(
            Stage0Request(
                req_id=next_req_id,
                trip_key=key,
                call_time_min=call_time_min,
                pickup_lat=pickup_lat,
                pickup_lon=pickup_lon,
                drop_lat=drop_lat,
                drop_lon=drop_lon,
                o_region=o_region,
                d_region=d_region,
                hour=hour,
                tt_pt_min=tt_pt,
                latest_min=latest,
            )
        )
        next_req_id += 1
        stats["built"] += 1

    return reqs, stats
