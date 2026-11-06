"""Existing KPI calculations extracted from the Korean reporting script.
Server-specific paths and batch discovery are intentionally excluded.
"""
from __future__ import annotations
import csv
import json
import math
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple
TripKey = Tuple[str, str, int, str]

def _f(value: Any) -> float:
    try:
        if value is None or value == "":
            return 0.0
        out = float(value)
        if math.isnan(out):
            return 0.0
        return out
    except Exception:
        return 0.0


def _i(value: Any) -> int:
    return int(round(_f(value)))


def _mode(value: Any) -> str:
    text = str(value or "").strip().upper()
    return text if text else "UNKNOWN"


def _read_csv(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def _write_csv(path: Path, rows: List[Mapping[str, Any]], fieldnames: Optional[List[str]] = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fieldnames is None:
        fieldnames = list(rows[0].keys()) if rows else []
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fieldnames})


def _fmt_num(value: Any, digits: int = 0) -> str:
    val = _f(value)
    return f"{val:,.{digits}f}"


def _fmt_pct(value: Any, digits: int = 2) -> str:
    return f"{_f(value):,.{digits}f}%"


def _fmt_change(delta: float, pct: float, digits: int = 0) -> str:
    return f"{delta:+,.{digits}f} ({pct:+.2f}%)"


def _plans(agent: Mapping[str, Any]) -> Iterable[Tuple[Mapping[str, Any], int, Mapping[str, Any]]]:
    for demand in agent.get("mobilityDemand", []) or []:
        for plan_index, item in enumerate(demand.get("plan", []) or []):
            yield demand, plan_index, item


def _agent_has_car_passenger(agent: Mapping[str, Any]) -> bool:
    for _demand, _idx, item in _plans(agent):
        if item.get("type") == "Trip" and _mode(item.get("mode")) == "CAR_PASSENGER":
            return True
    return False


@dataclass
class JsonSummary:
    path: Path
    total_agents: int
    excluded_car_passenger_agents: int
    analysis_agents: int
    trip_count: int
    so_agents: int
    wfh_agents: int
    drt_agents: int
    any_impact_agents: int
    so_wfh_agents: int
    so_drt_agents: int
    wfh_drt_agents: int
    all_three_agents: int
    drt_trip_count: int
    passenger_car_vmt_km: float
    total_tt_min: float
    trips: Dict[TripKey, Dict[str, Any]]


def summarize_json(path: Path) -> JsonSummary:
    if not path.exists():
        raise FileNotFoundError(path)
    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)

    total_agents = len(data.get("agents", []) or [])
    excluded = 0
    analysis_agents = 0
    trip_count = 0
    so_agents = 0
    wfh_agents = 0
    drt_agents = 0
    any_impact_agents = 0
    so_wfh_agents = 0
    so_drt_agents = 0
    wfh_drt_agents = 0
    all_three_agents = 0
    drt_trip_count = 0
    passenger_car_vmt = 0.0
    total_tt = 0.0
    trips: Dict[TripKey, Dict[str, Any]] = {}

    for agent in data.get("agents", []) or []:
        if _agent_has_car_passenger(agent):
            excluded += 1
            continue

        analysis_agents += 1
        agent_id = str(agent.get("id", ""))
        has_so = False
        has_wfh = False
        has_drt = False

        for demand, plan_index, item in _plans(agent):
            if item.get("type") == "Activity":
                activity_type = item.get("activityType")
                has_so = has_so or activity_type == "SHARED_OFFICE"
                has_wfh = has_wfh or activity_type == "HOME_OFFICE"
                continue

            if item.get("type") != "Trip":
                continue

            day = str(demand.get("day", ""))
            leg_id = str(item.get("legID", plan_index))
            mode = _mode(item.get("mode"))
            distance = _f(item.get("distanceKilometer"))
            tt = _f(item.get("timeMinute"))
            trip_count += 1
            total_tt += tt

            if mode == "CAR_DRIVER":
                passenger_car_vmt += distance
            if mode == "DRT":
                has_drt = True
                drt_trip_count += 1

            trips[(agent_id, day, plan_index, leg_id)] = {
                "agent_id": agent_id,
                "day": day,
                "plan_index": plan_index,
                "leg_id": leg_id,
                "mode": mode,
                "distance_km": distance,
                "time_min": tt,
                "start_time": item.get("startTime", ""),
            }

        so_agents += int(has_so)
        wfh_agents += int(has_wfh)
        drt_agents += int(has_drt)
        any_impact_agents += int(has_so or has_wfh or has_drt)
        so_wfh_agents += int(has_so and has_wfh)
        so_drt_agents += int(has_so and has_drt)
        wfh_drt_agents += int(has_wfh and has_drt)
        all_three_agents += int(has_so and has_wfh and has_drt)

    return JsonSummary(
        path=path,
        total_agents=total_agents,
        excluded_car_passenger_agents=excluded,
        analysis_agents=analysis_agents,
        trip_count=trip_count,
        so_agents=so_agents,
        wfh_agents=wfh_agents,
        drt_agents=drt_agents,
        any_impact_agents=any_impact_agents,
        so_wfh_agents=so_wfh_agents,
        so_drt_agents=so_drt_agents,
        wfh_drt_agents=wfh_drt_agents,
        all_three_agents=all_three_agents,
        drt_trip_count=drt_trip_count,
        passenger_car_vmt_km=passenger_car_vmt,
        total_tt_min=total_tt,
        trips=trips,
    )


