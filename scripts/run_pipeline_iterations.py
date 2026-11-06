#!/usr/bin/env python
"""Iterative pipeline runner: Stage0 -> Simulation -> Stage1/2."""

from __future__ import annotations

import argparse
import csv
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.append(str(Path(__file__).resolve().parents[1]))

from drt_pipeline.io_utils import now_stamp, read_json, write_json_atomic
from drt_pipeline.simulate import run_simulation_from_stage0
from drt_pipeline.stage0 import (
    build_stage0_requests,
    ensure_drt_altmode_for_all_trips,
    stage0_identify_candidates,
    summarize_trip_tour_units,
)
from drt_pipeline import travel


def _write_stage0_outputs(state_in_json: Path, stage0_dir: Path, seed: int = 42, like_threshold: int = 3,
                          drt_like_threshold: float = None, drt_like_sub_ratio: float = 1.0) -> Dict[str, Any]:
    if stage0_dir.exists():
        raise FileExistsError(f"stage0_dir already exists: {stage0_dir}")
    stage0_dir.mkdir(parents=True, exist_ok=False)

    output = read_json(state_in_json)
    
    # Filter out any agent that contains CAR_PASSENGER in their plan
    filtered_agents = []
    for ag in output.get("agents", []):
        has_car_passenger = False
        for md in ag.get("mobilityDemand", []):
            for item in md.get("plan", []):
                if item.get("type") == "Trip" and str(item.get("mode", "")).upper() == "CAR_PASSENGER":
                    has_car_passenger = True
                    break
            if has_car_passenger:
                break
        if not has_car_passenger:
            filtered_agents.append(ag)
    output["agents"] = filtered_agents

    units = summarize_trip_tour_units(output)
    added = ensure_drt_altmode_for_all_trips(output)
    candidates, stats0 = stage0_identify_candidates(
        output, seed=seed, like_threshold=like_threshold,
        drt_like_threshold=drt_like_threshold, drt_like_sub_ratio=drt_like_sub_ratio,
    )
    reqs, stats1 = build_stage0_requests(output, candidates)

    write_json_atomic(stage0_dir / "output_with_drt_stub.json", output, indent=None)

    with open(stage0_dir / "stage0_requests.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "req_id",
                "agent_id",
                "day",
                "plan_index",
                "trip_leg_id",
                "call_time_min",
                "pickup_lat",
                "pickup_lon",
                "drop_lat",
                "drop_lon",
                "o_region",
                "d_region",
                "hour",
                "tt_pt_min",
                "latest_min",
            ]
        )
        for r in reqs:
            w.writerow(
                [
                    r.req_id,
                    r.trip_key.agent_id,
                    r.trip_key.day,
                    r.trip_key.plan_index,
                    r.trip_key.trip_leg_id,
                    r.call_time_min,
                    r.pickup_lat,
                    r.pickup_lon,
                    r.drop_lat,
                    r.drop_lon,
                    r.o_region.value,
                    r.d_region.value,
                    r.hour,
                    r.tt_pt_min,
                    r.latest_min,
                ]
            )

    summary = {
        "input": str(state_in_json),
        "seed": seed,
        "stage0_agents_agent_cnt": units["agents_agent_cnt"],
        "stage0_mobility_demands_day_cnt": units["mobility_demands_day_cnt"],
        "stage0_trips_trip_cnt": units["trips_trip_cnt"],
        "stage0_tours_tour_cnt": units["tours_tour_cnt"],
        "drt_altmode_added_trips": added,
        "candidates_count": len(candidates),
        "requests_built": len(reqs),
        "stage0_stats": stats0,
        "build_stats": stats1,
        "outputs": {
            "output_with_drt_stub_json": str(stage0_dir / "output_with_drt_stub.json"),
            "stage0_requests_csv": str(stage0_dir / "stage0_requests.csv"),
        },
    }
    write_json_atomic(stage0_dir / "stage0_summary.json", summary, indent=2)
    return summary


