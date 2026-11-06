#!/usr/bin/env python
"""Run Stage1 + Stage2 on a simulation run output."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.append(str(Path(__file__).resolve().parents[1]))

from drt_pipeline.io_utils import read_json, write_json_atomic
from drt_pipeline.stage12 import parse_stage0_keys, run_stage1, run_stage2
from drt_pipeline import travel

FIXED_MODE_COLUMNS = [
    "CAR_DRIVER",
    "CAR_PASSENGER",
    "PUBLIC_TRANSIT",
    "FOOT",
    "BICYCLE",
    "DRT",
    "UNDEFINED",
]


def _safe_float(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _iter_trip_records(obj: Dict[str, Any]):
    for agent in obj.get("agents", []):
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
            for plan_index, item in enumerate(plan):
                if item.get("type") != "Trip":
                    continue
                try:
                    leg_id = int(item.get("legID"))
                except (TypeError, ValueError):
                    continue
                yield (agent_id, day, plan_index, leg_id, item)


def _mode_map(obj: Dict[str, Any]) -> Dict[Tuple[int, int, int, int], Any]:
    out: Dict[Tuple[int, int, int, int], Any] = {}
    for aid, day, pidx, leg, trip in _iter_trip_records(obj):
        out[(aid, day, pidx, leg)] = trip.get("mode")
    return out


def _sum_drt_vehicle_vmt(run_dir: Path) -> float:
    hour_kpi = run_dir / "metrics" / "hour_kpi.csv"
    if not hour_kpi.exists():
        return 0.0
    total = 0.0
    with open(hour_kpi, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                total += float(row.get("vmt_total_km", 0.0))
            except (TypeError, ValueError):
                continue
    return total


def _count_trips_tours(obj: Dict[str, Any]) -> Tuple[int, int]:
    trips_trip_cnt = 0
    tours_tour_cnt = 0
    for agent in obj.get("agents", []):
        for day_plan in agent.get("mobilityDemand", []):
            day_trip_cnt = 0
            for item in day_plan.get("plan", []):
                if item.get("type") == "Trip":
                    day_trip_cnt += 1
            trips_trip_cnt += day_trip_cnt
            if day_trip_cnt > 0:
                tours_tour_cnt += 1
    return trips_trip_cnt, tours_tour_cnt


def _sum_trip_time_min(obj: Dict[str, Any]) -> Tuple[float, int]:
    total = 0.0
    valid_cnt = 0
    for _, _, _, _, trip in _iter_trip_records(obj):
        tm = _safe_float(trip.get("timeMinute"))
        if tm is None:
            continue
        total += tm
        valid_cnt += 1
    return total, valid_cnt


def _drt_passenger_and_occupancy_metrics(run_dir: Path) -> Dict[str, Optional[float]]:
    req_path = run_dir / "kpi" / "requests.csv"
    veh_path = run_dir / "kpi" / "vehicles.csv"

    drt_passenger_time_sum_min = 0.0
    drt_served_trip_cnt = 0
    drt_passenger_onboard_time_sum_min = 0.0
    drt_passenger_onboard_dist_sum_km = 0.0

    if req_path.exists():
        with open(req_path, "r", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                drt_t = _safe_float(row.get("drt_time_min"))
                if drt_t is None or drt_t < 0:
                    continue
                drt_served_trip_cnt += 1
                drt_passenger_time_sum_min += drt_t

                onboard_dist = _safe_float(row.get("onboard_dist_km"))
                if onboard_dist is not None:
                    drt_passenger_onboard_dist_sum_km += onboard_dist

                pu = _safe_float(row.get("pickup_time_min"))
                do = _safe_float(row.get("dropoff_time_min"))
                if pu is not None and do is not None and do >= pu:
                    drt_passenger_onboard_time_sum_min += (do - pu)

    drt_vehicle_service_distance_sum_km = 0.0
    drt_vehicle_service_time_sum_min = 0.0
    if veh_path.exists():
        with open(veh_path, "r", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                d = _safe_float(row.get("vmt_service_km"))
                t = _safe_float(row.get("time_service_min"))
                if d is not None:
                    drt_vehicle_service_distance_sum_km += d
                if t is not None:
                    drt_vehicle_service_time_sum_min += t

    occ_dist = None
    if drt_vehicle_service_distance_sum_km > 0:
        occ_dist = drt_passenger_onboard_dist_sum_km / drt_vehicle_service_distance_sum_km

    occ_time = None
    if drt_vehicle_service_time_sum_min > 0:
        occ_time = drt_passenger_onboard_time_sum_min / drt_vehicle_service_time_sum_min

    return {
        "drt_passenger_time_sum_min": drt_passenger_time_sum_min,
        "drt_passenger_time_avg_min": (
            drt_passenger_time_sum_min / drt_served_trip_cnt if drt_served_trip_cnt > 0 else None
        ),
        "drt_passenger_onboard_time_sum_min": drt_passenger_onboard_time_sum_min,
        "drt_passenger_onboard_dist_sum_km": drt_passenger_onboard_dist_sum_km,
        "drt_vehicle_service_time_sum_min": drt_vehicle_service_time_sum_min,
        "drt_vehicle_service_distance_sum_km": drt_vehicle_service_distance_sum_km,
        "drt_avg_occupancy_time_pax": occ_time,
        "drt_avg_occupancy_distance_pax": occ_dist,
    }


def _build_kpi_row(
    iter_idx: int,
    run_dir: Path,
    input_obj: Dict[str, Any],
    stage2_obj: Dict[str, Any],
    stage1_stats: Dict[str, Any],
    stage2_stats: Dict[str, Any],
    drt_vehicle_vmt_km: float,
    car_time_ratio_cap: float,
) -> Dict[str, Any]:
    mode_counts_after = dict(stage2_stats.get("mode_counts_after", {}))
    sim_trips_trip_cnt, sim_tours_tour_cnt = _count_trips_tours(input_obj)
    stage2_trips_trip_cnt, stage2_tours_tour_cnt = _count_trips_tours(stage2_obj)
    sim_passenger_time_sum_min, sim_trip_time_valid_cnt = _sum_trip_time_min(input_obj)
    stage2_passenger_time_sum_min, stage2_trip_time_valid_cnt = _sum_trip_time_min(stage2_obj)
    drt_occ_metrics = _drt_passenger_and_occupancy_metrics(run_dir)

    before_modes = _mode_map(input_obj)
    after_modes = _mode_map(stage2_obj)
    mode_change_count_total = 0
    for key, before_mode in before_modes.items():
        if after_modes.get(key) != before_mode:
            mode_change_count_total += 1

    car_vmt_after = 0.0
    for _, _, _, _, trip in _iter_trip_records(stage2_obj):
        mode = str(trip.get("mode"))
        if mode not in {"CAR_DRIVER", "CAR_PASSENGER"}:
            continue
        try:
            dist = float(trip.get("distanceKilometer"))
        except (TypeError, ValueError):
            continue
        car_vmt_after += dist

    others = 0
    for m, c in mode_counts_after.items():
        if m not in FIXED_MODE_COLUMNS:
            others += int(c)

    row: Dict[str, Any] = {
        "iter": iter_idx,
        "car_time_ratio_cap": car_time_ratio_cap,
        "trips_total": int(stage2_stats.get("trips_total", 0)),
        "trips_base_scope_count": int(stage2_stats.get("trips_base_scope_count", 0)),
        "trips_updated_count_total": int(stage2_stats.get("trips_updated_count_total", 0)),
        "stage1_tours_total_tour_cnt": int(stage1_stats.get("agent_day_total", 0)),
        "stage1_triggered_tours_tour_cnt": int(stage1_stats.get("triggered_count", 0)),
        "sim_trips_trip_cnt": sim_trips_trip_cnt,
        "sim_tours_tour_cnt": sim_tours_tour_cnt,
        "stage2_trips_trip_cnt": stage2_trips_trip_cnt,
        "stage2_tours_tour_cnt": stage2_tours_tour_cnt,
        "drt_available_in_scope_count": int(stage2_stats.get("drt_available_in_scope_count", 0)),
        "drt_chosen_count": int(stage2_stats.get("drt_chosen_count", 0)),
        "car_free_day_count": int(stage2_stats.get("car_free_day_count", 0)),
        "car_vmt_after_km": car_vmt_after,
        "drt_vehicle_vmt_km": drt_vehicle_vmt_km,
        "total_vmt_after_km": car_vmt_after + drt_vehicle_vmt_km,
        "drt_passenger_time_sum_min": drt_occ_metrics["drt_passenger_time_sum_min"],
        "drt_passenger_time_avg_min": drt_occ_metrics["drt_passenger_time_avg_min"],
        "drt_passenger_onboard_time_sum_min": drt_occ_metrics["drt_passenger_onboard_time_sum_min"],
        "drt_passenger_onboard_dist_sum_km": drt_occ_metrics["drt_passenger_onboard_dist_sum_km"],
        "drt_vehicle_service_time_sum_min": drt_occ_metrics["drt_vehicle_service_time_sum_min"],
        "drt_vehicle_service_distance_sum_km": drt_occ_metrics["drt_vehicle_service_distance_sum_km"],
        "drt_avg_occupancy_time_pax": drt_occ_metrics["drt_avg_occupancy_time_pax"],
        "drt_avg_occupancy_distance_pax": drt_occ_metrics["drt_avg_occupancy_distance_pax"],
        "sim_passenger_time_sum_min": sim_passenger_time_sum_min,
        "sim_trip_time_valid_cnt": sim_trip_time_valid_cnt,
        "stage2_passenger_time_sum_min": stage2_passenger_time_sum_min,
        "stage2_trip_time_valid_cnt": stage2_trip_time_valid_cnt,
        "stage2_passenger_time_avg_min_per_trip": (
            stage2_passenger_time_sum_min / stage2_trip_time_valid_cnt if stage2_trip_time_valid_cnt > 0 else None
        ),
        "passenger_time_delta_stage2_minus_sim_min": stage2_passenger_time_sum_min - sim_passenger_time_sum_min,
        "mode_change_count_total": mode_change_count_total,
        "stage2_error_count": int(stage2_stats.get("error_count", 0)),
        "mode_count_OTHERS": others,
        "mode_counts_after_json": json.dumps(mode_counts_after, ensure_ascii=False, sort_keys=True),
    }

    for mode in FIXED_MODE_COLUMNS:
        row[f"mode_count_{mode}"] = int(mode_counts_after.get(mode, 0))

    return row


def _write_csv(path: Path, fieldnames: List[str], rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _append_csv_row(path: Path, row: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    file_exists = path.exists()
    fieldnames = list(row.keys())
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run_dir", required=True, type=Path)
    ap.add_argument("--stage0_requests_csv", required=True, type=Path)
    ap.add_argument("--iter", required=True, type=int)
    ap.add_argument("--car_time_ratio_cap", type=float, default=2.0)
    ap.add_argument("--fallback_criteria", type=str, choices=["time", "utility"], default="time")
    ap.add_argument("--od_cache", type=str, default=None, help="Path to GraphHopper OD Cache JSON")

    ap.add_argument("--od_cache_strict", action="store_true")
    args = ap.parse_args()

    if args.od_cache:
        travel.load_od_cache(args.od_cache, strict=args.od_cache_strict)

    if args.car_time_ratio_cap <= 0:
        raise ValueError("--car_time_ratio_cap must be > 0")

    run_dir = args.run_dir
    iter_idx = int(args.iter)
    output_path = run_dir / "output_withDRT.json"
    if not output_path.exists():
        raise FileNotFoundError(output_path)
    if not args.stage0_requests_csv.exists():
        raise FileNotFoundError(args.stage0_requests_csv)

    input_obj = read_json(output_path)
    stage0_keys = parse_stage0_keys(str(args.stage0_requests_csv))
    prior_mode_map = _mode_map(input_obj)

    stage1_obj, stage1_rows, stage1_stats = run_stage1(
        input_obj,
        car_time_ratio_cap=float(args.car_time_ratio_cap),
    )
    stage2_obj, stage2_rows, stage2_stats = run_stage2(
        stage1_obj,
        stage0_keys,
        prior_mode_map=prior_mode_map,
        car_time_ratio_cap=float(args.car_time_ratio_cap),
        fallback_criteria=args.fallback_criteria,
    )

    stage12_dir = run_dir / "stage12"
    stage12_dir.mkdir(parents=True, exist_ok=True)

    stage1_json = stage12_dir / f"output_afterStage1_iter{iter_idx:03d}.json"
    stage2_json = stage12_dir / f"output_afterStage2_iter{iter_idx:03d}.json"
    stage1_csv = stage12_dir / f"stage1_reset_tours_iter{iter_idx:03d}.csv"
    stage2_csv = stage12_dir / f"stage2_mode_changes_iter{iter_idx:03d}.csv"
    stage12_cfg_json = stage12_dir / "stage12_config.json"

    write_json_atomic(stage1_json, stage1_obj, indent=None)
    write_json_atomic(stage2_json, stage2_obj, indent=None)
    write_json_atomic(
        stage12_cfg_json,
        {
            "iter": iter_idx,
            "car_time_ratio_cap": float(args.car_time_ratio_cap),
            "fallback_criteria": args.fallback_criteria,
            "od_cache": travel._normalise_cache_path(args.od_cache) if args.od_cache else None,
            "od_cache_stats": travel.get_od_cache_stats(),
            "stage0_requests_csv": str(args.stage0_requests_csv),
        },
        indent=2,
    )

    _write_csv(
        stage1_csv,
        ["agent_id", "day", "trip_count", "drt_available_trip_count", "faster_trip_count", "triggered"],
        stage1_rows,
    )

    _write_csv(
        stage2_csv,
        [
            "agent_id",
            "day",
            "plan_index",
            "trip_leg_id",
            "before_mode",
            "after_mode",
            "car_free_day",
            "in_scope",
            "reason",
        ],
        stage2_rows,
    )

    drt_vehicle_vmt_km = _sum_drt_vehicle_vmt(run_dir)
    kpi_row = _build_kpi_row(
        iter_idx=iter_idx,
        run_dir=run_dir,
        input_obj=input_obj,
        stage2_obj=stage2_obj,
        stage1_stats=stage1_stats,
        stage2_stats=stage2_stats,
        drt_vehicle_vmt_km=drt_vehicle_vmt_km,
        car_time_ratio_cap=float(args.car_time_ratio_cap),
    )
    _append_csv_row(stage12_dir / "iteration_kpi.csv", kpi_row)

    if args.od_cache_strict and int(stage2_stats.get("error_count", 0)):
        raise RuntimeError(f"Stage2 errors: {stage2_stats}")
    print(f"Stage1/2 complete: {stage12_dir}")
    print(f"- Stage1 tours triggered: {stage1_stats.get('triggered_count', 0)}")
    print(f"- Stage2 mode changes: {len(stage2_rows)}")
    print(f"- Stage2 errors: {stage2_stats.get('error_count', 0)}")


if __name__ == "__main__":
    main()