@dataclass
class RunInfo:
    scenario: str
    run_dir: Path
    stage2_json: Path
    iteration_kpi: Dict[str, str]
    requests: List[Dict[str, str]]
    vehicles: List[Dict[str, str]]
    hour_kpi: List[Dict[str, str]]
    run_config: Dict[str, Any]


def _count_pct(count: int, denominator: int) -> str:
    pct = (count / denominator * 100.0) if denominator else 0.0
    return f"{count:,} ({pct:.2f}%)"


def _agents_with_mode(summary: JsonSummary, mode: str) -> set[str]:
    return {trip["agent_id"] for trip in summary.trips.values() if trip["mode"] == mode}


def _fleet_size(run: Optional[RunInfo]) -> int:
    if run is None:
        return 0

    if run.run_config:
        return _i(run.run_config.get("fleet_size"))

    return len(run.vehicles)


def utilization_metrics(run: Optional[RunInfo]) -> Dict[str, Any]:
    fields = {
        "Passenger DRT Adoption Rate (%)": "N/A",
        "Overall DRT Request Conversion Rate (%)": "N/A",
        "Planned Fleet Service Utilization (%)": "N/A",
        "Productive VMT Share (%)": "N/A",
        "Average Vehicle Occupancy (passengers)": "N/A",
    }
    if run is None:
        return fields

    kpi = run.iteration_kpi
    requests_built = _i(kpi.get("stage0_requests_trip_cnt")) or len(run.requests)
    available = _i(kpi.get("drt_available_in_scope_count"))
    chosen = _i(kpi.get("drt_chosen_count"))

    fleet_size = _fleet_size(run)
    service_start = _i(run.run_config.get("service_hour_start", 6))
    service_end = _i(run.run_config.get("service_hour_end", 22))
    service_hours = max(0, service_end - service_start + 1)
    available_vehicle_min = fleet_size * service_hours * 60
    service_vehicle_min = sum(_f(row.get("time_service_min")) for row in run.vehicles)

    total_vehicle_vmt = sum(_f(row.get("vmt_total_km")) for row in run.vehicles)
    service_vehicle_vmt = sum(_f(row.get("vmt_service_km")) for row in run.vehicles)

    fields.update({
        "Passenger DRT Adoption Rate (%)": (
            chosen / available * 100.0 if available else 0.0
        ),
        "Overall DRT Request Conversion Rate (%)": (
            chosen / requests_built * 100.0 if requests_built else 0.0
        ),
        "Planned Fleet Service Utilization (%)": (
            service_vehicle_min / available_vehicle_min * 100.0
            if available_vehicle_min else 0.0
        ),
        "Productive VMT Share (%)": (
            service_vehicle_vmt / total_vehicle_vmt * 100.0
            if total_vehicle_vmt else 0.0
        ),
        "Average Vehicle Occupancy (passengers)": _f(
            kpi.get("drt_avg_occupancy_time_pax")
        ),
    })
    return fields