def _read_single_kpi_row(iter_kpi_path: Path) -> Dict[str, Any]:
    with open(iter_kpi_path, "r", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise RuntimeError(f"No rows found in {iter_kpi_path}")
    return rows[-1]


def _append_dict_row(path: Path, row: Dict[str, Any], fieldnames: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        if not exists:
            w.writeheader()
        w.writerow(row)


def _timestamped_dir(base_dir: Path, stamp: str) -> Path:
    """Return a unique directory path by appending execution timestamp.

    Example:
      pipelines/pipeline_150v_30kmh -> pipelines/pipeline_150v_30kmh_20260311_101530
    """
    candidate = base_dir.parent / f"{base_dir.name}_{stamp}"
    if not candidate.exists():
        return candidate
    seq = 2
    while True:
        c = base_dir.parent / f"{base_dir.name}_{stamp}_{seq:02d}"
        if not c.exists():
            return c
        seq += 1


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input_output_json", required=True, type=Path, help="iteration 1 input JSON")
    ap.add_argument("--iters", required=True, type=int)

    ap.add_argument("--workers", type=int, default=18)
    ap.add_argument("--skip_od_build", action="store_true", help="Use an already prepared OD DB")
    ap.add_argument("--od_cache_strict", action="store_true")
    ap.add_argument("--time_limit_sec", type=float, default=10800.0)
    ap.add_argument("--fleet_size", type=int, default=300)
    ap.add_argument("--cap", type=int, default=15)
    ap.add_argument("--alns_iter_cap", type=int, default=12000)
    ap.add_argument("--init_strategy", choices=["fast", "premium"], default="fast")
    ap.add_argument("--init_multistart", type=int, default=8)
    ap.add_argument("--init_regret_k", type=int, default=3)
    ap.add_argument("--init_vehicle_top_k", type=int, default=12)
    ap.add_argument("--init_pos_samples", type=int, default=160)
    ap.add_argument("--init_candidate_pool", type=int, default=24)
    ap.add_argument("--init_time_limit_sec", type=float, default=0.0)
    ap.add_argument("--speed_kmph", type=float, default=45.0)
    ap.add_argument("--rejection_penalty", type=float, default=300.0)
    ap.add_argument(
        "--alns_selective_route_reevaluation",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Re-evaluate only routes touched by ALNS request removal.",
    )
    ap.add_argument(
        "--alns_lightweight_eval_only",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Use lightweight route evaluation for insertion candidates.",
    )
    ap.add_argument(
        "--alns_true_regret2",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Use exact best/second-best insertion costs for regret-2 repair.",
    )
    ap.add_argument(
        "--alns_noop_operator_adaptation",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Penalize structurally unchanged ALNS operator pairs while preserving weight floors.",
    )
    ap.add_argument(
        "--alns_t0_samples",
        type=int,
        default=0,
        help="Override local initial-temperature samples; 0 keeps the solver default.",
    )
    ap.add_argument(
        "--sa_cooling_rate", type=float, default=0.998,
        help="SA cooling multiplier per iteration (0 < value <= 1).",
    )
    ap.add_argument(
        "--sa_initial_acceptance_probability",
        type=float,
        default=0.36787944117144233,
        help="Target initial uphill acceptance probability used to scale T0.",
    )
    ap.add_argument(
        "--sa_stagnation_patience_iters", type=int, default=4000,
        help="Stop after this many iterations without a new best; 0 disables early stop.",
    )
    ap.add_argument(
        "--sa_stagnation_min_iters", type=int, default=1000,
        help="Minimum ALNS iterations before stagnation early stop is allowed.",
    )
    ap.add_argument(
        "--quality_stagnation_stop",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Allow conservative stagnation stopping in the quality profile.",
    )
    ap.add_argument(
        "--sa_accept_rate_window_iters", type=int, default=200,
        help="Rolling window used to track excessive SA acceptance.",
    )
    ap.add_argument(
        "--sa_high_accept_rate_threshold", type=float, default=0.90,
        help="Rolling acceptance rate considered excessively high.",
    )
    ap.add_argument(
        "--alns_quality_profile",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Prioritize exact Stage2-eligible DRT trips, then passenger time and VMT.",
    )
    ap.add_argument("--quality_choice_miss_penalty", type=float, default=10000.0)
    ap.add_argument("--quality_choice_excess_weight", type=float, default=25.0)
    ap.add_argument("--quality_near_deadline_buffer_min", type=float, default=1.0)
    ap.add_argument("--quality_regret_k", type=int, default=3)
    ap.add_argument("--quality_unassigned_repair_limit", type=int, default=16)
    ap.add_argument("--quality_reheat_interval_iters", type=int, default=500)
    ap.add_argument("--quality_max_reheats", type=int, default=4)
    ap.add_argument("--quality_choice_loss_acceptance_cap", type=float, default=0.02)
    ap.add_argument(
        "--fleet_target_strategy",
        choices=["demand", "workload_deadline"],
        default="demand",
    )
    ap.add_argument("--portfolio_replicas", type=int, default=1)
    ap.add_argument(
        "--global_rescue",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Re-optimize the merged hourly routes across all solver groups.",
    )
    ap.add_argument("--global_rescue_replicas", type=int, default=1)
    ap.add_argument("--global_rescue_time_fraction", type=float, default=0.15)
    ap.add_argument(
        "--global_rescue_t0_samples",
        type=int,
        default=0,
        help="Override global-rescue initial-temperature samples; 0 reuses the local setting/default.",
    )
    ap.add_argument(
        "--adaptive_global_rescue",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "Use live-quality rescue tiers with exact full fallback for "
            "unresolved small miss sets."
        ),
    )
    ap.add_argument(
        "--adaptive_global_rescue_compact_polish",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "Limit all-eligible adaptive rescue to one polish wave under "
            "low live downstream demand pressure."
        ),
    )
    ap.add_argument(
        "--adaptive_global_rescue_compact_pressure_threshold",
        type=float,
        default=3.0,
    )
    ap.add_argument("--like_threshold", type=int, default=3,
                    help="DRT likelihood filter (legacy hash mode). like<=threshold excluded. (3:40%%, 2:60%%, 1:80%%, 0:100%%)")
    ap.add_argument("--drt_like_threshold", type=float, default=None,
                    help="Agent drtLike threshold. Agents with drtLike >= this value become DRT candidates. Overrides --like_threshold.")
    ap.add_argument("--drt_like_sub_ratio", type=float, default=1.0,
                    help="Sub-sampling ratio for agents at exactly the drt_like_threshold boundary (0.0-1.0).")
    ap.add_argument("--car_time_ratio_cap", type=float, default=2.0)
    ap.add_argument("--fallback_criteria", type=str, choices=["time", "utility"], default="time",
                    help="Fallback criteria when car-free day is forced (time vs utility). Default is time.")
    ap.add_argument("--service_hour_start", type=int, default=0,
                    help="DRT service start hour (inclusive, 0-23). Default 0")
    ap.add_argument("--service_hour_end", type=int, default=23,
                    help="DRT service end hour (inclusive, 0-23). Default 23.")
    ap.add_argument("--rebalancing_strategy", choices=["default", "sticky", "hungarian"], default="default",
                    help="Vehicle assignment strategy.")

    ap.add_argument("--pipeline_dir", type=Path, default=None)
    ap.add_argument("--runs_dir", type=Path, default=None)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--seed_base", type=int, default=42,
                    help="ALNS solver seed base.")
    ap.add_argument("--gh_url", type=str, default=None,
                    help="Optional GraphHopper server URL (e.g., http://localhost:8989). If set, builds and uses OD Matrix cache.")
    ap.add_argument("--od_cache", type=Path, default=None,
                    help="Optional path to a shared OD cache file (e.g., shared_od_cache.json).")
    ap.add_argument(
        "--solver_travel_cache",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Bounded process-local travel lookup cache. If omitted, it is "
            "enabled for the ALNS quality profile and disabled otherwise."
        ),
    )
    ap.add_argument(
        "--solver_travel_cache_max_entries",
        type=int,
        default=524_288,
        help="Maximum directed OD entries retained by each worker process.",
    )
    ap.add_argument(
        "--winner_only_materialization",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Return lightweight replica route plans and fully materialize only "
            "the selected result. If omitted, follows the quality profile."
        ),
    )
    args = ap.parse_args()

    if args.iters <= 0:
        raise ValueError("--iters must be >= 1")
    if not args.input_output_json.exists():
        raise FileNotFoundError(args.input_output_json)

    exec_stamp = now_stamp()

    if args.pipeline_dir is None:
        pipeline_dir = Path("pipelines") / f"pipeline_{exec_stamp}"
    else:
        # Always append execution timestamp to avoid collisions across reruns.
        pipeline_dir = _timestamped_dir(args.pipeline_dir, exec_stamp)
    pipeline_dir.mkdir(parents=True, exist_ok=False)

    pipeline_id = pipeline_dir.name
    if args.runs_dir is None:
        runs_dir = Path("runs") / pipeline_id
    else:
        # Keep runs/output folders aligned and unique per execution.
        runs_dir = _timestamped_dir(args.runs_dir, exec_stamp)
    runs_dir.mkdir(parents=True, exist_ok=True)

    stage0_root = Path("cache") / pipeline_id
    stage0_root.mkdir(parents=True, exist_ok=True)

    iteration_map_path = pipeline_dir / "iteration_map.csv"
    iteration_kpi_all_path = pipeline_dir / "iteration_kpi_all.csv"
    iteration_failures_path = pipeline_dir / "iteration_failures.csv"

    map_fields = ["iter", "stage0_dir", "run_dir", "stage2_output_json"]
    failure_fields = [
        "iter",
        "stage",
        "stage0_dir",
        "run_dir",
        "failed_run_dir",
        "failure_report_json",
        "error_type",
        "error_message",
    ]

    kpi_fields = [
        "iter",
        "car_time_ratio_cap",
        "stage0_agents_agent_cnt",
        "stage0_mobility_demands_day_cnt",
        "stage0_trips_trip_cnt",
        "stage0_tours_tour_cnt",
        "stage0_candidates_trip_cnt",
        "stage0_requests_trip_cnt",
        "stage0_build_bad_plan_index_trip_cnt",
        "trips_total",
        "trips_base_scope_count",
        "trips_updated_count_total",
        "stage1_tours_total_tour_cnt",
        "stage1_triggered_tours_tour_cnt",
        "sim_trips_trip_cnt",
        "sim_tours_tour_cnt",
        "stage2_trips_trip_cnt",
        "stage2_tours_tour_cnt",
        "drt_available_in_scope_count",
        "drt_chosen_count",
        "car_free_day_count",
        "mode_count_CAR_DRIVER",
        "mode_count_CAR_PASSENGER",
        "mode_count_PUBLIC_TRANSIT",
        "mode_count_FOOT",
        "mode_count_BICYCLE",
        "mode_count_DRT",
        "mode_count_UNDEFINED",
        "mode_count_OTHERS",
        "car_vmt_after_km",
        "drt_vehicle_vmt_km",
        "total_vmt_after_km",
        "drt_passenger_time_sum_min",
        "drt_passenger_time_avg_min",
        "drt_passenger_onboard_time_sum_min",
        "drt_passenger_onboard_dist_sum_km",
        "drt_vehicle_service_time_sum_min",
        "drt_vehicle_service_distance_sum_km",
        "drt_avg_occupancy_time_pax",
        "drt_avg_occupancy_distance_pax",
        "sim_passenger_time_sum_min",
        "sim_trip_time_valid_cnt",
        "stage2_passenger_time_sum_min",
        "stage2_trip_time_valid_cnt",
        "stage2_passenger_time_avg_min_per_trip",
        "passenger_time_delta_stage2_minus_sim_min",
        "mode_change_count_total",
        "stage2_error_count",
        "mode_counts_after_json",
        "stage0_dir",
        "run_dir",
        "stage2_output_json",
    ]

    state_in = args.input_output_json
    run_invocation = subprocess.list2cmdline([sys.executable, *sys.argv])

    for iter_idx in range(1, args.iters + 1):
        stage0_dir = stage0_root / f"iter{iter_idx:03d}" / "stage0"
        run_dir = None
        stage0_summary = {}
        try:
            print("\n========== [STAGE 0] Extracting DRT candidates & building requests ==========")
            stage0_summary = _write_stage0_outputs(
                state_in_json=state_in, stage0_dir=stage0_dir, seed=int(args.seed),
                like_threshold=args.like_threshold,
                drt_like_threshold=args.drt_like_threshold,
                drt_like_sub_ratio=args.drt_like_sub_ratio,
            )

            if args.od_cache:
                od_cache_path = args.od_cache
            else:
                od_cache_path = stage0_root / "od_cache.json"
                
            od_cache_path = Path(travel._normalise_cache_path(str(od_cache_path)))
            if args.gh_url and not args.skip_od_build:
                print(f"Building/Loading GraphHopper OD Cache from {args.gh_url}...")
                subprocess.run([
                    sys.executable, "scripts/build_od_cache.py",
                    "--csv", str(stage0_dir / "stage0_requests.csv"),
                    "--gh_url", args.gh_url,
                    "--out", str(od_cache_path),
                    "--workers", str(max(1, int(args.workers))),
                ], check=True)
                # Load it into memory for the simulate step (run_simulation_from_stage0)
            if args.od_cache or args.gh_url:
                travel.load_od_cache(str(od_cache_path), strict=args.od_cache_strict)

            print("\n========== [STAGE 1] Running ALNS+SA DRT Simulation ==========")
            run_dir = run_simulation_from_stage0(
                stage0_dir=stage0_dir,
                runs_base_dir=runs_dir,
                workers=args.workers,
                global_time_limit_sec=args.time_limit_sec,
                fleet_size=args.fleet_size,
                cap=args.cap,
                alns_iter_cap=args.alns_iter_cap,
                init_strategy=args.init_strategy,
                init_multistart=args.init_multistart,
                init_regret_k=args.init_regret_k,
                init_vehicle_top_k=args.init_vehicle_top_k,
                init_pos_samples=args.init_pos_samples,
                init_candidate_pool=args.init_candidate_pool,
                init_time_limit_sec=args.init_time_limit_sec,
                speed_kmph=args.speed_kmph,
                rejection_penalty=args.rejection_penalty,
                run_invocation=run_invocation,
                service_hour_start=args.service_hour_start,
                service_hour_end=args.service_hour_end,
                rebalancing_strategy=args.rebalancing_strategy,
                seed_base=args.seed_base,
                alns_selective_route_reevaluation=args.alns_selective_route_reevaluation,
                alns_lightweight_eval_only=args.alns_lightweight_eval_only,
                alns_true_regret2=args.alns_true_regret2,
                alns_noop_operator_adaptation=args.alns_noop_operator_adaptation,
                alns_t0_samples=args.alns_t0_samples,
                sa_cooling_rate=args.sa_cooling_rate,
                sa_initial_acceptance_probability=args.sa_initial_acceptance_probability,
                sa_stagnation_patience_iters=args.sa_stagnation_patience_iters,
                sa_stagnation_min_iters=args.sa_stagnation_min_iters,
                quality_stagnation_stop=args.quality_stagnation_stop,
                sa_accept_rate_window_iters=args.sa_accept_rate_window_iters,
                sa_high_accept_rate_threshold=args.sa_high_accept_rate_threshold,
                car_time_ratio_cap=args.car_time_ratio_cap,
                alns_quality_profile=args.alns_quality_profile,
                quality_choice_miss_penalty=args.quality_choice_miss_penalty,
                quality_choice_excess_weight=args.quality_choice_excess_weight,
                quality_near_deadline_buffer_min=args.quality_near_deadline_buffer_min,
                quality_regret_k=args.quality_regret_k,
                quality_unassigned_repair_limit=args.quality_unassigned_repair_limit,
                quality_reheat_interval_iters=args.quality_reheat_interval_iters,
                quality_max_reheats=args.quality_max_reheats,
                quality_choice_loss_acceptance_cap=args.quality_choice_loss_acceptance_cap,
                fleet_target_strategy=args.fleet_target_strategy,
                portfolio_replicas=args.portfolio_replicas,
                global_rescue=args.global_rescue,
                global_rescue_replicas=args.global_rescue_replicas,
                global_rescue_time_fraction=args.global_rescue_time_fraction,
                global_rescue_t0_samples=args.global_rescue_t0_samples,
                adaptive_global_rescue=args.adaptive_global_rescue,
                adaptive_global_rescue_compact_polish=(
                    args.adaptive_global_rescue_compact_polish
                ),
                adaptive_global_rescue_compact_pressure_threshold=(
                    args.adaptive_global_rescue_compact_pressure_threshold
                ),
                od_cache_path=(str(od_cache_path) if (args.od_cache or args.gh_url) else None),
                od_cache_strict=args.od_cache_strict,
                solver_travel_cache=args.solver_travel_cache,
                solver_travel_cache_max_entries=args.solver_travel_cache_max_entries,
                winner_only_materialization=args.winner_only_materialization,
            )

            stage12_cmd = [
                sys.executable,
                "scripts/run_stage12.py",
                "--run_dir",
                str(run_dir),
                "--stage0_requests_csv",
                str(stage0_dir / "stage0_requests.csv"),
                "--iter",
                str(iter_idx),
                "--car_time_ratio_cap",
                str(args.car_time_ratio_cap),
                "--fallback_criteria",
                args.fallback_criteria,
            ]
            
            if args.od_cache or args.gh_url:
                stage12_cmd.extend(["--od_cache", str(od_cache_path)])
                if args.od_cache_strict:
                    stage12_cmd.append("--od_cache_strict")

            print("\n========== [STAGE 2] Evaluating fallback & Finalizing KPIs ==========")
            subprocess.run(
                stage12_cmd,
                check=True,
            )

            stage2_output_json = run_dir / "stage12" / f"output_afterStage2_iter{iter_idx:03d}.json"
            if not stage2_output_json.exists():
                raise FileNotFoundError(stage2_output_json)

            map_row = {
                "iter": iter_idx,
                "stage0_dir": str(stage0_dir),
                "run_dir": str(run_dir),
                "stage2_output_json": str(stage2_output_json),
            }
            _append_dict_row(iteration_map_path, map_row, map_fields)

            run_kpi = _read_single_kpi_row(run_dir / "stage12" / "iteration_kpi.csv")
            run_kpi.update(
                {
                    "stage0_agents_agent_cnt": stage0_summary.get("stage0_agents_agent_cnt", ""),
                    "stage0_mobility_demands_day_cnt": stage0_summary.get("stage0_mobility_demands_day_cnt", ""),
                    "stage0_trips_trip_cnt": stage0_summary.get("stage0_trips_trip_cnt", ""),
                    "stage0_tours_tour_cnt": stage0_summary.get("stage0_tours_tour_cnt", ""),
                    "stage0_candidates_trip_cnt": stage0_summary.get("candidates_count", ""),
                    "stage0_requests_trip_cnt": stage0_summary.get("requests_built", ""),
                    "stage0_build_bad_plan_index_trip_cnt": (
                        stage0_summary.get("build_stats", {}).get("bad_plan_index", "")
                        if isinstance(stage0_summary.get("build_stats"), dict)
                        else ""
                    ),
                    "stage0_dir": str(stage0_dir),
                    "run_dir": str(run_dir),
                    "stage2_output_json": str(stage2_output_json),
                }
            )
            _append_dict_row(iteration_kpi_all_path, run_kpi, kpi_fields)

            state_in = stage2_output_json

            print(
                f"[iter {iter_idx}] stage0={stage0_dir} run={run_dir} stage2={stage2_output_json}"
            )
        except Exception as exc:
            failure_report = str(getattr(exc, "failure_report_path", ""))
            failed_run_dir = str(getattr(exc, "failed_run_dir", ""))
            if not failed_run_dir and run_dir is not None:
                failed_run_dir = str(run_dir)

            _append_dict_row(
                iteration_failures_path,
                {
                    "iter": iter_idx,
                    "stage": "simulation_or_stage12",
                    "stage0_dir": str(stage0_dir),
                    "run_dir": str(run_dir) if run_dir is not None else "",
                    "failed_run_dir": failed_run_dir,
                    "failure_report_json": failure_report,
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                },
                failure_fields,
            )
            print(f"[iter {iter_idx} FAILED] stage0={stage0_dir}", file=sys.stderr)
            if failed_run_dir:
                print(f"- failed_run_dir: {failed_run_dir}", file=sys.stderr)
            if failure_report:
                print(f"- failure_report: {failure_report}", file=sys.stderr)
            raise

    print(f"Pipeline complete: {pipeline_dir}")
    print(f"- iteration_map: {iteration_map_path}")
    print(f"- iteration_kpi_all: {iteration_kpi_all_path}")


if __name__ == "__main__":
    main()
