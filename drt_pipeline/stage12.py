"""Stage1/Stage2 mode update logic for iterative DRT pipeline."""

from __future__ import annotations

import copy
import csv
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from .choice_rules import (
    DEFAULT_CAR_TIME_RATIO_CAP,
    evaluate_trip_drt_choice,
)

TripKey = Tuple[int, int, int, int]

CAR_SET = {"CAR_DRIVER", "CAR_PASSENGER"}
CARFREE_FALLBACK: Tuple[str, ...] = ("PUBLIC_TRANSIT", "FOOT")

MODE_PRIORITY: Tuple[str, ...] = (
    "CAR_PASSENGER",
    "PUBLIC_TRANSIT",
    "FOOT",
    "DRT",
    "CAR_DRIVER",
    "BICYCLE",
    "UNDEFINED",
)

@dataclass
class _TripRef:
    agent_id: int
    day: int
    plan_index: int
    trip_leg_id: int
    trip: Dict[str, Any]
    in_scope: bool = False

    @property
    def key(self) -> TripKey:
        return (self.agent_id, self.day, self.plan_index, self.trip_leg_id)


def parse_stage0_keys(stage0_requests_csv_path: str) -> Set[TripKey]:
    keys: Set[TripKey] = set()
    with open(stage0_requests_csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                key: TripKey = (
                    int(row["agent_id"]),
                    int(row["day"]),
                    int(row["plan_index"]),
                    int(row["trip_leg_id"]),
                )
            except (KeyError, TypeError, ValueError):
                continue
            keys.add(key)
    return keys


def get_alt(trip: Dict[str, Any], mode: str) -> Optional[Dict[str, Any]]:
    for alt in trip.get("alternativeModes", []):
        if alt.get("mode") == mode:
            return alt
    return None


def _safe_float(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _norm_mode(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s if s != "" else None


def _drt_rule_satisfied(
    trip: Dict[str, Any],
    original_mode: Optional[str] = None,
    mobility_group: Optional[str] = None,
    car_time_ratio_cap: float = DEFAULT_CAR_TIME_RATIO_CAP,
) -> bool:
    return evaluate_trip_drt_choice(
        trip,
        original_mode=original_mode,
        mobility_group=mobility_group,
        car_time_ratio_cap=car_time_ratio_cap,
    ).eligible


def _mode_priority_index(mode: str) -> int:
    try:
        return MODE_PRIORITY.index(mode)
    except ValueError:
        return len(MODE_PRIORITY)


def choose_fallback_mode(trip: Dict[str, Any], mode_set: Iterable[str], criteria: str = "time") -> Optional[str]:
    modes = list(mode_set)
    candidates: List[Tuple[float, int, str]] = []
    fallback_modes: List[Tuple[int, str]] = []
    for mode in modes:
        alt = get_alt(trip, mode)
        if alt is None:
            continue
        pidx = _mode_priority_index(mode)
        fallback_modes.append((pidx, mode))
        
        if criteria == "time":
            val = _safe_float(alt.get("timeMinute"))
            if val is not None:
                candidates.append((-val, -pidx, mode))
        else:
            val = _safe_float(alt.get("utility"))
            if val is not None:
                candidates.append((val, -pidx, mode))

    if candidates:
        # max utility or max(-time), and deterministic tie-break by fixed mode priority
        candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
        return candidates[0][2]

    if fallback_modes:
        fallback_modes.sort(key=lambda x: x[0])
        return fallback_modes[0][1]

    return None


def _fallback_to_existing_mode(trip: Dict[str, Any], existing_mode: Optional[str]) -> Tuple[Optional[str], str]:
    mode = _norm_mode(existing_mode)
    if mode is None:
        return None, "fallback_existing_mode_missing"
    if get_alt(trip, mode) is None:
        return None, "fallback_existing_mode_no_alt"
    return mode, "fallback_existing_mode"


def set_selected(trip: Dict[str, Any], chosen_mode: str) -> bool:
    found = False
    for alt in trip.get("alternativeModes", []):
        is_chosen = alt.get("mode") == chosen_mode
        alt["selected"] = bool(is_chosen)
        if is_chosen:
            found = True
    return found


def overwrite_top_fields(trip: Dict[str, Any], chosen_mode: str) -> Optional[str]:
    alt = get_alt(trip, chosen_mode)
    if alt is None:
        return f"missing_alt:{chosen_mode}"
    if "timeMinute" not in alt or "distanceKilometer" not in alt:
        return f"missing_time_or_distance:{chosen_mode}"
    trip["timeMinute"] = alt.get("timeMinute")
    trip["distanceKilometer"] = alt.get("distanceKilometer")
    return None


def _collect_agent_day_trips(output_obj: Dict[str, Any]) -> Dict[Tuple[int, int], List[_TripRef]]:
    groups: Dict[Tuple[int, int], List[_TripRef]] = {}
    for agent in output_obj.get("agents", []):
        try:
            agent_id = int(agent.get("id"))
        except (TypeError, ValueError):
            continue
        for day_plan in agent.get("mobilityDemand", []):
            try:
                day = int(day_plan.get("day"))
            except (TypeError, ValueError):
                continue
            plan = day_plan.get("plan", [])
            key_ad = (agent_id, day)
            refs = groups.setdefault(key_ad, [])
            for plan_index, item in enumerate(plan):
                if item.get("type") != "Trip":
                    continue
                try:
                    trip_leg_id = int(item.get("legID"))
                except (TypeError, ValueError):
                    continue
                refs.append(
                    _TripRef(
                        agent_id=agent_id,
                        day=day,
                        plan_index=plan_index,
                        trip_leg_id=trip_leg_id,
                        trip=item,
                    )
                )
    return groups


def _agent_lookup(output_obj: Dict[str, Any]) -> Dict[int, Dict[str, Any]]:
    out: Dict[int, Dict[str, Any]] = {}
    for a in output_obj.get("agents", []):
        try:
            out[int(a.get("id"))] = a
        except (TypeError, ValueError):
            continue
    return out


def run_stage1(
    output_obj: Dict[str, Any],
    car_time_ratio_cap: float = DEFAULT_CAR_TIME_RATIO_CAP,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]], Dict[str, Any]]:
    out_obj = copy.deepcopy(output_obj)
    groups = _collect_agent_day_trips(out_obj)
    agents = _agent_lookup(out_obj)

    reset_rows: List[Dict[str, Any]] = []
    triggered_count = 0

    for (agent_id, day), refs in sorted(groups.items()):
        if not refs:
            continue
        agent = agents.get(agent_id, {})
        mobility_group = agent.get("mobilityGroup")
        is_car_user = mobility_group == "CAR_USER"
        any_car = any(str(r.trip.get("mode")) in CAR_SET for r in refs)

        drt_available_trip_count = 0
        faster_trip_count = 0
        for r in refs:
            alt_drt = get_alt(r.trip, "DRT")
            alt_pt = get_alt(r.trip, "PUBLIC_TRANSIT")
            drt_t = None if alt_drt is None else _safe_float(alt_drt.get("timeMinute"))
            pt_t = None if alt_pt is None else _safe_float(alt_pt.get("timeMinute"))
            if drt_t is not None and drt_t != -1:
                drt_available_trip_count += 1
                if _drt_rule_satisfied(
                    r.trip,
                    original_mode=_norm_mode(r.trip.get("mode")),
                    mobility_group=mobility_group,
                    car_time_ratio_cap=car_time_ratio_cap,
                ):
                    faster_trip_count += 1

        triggered = bool(
            is_car_user
            and any_car
            and drt_available_trip_count > 0
            and faster_trip_count > 0
        )

        if triggered:
            triggered_count += 1
            for r in refs:
                r.trip["mode"] = None

        reset_rows.append(
            {
                "agent_id": agent_id,
                "day": day,
                "trip_count": len(refs),
                "drt_available_trip_count": drt_available_trip_count,
                "faster_trip_count": faster_trip_count,
                "triggered": 1 if triggered else 0,
            }
        )

    stats = {
        "agent_day_total": len(groups),
        "triggered_count": triggered_count,
    }
    return out_obj, reset_rows, stats


def run_stage2(
    output_obj: Dict[str, Any],
    stage0_keys_set: Set[TripKey],
    prior_mode_map: Optional[Mapping[TripKey, Any]] = None,
    car_time_ratio_cap: float = DEFAULT_CAR_TIME_RATIO_CAP,
    fallback_criteria: str = "time",
) -> Tuple[Dict[str, Any], List[Dict[str, Any]], Dict[str, Any]]:
    out_obj = copy.deepcopy(output_obj)
    groups = _collect_agent_day_trips(out_obj)
    agents = _agent_lookup(out_obj)

    stage0_keys_norm: Set[TripKey] = {
        (int(k[0]), int(k[1]), int(k[2]), int(k[3]))
        for k in stage0_keys_set
    }

    trips_total = 0
    trips_base_scope_count = 0
    drt_available_in_scope_count = 0
    errors: List[str] = []
    original_mode_by_key: Dict[TripKey, Optional[str]] = {}

    # Mark base scope: mode is null OR in current iteration Stage0 keys.
    for refs in groups.values():
        for r in refs:
            trips_total += 1
            in_scope = (r.trip.get("mode") is None) or (r.key in stage0_keys_norm)
            r.in_scope = in_scope
            original_mode = None
            if prior_mode_map is not None and r.key in prior_mode_map:
                original_mode = _norm_mode(prior_mode_map[r.key])
            if original_mode is None:
                original_mode = _norm_mode(r.trip.get("mode"))
            original_mode_by_key[r.key] = original_mode
            if in_scope:
                trips_base_scope_count += 1
                alt_drt = get_alt(r.trip, "DRT")
                drt_t = None if alt_drt is None else _safe_float(alt_drt.get("timeMinute"))
                if drt_t is not None and drt_t != -1:
                    drt_available_in_scope_count += 1

    pass1_choice: Dict[TripKey, Tuple[str, str]] = {}
    car_free_day: Dict[Tuple[int, int], bool] = {}

    # PASS 1: base scope decision with normal fallback.
    for ad_key, refs in groups.items():
        mobility_group = agents.get(ad_key[0], {}).get("mobilityGroup")
        has_drt = False
        for r in refs:
            if not r.in_scope:
                continue
            if _drt_rule_satisfied(
                r.trip,
                original_mode=original_mode_by_key.get(r.key),
                mobility_group=mobility_group,
                car_time_ratio_cap=car_time_ratio_cap,
            ):
                chosen = "DRT"
                reason = "DRT<=PT"
            else:
                chosen, reason = _fallback_to_existing_mode(
                    r.trip, original_mode_by_key.get(r.key)
                )

            if chosen is None:
                errors.append(
                    (
                        "pass1_choose_failed"
                        f"|agent={r.agent_id}|day={r.day}|plan_index={r.plan_index}|legID={r.trip_leg_id}"
                        f"|reason={reason}"
                    )
                )
                continue

            pass1_choice[r.key] = (chosen, reason)
            if chosen == "DRT":
                has_drt = True

        car_free_day[ad_key] = has_drt

    # PASS 2: finalize with car-free enforcement.
    change_rows: List[Dict[str, Any]] = []
    trips_updated_count_total = 0

    for ad_key, refs in groups.items():
        day_is_car_free = car_free_day.get(ad_key, False)
        mobility_group = agents.get(ad_key[0], {}).get("mobilityGroup")
        for r in refs:
            before_mode = r.trip.get("mode")

            if not day_is_car_free:
                if r.key not in pass1_choice:
                    continue
                chosen_mode, reason = pass1_choice[r.key]
            else:
                pass1 = pass1_choice.get(r.key)
                if pass1 is not None and pass1[0] == "DRT":
                    chosen_mode = "DRT"
                    reason = "DRT<=PT"
                elif r.in_scope and _drt_rule_satisfied(
                    r.trip,
                    original_mode=original_mode_by_key.get(r.key),
                    mobility_group=mobility_group,
                    car_time_ratio_cap=car_time_ratio_cap,
                ):
                    chosen_mode = "DRT"
                    reason = "DRT<=PT"
                else:
                    original_mode = original_mode_by_key.get(r.key)
                    if _norm_mode(original_mode) in CAR_SET:
                        chosen_mode = choose_fallback_mode(r.trip, CARFREE_FALLBACK, criteria=fallback_criteria)
                        reason = "carfree_forced"
                    else:
                        chosen_mode, reason = _fallback_to_existing_mode(r.trip, original_mode)

                if chosen_mode is None:
                    errors.append(
                        (
                            "pass2_choose_failed"
                            f"|agent={r.agent_id}|day={r.day}|plan_index={r.plan_index}|legID={r.trip_leg_id}"
                            f"|reason={reason}"
                        )
                    )
                    continue

            r.trip["mode"] = chosen_mode

            found_alt = set_selected(r.trip, chosen_mode)
            if not found_alt:
                errors.append(
                    f"selected_sync_failed|agent={r.agent_id}|day={r.day}|plan_index={r.plan_index}|legID={r.trip_leg_id}|mode={chosen_mode}"
                )
                continue

            err = overwrite_top_fields(r.trip, chosen_mode)
            if err is not None:
                errors.append(
                    f"top_field_sync_failed|agent={r.agent_id}|day={r.day}|plan_index={r.plan_index}|legID={r.trip_leg_id}|{err}"
                )
                continue

            after_mode = r.trip.get("mode")
            if before_mode != after_mode:
                trips_updated_count_total += 1
                change_rows.append(
                    {
                        "agent_id": r.agent_id,
                        "day": r.day,
                        "plan_index": r.plan_index,
                        "trip_leg_id": r.trip_leg_id,
                        "before_mode": before_mode,
                        "after_mode": after_mode,
                        "car_free_day": 1 if day_is_car_free else 0,
                        "in_scope": 1 if r.in_scope else 0,
                        "reason": reason,
                    }
                )

    # Global sync: keep selected/time/distance consistent with the final trip.mode.
    for refs in groups.values():
        for r in refs:
            mode_now = r.trip.get("mode")
            if mode_now is None:
                continue
            mode_now = str(mode_now)
            found_alt = set_selected(r.trip, mode_now)
            if not found_alt:
                errors.append(
                    f"global_sync_selected_failed|agent={r.agent_id}|day={r.day}|plan_index={r.plan_index}|legID={r.trip_leg_id}|mode={mode_now}"
                )
                continue
            err = overwrite_top_fields(r.trip, mode_now)
            if err is not None:
                errors.append(
                    f"global_sync_top_field_failed|agent={r.agent_id}|day={r.day}|plan_index={r.plan_index}|legID={r.trip_leg_id}|{err}"
                )

    mode_counts_after: Dict[str, int] = {}
    drt_chosen_count = 0
    for refs in groups.values():
        for r in refs:
            mode = str(r.trip.get("mode"))
            mode_counts_after[mode] = mode_counts_after.get(mode, 0) + 1
            if mode == "DRT":
                drt_chosen_count += 1

    stats = {
        "trips_total": trips_total,
        "trips_base_scope_count": trips_base_scope_count,
        "trips_updated_count_total": trips_updated_count_total,
        "drt_available_in_scope_count": drt_available_in_scope_count,
        "drt_chosen_count": drt_chosen_count,
        "car_free_day_count": sum(1 for v in car_free_day.values() if v),
        "mode_counts_after": mode_counts_after,
        "error_count": len(errors),
        "errors": errors,
    }
    return out_obj, change_rows, stats