def _car_to_drt_agents(before: JsonSummary, after: JsonSummary) -> set[str]:
    agents: set[str] = set()
    for key, before_trip in before.trips.items():
        after_trip = after.trips.get(key)
        if not after_trip:
            continue
        if before_trip["mode"] == "CAR_DRIVER" and after_trip["mode"] == "DRT":
            agents.add(before_trip["agent_id"])
    return agents


def main_kpi_row(
    scenario: str,
    summary: JsonSummary,
    baseline_summary: JsonSummary,
    run: Optional[RunInfo] = None,
) -> Dict[str, Any]:
    denom = summary.analysis_agents
    baseline_car = baseline_summary.passenger_car_vmt_km
    baseline_total_vmt = baseline_summary.passenger_car_vmt_km
    baseline_tt = baseline_summary.total_tt_min

    if run:
        kpi = run.iteration_kpi
        car_vmt = _f(kpi.get("car_vmt_after_km"))
        drt_vmt = _f(kpi.get("drt_vehicle_vmt_km"))
        total_vmt = _f(kpi.get("total_vmt_after_km"))
        total_tt = _f(kpi.get("stage2_passenger_time_sum_min"))
        stage2_errors = _i(kpi.get("stage2_error_count"))
        drt_trips = _i(kpi.get("drt_chosen_count")) or summary.drt_trip_count
        trip_count = _i(kpi.get("stage2_trips_trip_cnt")) or summary.trip_count
    else:
        car_vmt = summary.passenger_car_vmt_km
        drt_vmt = 0.0
        total_vmt = car_vmt
        total_tt = summary.total_tt_min
        stage2_errors = 0
        drt_trips = summary.drt_trip_count
        trip_count = summary.trip_count

    pc_delta = car_vmt - baseline_car
    pc_pct = pc_delta / baseline_car * 100.0 if baseline_car else 0.0
    total_vmt_delta = total_vmt - baseline_total_vmt
    total_vmt_pct = total_vmt_delta / baseline_total_vmt * 100.0 if baseline_total_vmt else 0.0
    tt_delta = total_tt - baseline_tt
    tt_pct = tt_delta / baseline_tt * 100.0 if baseline_tt else 0.0
    drt_trip_pct = drt_trips / trip_count * 100.0 if trip_count else 0.0
    private_car_users_baseline = len(_agents_with_mode(baseline_summary, "CAR_DRIVER"))
    private_car_users_after = len(_agents_with_mode(summary, "CAR_DRIVER"))
    private_car_user_reduction = private_car_users_baseline - private_car_users_after
    fleet_size = _fleet_size(run)
    net_vehicle_reduction = private_car_user_reduction - fleet_size
    utilization = utilization_metrics(run)

    return {
        "Scenario": scenario,
        "Total Agents": summary.total_agents,
        "Excluded CAR_PASSENGER Agents": summary.excluded_car_passenger_agents,
        "Analysis Agents": denom,
        "SO Agents (%)": _count_pct(summary.so_agents, denom),
        "WFH Agents (%)": _count_pct(summary.wfh_agents, denom),
        "DRT Agents (%)": _count_pct(summary.drt_agents, denom),
        "Any Impact Agents (%)": _count_pct(summary.any_impact_agents, denom),
        "SO+WFH Both": summary.so_wfh_agents,
        "SO+DRT Both": summary.so_drt_agents,
        "WFH+DRT Both": summary.wfh_drt_agents,
        "All Three": summary.all_three_agents,
        "DRT Trips": drt_trips,
        "DRT Trip Ratio (%)": drt_trip_pct,
        **utilization,
        "Private Car Users Reduced": private_car_user_reduction,
        "DRT Fleet Vehicles": fleet_size,
        "Net Vehicle Reduction": net_vehicle_reduction,
        "Passenger Car VMT (km)": car_vmt,
        "PC VMT Change": _fmt_change(pc_delta, pc_pct),
        "DRT VMT (km)": drt_vmt,
        "Total VMT (km)": total_vmt,
        "Total VMT Change": _fmt_change(total_vmt_delta, total_vmt_pct),
        "Total TT (min)": total_tt,
        "Total TT Change": _fmt_change(tt_delta, tt_pct),
        "Stage2 Errors": stage2_errors,
    }


def _request_lookup(requests: List[Dict[str, str]]) -> Dict[TripKey, Dict[str, str]]:
    lookup: Dict[TripKey, Dict[str, str]] = {}
    for row in requests:
        key = (
            str(row.get("agent_id", "")),
            str(row.get("day", "")),
            _i(row.get("plan_index")),
            str(row.get("trip_leg_id", "")),
        )
        lookup[key] = row
    return lookup


def operation_row(scenario: str, before: JsonSummary, after: JsonSummary, run: RunInfo) -> Dict[str, Any]:
    kpi = run.iteration_kpi
    total_runtime = sum(_f(r.get("runtime_sec")) for r in run.hour_kpi)
    candidates = _i(kpi.get("stage0_candidates_trip_cnt"))
    requests = _i(kpi.get("stage0_requests_trip_cnt"))
    chosen = _i(kpi.get("drt_chosen_count"))
    drt_vmt = _f(kpi.get("drt_vehicle_vmt_km"))
    service_vmt = _f(kpi.get("drt_vehicle_service_distance_sum_km"))
    deadhead_vmt = drt_vmt - service_vmt
    if deadhead_vmt < 0:
        deadhead_vmt = sum(_f(r.get("vmt_deadhead_km")) for r in run.hour_kpi)
    pre_car_agents = _agents_with_mode(before, "CAR_DRIVER")
    post_car_agents = _agents_with_mode(after, "CAR_DRIVER")
    car_to_drt_agents = _car_to_drt_agents(before, after)
    car_free_after_drt_agents = car_to_drt_agents - post_car_agents
    private_car_user_reduction = len(pre_car_agents) - len(post_car_agents)
    fleet_size = _fleet_size(run)
    utilization = utilization_metrics(run)
    return {
        "Scenario": scenario,
        "DRT Candidates": candidates,
        "DRT Requests Built": requests,
        "DRT Available In Scope": _i(kpi.get("drt_available_in_scope_count")),
        "DRT Chosen Trips": chosen,
        "Chosen / Candidates (%)": chosen / candidates * 100.0 if candidates else 0.0,
        **utilization,
        "CAR->DRT Agents": len(car_to_drt_agents),
        "Car-Free After DRT Agents": len(car_free_after_drt_agents),
        "Private Car Users Reduced": private_car_user_reduction,
        "DRT Fleet Vehicles": fleet_size,
        "Net Vehicle Reduction": private_car_user_reduction - fleet_size,
        "DRT Vehicle VMT (km)": drt_vmt,
        "DRT Service VMT (km)": service_vmt,
        "DRT Deadhead VMT (km)": deadhead_vmt,
        "Deadhead Ratio (%)": deadhead_vmt / drt_vmt * 100.0 if drt_vmt else 0.0,
        "Avg Occupancy Time (pax)": _f(kpi.get("drt_avg_occupancy_time_pax")),
        "Avg Occupancy Distance (pax)": _f(kpi.get("drt_avg_occupancy_distance_pax")),
        "Stage1 Triggered Tours": _i(kpi.get("stage1_triggered_tours_tour_cnt")),
        "Mode Changes": _i(kpi.get("mode_change_count_total")),
        "Runtime Sum (sec)": total_runtime,
        "Stage2 Errors": _i(kpi.get("stage2_error_count")),
        "Run Dir": str(run.run_dir),
    }


def vmt_component_rows(
    scenario: str,
    before: JsonSummary,
    after: JsonSummary,
    run: RunInfo,
) -> List[Dict[str, Any]]:
    groups: Dict[str, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for key, before_trip in before.trips.items():
        after_trip = after.trips.get(key)
        if not after_trip:
            continue
        before_mode = before_trip["mode"]
        after_mode = after_trip["mode"]
        before_car = before_trip["distance_km"] if before_mode == "CAR_DRIVER" else 0.0
        after_car = after_trip["distance_km"] if after_mode == "CAR_DRIVER" else 0.0
        car_delta = after_car - before_car
        if before_mode == after_mode and abs(car_delta) < 1e-9:
            continue
        component = f"{before_mode} -> {after_mode}"
        groups[component]["Trip Count"] += 1
        groups[component]["Passenger Car VMT Before (km)"] += before_car
        groups[component]["Passenger Car VMT After (km)"] += after_car
        groups[component]["Passenger Car VMT Change (km)"] += car_delta

    rows: List[Dict[str, Any]] = []
    for component, values in sorted(groups.items(), key=lambda x: (x[0].split(" -> ")[0], x[0])):
        rows.append({
            "Scenario": scenario,
            "VMT Component": component,
            "Trip Count": int(values["Trip Count"]),
            "Passenger Car VMT Before (km)": values["Passenger Car VMT Before (km)"],
            "Passenger Car VMT After (km)": values["Passenger Car VMT After (km)"],
            "Passenger Car VMT Change (km)": values["Passenger Car VMT Change (km)"],
            "DRT Vehicle VMT Added (km)": 0.0,
            "DRT Service VMT (km)": 0.0,
            "DRT Deadhead VMT (km)": 0.0,
            "Deadhead Ratio (%)": "",
            "Net Total VMT Change (km)": values["Passenger Car VMT Change (km)"],
            "Net Total VMT Change (%)": "",
        })

    kpi = run.iteration_kpi
    drt_vmt = _f(kpi.get("drt_vehicle_vmt_km"))
    service_vmt = _f(kpi.get("drt_vehicle_service_distance_sum_km"))
    deadhead_vmt = drt_vmt - service_vmt
    if deadhead_vmt < 0:
        deadhead_vmt = sum(_f(r.get("vmt_deadhead_km")) for r in run.hour_kpi)

    car_delta_total = sum(_f(row["Passenger Car VMT Change (km)"]) for row in rows)
    net_total = car_delta_total + drt_vmt
    base_total_vmt = before.passenger_car_vmt_km
    net_pct = net_total / base_total_vmt * 100.0 if base_total_vmt else 0.0

    rows.append({
        "Scenario": scenario,
        "VMT Component": "DRT vehicle VMT added",
        "Trip Count": "",
        "Passenger Car VMT Before (km)": "",
        "Passenger Car VMT After (km)": "",
        "Passenger Car VMT Change (km)": "",
        "DRT Vehicle VMT Added (km)": drt_vmt,
        "DRT Service VMT (km)": service_vmt,
        "DRT Deadhead VMT (km)": deadhead_vmt,
        "Deadhead Ratio (%)": deadhead_vmt / drt_vmt * 100.0 if drt_vmt else 0.0,
        "Net Total VMT Change (km)": drt_vmt,
        "Net Total VMT Change (%)": "",
    })
    rows.append({
        "Scenario": scenario,
        "VMT Component": "Net total VMT change vs pre-DRT input",
        "Trip Count": "",
        "Passenger Car VMT Before (km)": before.passenger_car_vmt_km,
        "Passenger Car VMT After (km)": after.passenger_car_vmt_km,
        "Passenger Car VMT Change (km)": car_delta_total,
        "DRT Vehicle VMT Added (km)": drt_vmt,
        "DRT Service VMT (km)": service_vmt,
        "DRT Deadhead VMT (km)": deadhead_vmt,
        "Deadhead Ratio (%)": deadhead_vmt / drt_vmt * 100.0 if drt_vmt else 0.0,
        "Net Total VMT Change (km)": net_total,
        "Net Total VMT Change (%)": net_pct,
    })
    return rows


def drt_tt_component_rows(
    scenario: str,
    before: JsonSummary,
    after: JsonSummary,
    run: RunInfo,
) -> List[Dict[str, Any]]:
    request_lookup = _request_lookup(run.requests)
    drt_agent_ids = _agents_with_mode(after, "DRT")
    grouped: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
        "drt_agents": set(),
        "trip_count": 0,
        "drt_trip_count": 0,
        "original_tt": 0.0,
        "after_tt": 0.0,
        "wait": 0.0,
        "onboard": 0.0,
        "lateness": 0.0,
        "onboard_dist": 0.0,
    })

    for key, after_trip in after.trips.items():
        if after_trip["agent_id"] not in drt_agent_ids:
            continue
        before_trip = before.trips.get(key)
        if not before_trip:
            continue
        before_mode = before_trip["mode"]
        after_mode = after_trip["mode"]
        group_name = f"{before_mode} -> {after_mode}"

        is_drt_trip = after_mode == "DRT"
        request = request_lookup.get(key, {}) if is_drt_trip else {}
        call = _f(request.get("call_time_min"))
        pickup = _f(request.get("pickup_time_min"))
        dropoff = _f(request.get("dropoff_time_min"))
        after_time = (_f(request.get("drt_time_min")) or after_trip["time_min"]) if is_drt_trip else after_trip["time_min"]
        wait = max(0.0, pickup - call) if is_drt_trip and pickup and call else 0.0
        onboard = max(0.0, dropoff - pickup) if is_drt_trip and dropoff and pickup else (after_time if is_drt_trip else 0.0)
        lateness = _f(request.get("lateness_min")) if is_drt_trip else 0.0
        onboard_dist = _f(request.get("onboard_dist_km")) if is_drt_trip else 0.0

        for label in (group_name, "All trips by DRT-user agents"):
            bucket = grouped[label]
            bucket["drt_agents"].add(after_trip["agent_id"])
            bucket["trip_count"] += 1
            bucket["drt_trip_count"] += int(is_drt_trip)
            bucket["original_tt"] += before_trip["time_min"]
            bucket["after_tt"] += after_time
            bucket["wait"] += wait
            bucket["onboard"] += onboard
            bucket["lateness"] += lateness
            bucket["onboard_dist"] += onboard_dist

    rows: List[Dict[str, Any]] = []
    def sort_key(component: str) -> Tuple[int, float, str]:
        if component == "All trips by DRT-user agents":
            return (0, 0.0, component)
        bucket = grouped[component]
        delta = bucket["after_tt"] - bucket["original_tt"]
        return (1, -abs(delta), component)

    for component in sorted(grouped.keys(), key=sort_key):
        bucket = grouped[component]
        trips = int(bucket["trip_count"])
        original = bucket["original_tt"]
        after_total = bucket["after_tt"]
        change = after_total - original
        rows.append({
            "Scenario": scenario,
            "TT Component": component,
            "Trip Count": trips,
            "DRT Trip Count": int(bucket["drt_trip_count"]),
            "DRT Agent Count": len(bucket["drt_agents"]),
            "Original TT Sum (min)": original,
            "Original TT Avg (min/trip)": original / trips if trips else 0.0,
            "After TT Sum (min)": after_total,
            "After TT Avg (min/trip)": after_total / trips if trips else 0.0,
            "DRT Wait Sum (min)": bucket["wait"],
            "DRT Wait Avg (min/trip)": bucket["wait"] / trips if trips else 0.0,
            "DRT Onboard Sum (min)": bucket["onboard"],
            "DRT Onboard Avg (min/trip)": bucket["onboard"] / trips if trips else 0.0,
            "TT Change Sum (min)": change,
            "TT Change Avg (min/trip)": change / trips if trips else 0.0,
            "TT Change (%)": change / original * 100.0 if original else 0.0,
            "Lateness Sum (min)": bucket["lateness"],
            "Lateness Avg (min/trip)": bucket["lateness"] / trips if trips else 0.0,
            "Onboard Distance Sum (km)": bucket["onboard_dist"],
        })
    return rows

