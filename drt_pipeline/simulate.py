"""Stage0 cache(요청 파일) 기반 DRT 시뮬레이션(시간대×OD×ALNS+SA).

구현 범위:
- Stage0는 별도 script로 1회 수행 후 requests CSV + DRT stub output JSON 저장
- 본 모듈은 stage0 결과를 입력으로 받아:
  - 24시간 순차 처리(차량 carry-over)
  - 시간당 36 OD를 18+18 wave로 병렬 처리
  - Option A: 차량은 한 시간대에 한 OD만 담당
  - ALNS+SA로 DARP 스케줄을 만들고 DRT alternativeModes의 timeMinute/distanceKilometer를 채움
  - KPI/로그(CSV) 저장

주의:
- 1회(run)당 1시간 내 완료를 목표로 글로벌 time limit을 적용하고, hour별 budget을 분배한다.
"""

from __future__ import annotations

import csv
import hashlib
import os
import shutil
import time
import traceback
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .alns_sa import HORIZON_MIN, materialize_route_plan, solve_od_alns_sa
from .choice_rules import (
    DEFAULT_CAR_TIME_RATIO_CAP,
    car_time_cap_applies,
    effective_drt_deadline_min,
    get_alternative,
    normalize_mode,
)
from .constants import (
    ALNS_DIAGNOSTICS_HEADER,
    ALNS_TRACE_HEADER,
    HOUR_KPI_HEADER,
    OD_KPI_HEADER,
    OD_LIST,
    OD_INDEX,
    REQUESTS_HEADER,
    SOLVER_GROUP_INDEX,
    SOLVER_GROUP_LIST,
    VEHICLE_EVENTS_HEADER,
    VEHICLES_HEADER,
    RegionId,
    canonical_solver_group,
    solver_group_label,
    solver_group_mode,
)
from .fleet import (
    VehicleState,
    assign_vehicles_to_ods,
    assign_vehicles_to_solver_groups,
    assign_vehicles_to_solver_groups_hungarian,
    assign_vehicles_to_solver_groups_sticky,
    compute_od_targets,
    compute_solver_group_targets,
    init_fleet_from_requests,
)
from .io_utils import append_file, append_csv_row, now_stamp, read_json, write_csv_header, write_json_atomic
from .regions import map_to_region
from .travel import (
    configure_process_memory_cache,
    get_od_cache_stats,
    load_od_cache,
    set_speed_kmph,
    travel,
)


def _sha256_file(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _initialize_solver_worker(
    cache_path: str,
    strict: bool,
    solver_travel_cache: bool,
    solver_travel_cache_max_entries: int,
) -> None:
    """Load the OD cache in spawned workers as well as Linux fork workers."""
    configure_process_memory_cache(
        bool(solver_travel_cache),
        max_entries=int(solver_travel_cache_max_entries),
    )
    if cache_path:
        load_od_cache(cache_path, strict=bool(strict))


def _stage0_input_identity(
    stage0_dir: Path, stage0_requests_csv: Path, stub_output_path: Path
) -> Dict[str, Any]:
    """Record immutable Stage0/source identities for reproducible A/B runs."""
    identity: Dict[str, Any] = {
        "stage0_requests_sha256": _sha256_file(stage0_requests_csv),
        "stage0_stub_sha256": _sha256_file(stub_output_path),
    }
    summary_path = stage0_dir / "stage0_summary.json"
    if not summary_path.exists():
        return identity
    summary = read_json(summary_path)
    source_value = summary.get("input") if isinstance(summary, dict) else None
    if not source_value:
        return identity
    source_path = Path(str(source_value))
    identity["source_input_json"] = str(source_path)
    if source_path.is_file():
        identity["source_input_sha256"] = _sha256_file(source_path)
    else:
        identity["source_input_sha256"] = None
        identity["source_input_missing"] = True
    return identity


def _parse_region(s: str) -> RegionId:
    return RegionId(s)


def _finalize_run_directory(src: Path, dst: Path, retries: int = 8, delay_sec: float = 0.25) -> None:
    """Finalize a run directory with retries for transient Windows/OneDrive locks."""
    if dst.exists():
        raise FileExistsError(f"destination already exists: {dst}")
    last_exc: Optional[Exception] = None
    for _ in range(retries):
        try:
            os.replace(src, dst)
            return
        except PermissionError as exc:
            last_exc = exc
            time.sleep(delay_sec)
    try:
        shutil.move(str(src), str(dst))
        return
    except Exception as exc:
        if last_exc is not None:
            raise PermissionError(f"failed to finalize run directory {src} -> {dst}: {last_exc}") from exc
        raise


def _group_direction_counts(
    group: Tuple[RegionId, RegionId],
    requests_group: List[Dict[str, Any]],
) -> Tuple[int, int]:
    if group[0] == group[1]:
        return (len(requests_group), 0)
    forward = sum(1 for req in requests_group if req["o_region"] == group[0] and req["d_region"] == group[1])
    reverse = sum(1 for req in requests_group if req["o_region"] == group[1] and req["d_region"] == group[0])
    return (forward, reverse)


def _build_requests_by_solver_group(
    req_rows: List[Dict[str, Any]],
) -> Dict[Tuple[RegionId, RegionId], List[Dict[str, Any]]]:
    requests_by_group: Dict[Tuple[RegionId, RegionId], List[Dict[str, Any]]] = {group: [] for group in SOLVER_GROUP_LIST}
    for req in req_rows:
        group = canonical_solver_group(req["o_region"], req["d_region"])
        requests_by_group[group].append(req)
    return requests_by_group


def load_stage0_requests(stage0_requests_csv: Path) -> List[Dict[str, Any]]:
    """Stage0 requests CSV를 읽어 dict list로 반환."""
    reqs: List[Dict[str, Any]] = []
    with open(stage0_requests_csv, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Stage0 파일은 기본 필드만 포함(결과 필드는 없음)
            reqs.append(
                {
                    "req_id": int(row["req_id"]),
                    "agent_id": int(row["agent_id"]),
                    "day": int(row["day"]),
                    "plan_index": int(row["plan_index"]),
                    "trip_leg_id": int(row["trip_leg_id"]),
                    "call_time_min": float(row["call_time_min"]),
                    "pickup_lat": float(row["pickup_lat"]),
                    "pickup_lon": float(row["pickup_lon"]),
                    "drop_lat": float(row["drop_lat"]),
                    "drop_lon": float(row["drop_lon"]),
                    "o_region": _parse_region(row["o_region"]),
                    "d_region": _parse_region(row["d_region"]),
                    "hour": int(row["hour"]),
                    "tt_pt_min": float(row["tt_pt_min"]),
                    "latest_min": float(row["latest_min"]),
                }
            )
    return reqs


def enrich_requests_with_choice_deadlines(
    reqs: List[Dict[str, Any]],
    output_stub: Dict[str, Any],
    *,
    car_time_ratio_cap: float = DEFAULT_CAR_TIME_RATIO_CAP,
) -> Dict[str, int]:
    """Attach exact Stage2 deadlines in memory without changing Stage0 CSV.

    The immutable Stage0 request file remains byte-for-byte identical for A/B
    experiments; metadata comes from the matching OMOSIM stub trip.
    """
    req_by_key = {
        (
            int(r["agent_id"]),
            int(r["day"]),
            int(r["plan_index"]),
            int(r["trip_leg_id"]),
        ): r
        for r in reqs
    }
    matched = car_cap = fallback_pt = always_ineligible = 0
    for agent in output_stub.get("agents", []):
        try:
            agent_id = int(agent.get("id"))
        except (TypeError, ValueError):
            continue
        mobility_group = agent.get("mobilityGroup")
        for demand in agent.get("mobilityDemand") or []:
            try:
                day = int(demand.get("day"))
            except (TypeError, ValueError):
                continue
            for plan_index, trip in enumerate(demand.get("plan") or []):
                if trip.get("type") != "Trip":
                    continue
                try:
                    leg_id = int(trip.get("legID"))
                except (TypeError, ValueError):
                    continue
                req = req_by_key.get((agent_id, day, plan_index, leg_id))
                if req is None:
                    continue
                matched += 1
                original_mode = normalize_mode(trip.get("mode"))
                original_car_time = None
                if car_time_cap_applies(
                    original_mode=original_mode, mobility_group=mobility_group
                ):
                    alt_car = get_alternative(trip, original_mode or "")
                    if alt_car is not None:
                        original_car_time = alt_car.get("timeMinute")
                        car_cap += 1
                deadline = effective_drt_deadline_min(
                    req["call_time_min"],
                    req["tt_pt_min"],
                    original_mode=original_mode,
                    mobility_group=mobility_group,
                    original_car_time_min=original_car_time,
                    car_time_ratio_cap=car_time_ratio_cap,
                )
                if deadline is None:
                    # Stage2 rejects a CAR_USER original-car trip when its
                    # matching car alternative/time is missing.  A finite
                    # already-expired deadline represents that exact outcome
                    # in the optimizer (non-finite values intentionally fall
                    # back to PT inside Req for legacy compatibility).
                    deadline = float(req["call_time_min"]) - 1.0
                    always_ineligible += 1
                req["choice_deadline_min"] = float(deadline)
                req["choice_rule_always_ineligible"] = deadline < float(req["call_time_min"])
                req["original_mode"] = original_mode
                req["mobility_group"] = mobility_group
                req["original_car_time_min"] = original_car_time

    for req in reqs:
        if "choice_deadline_min" not in req:
            req["choice_deadline_min"] = float(req["latest_min"])
            fallback_pt += 1
    return {
        "requests": len(reqs),
        "matched_stub_trips": matched,
        "car_cap_requests": car_cap,
        "pt_deadline_fallbacks": fallback_pt,
        "always_ineligible_requests": always_ineligible,
    }


def _order_ods_for_hour(requests_by_od: Dict[Tuple[RegionId, RegionId], List[Dict[str, Any]]]) -> List[Tuple[RegionId, RegionId]]:
    """AA/BB 같은 intra-OD를 먼저, 그 다음 demand 내림차순."""
    demand = {od: len(requests_by_od.get(od, [])) for od in OD_LIST}

    def key(od):
        o, d = od
        intra = 0 if o == d else 1
        return (intra, -demand[od], o.value, d.value)

    return sorted(OD_LIST, key=key)


def _order_solver_groups_for_hour(
    requests_by_group: Dict[Tuple[RegionId, RegionId], List[Dict[str, Any]]]
) -> List[Tuple[RegionId, RegionId]]:
    demand = {group: len(requests_by_group.get(group, [])) for group in SOLVER_GROUP_LIST}

    def key(group: Tuple[RegionId, RegionId]):
        g1, g2 = group
        intra = 0 if g1 == g2 else 1
        return (intra, -demand[group], g1.value, g2.value)

    return sorted(SOLVER_GROUP_LIST, key=key)


def _portfolio_seed(
    seed_base: int,
    hour: int,
    group: Tuple[RegionId, RegionId],
    replica_index: int,
) -> int:
    """Replica zero preserves the historical seed exactly."""
    return (
        int(seed_base)
        + int(hour) * 1000
        + SOLVER_GROUP_INDEX[group]
        + int(replica_index) * 1_000_003
    )


def _result_quality_key(result: Dict[str, Any], replica_index: int) -> Tuple:
    summary = result["summary"]
    score = summary.get("quality_score") or {}
    if score and bool(summary.get("quality_profile", False)):
        return (
            int(score.get("not_choice_eligible", 10**9)),
            float(score.get("choice_excess_min", float("inf"))),
            int(score.get("unassigned", 10**9)),
            float(score.get("near_deadline_risk_min", float("inf"))),
            float(score.get("passenger_time_min", float("inf"))),
            float(score.get("deadhead_km", float("inf"))),
            float(score.get("total_vmt_km", float("inf"))),
            float(score.get("finish_time_sum_min", float("inf"))),
            int(replica_index),
        )
    return (float(summary.get("best_cost", float("inf"))), int(replica_index))


def _aggregate_result_quality(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Build one comparable summary for the disjoint local group winners."""
    score_fields = (
        "not_choice_eligible",
        "choice_eligible",
        "unassigned",
        "choice_excess_min",
        "near_deadline_risk_min",
        "passenger_time_min",
        "deadhead_km",
        "total_vmt_km",
        "finish_time_sum_min",
    )
    integer_fields = {"not_choice_eligible", "choice_eligible", "unassigned"}
    score: Dict[str, float | int] = {}
    for field in score_fields:
        total = sum(
            float((result.get("summary", {}).get("quality_score") or {}).get(field, 0.0) or 0.0)
            for result in results
        )
        score[field] = int(total) if field in integer_fields else total
    return {
        "summary": {
            "quality_profile": all(
                bool(result.get("summary", {}).get("quality_profile", False))
                for result in results
            ),
            "quality_score": score,
            "best_cost": sum(
                float(result.get("summary", {}).get("best_cost", 0.0) or 0.0)
                for result in results
            ),
            "candidates": sum(
                int(result.get("summary", {}).get("candidates", 0) or 0)
                for result in results
            ),
        },
        "route_plan": _merge_route_plans(results),
    }


def _adaptive_global_rescue_plan(
    initial_result: Dict[str, Any],
    *,
    full_replicas: int,
    compact_polish_requested: bool = False,
    compact_polish_effective: bool = False,
    downstream_pressure: float = 0.0,
    compact_pressure_threshold: float = 3.0,
) -> Dict[str, Any]:
    """Choose a live-state rescue tier without using hour identifiers.

    Hard cases retain the historical full rescue.  Small miss sets begin with
    a targeted wave and must fall back to the full policy if unresolved.  A
    completely eligible state receives progressive secondary-quality polish.
    """
    pressure = max(0.0, float(downstream_pressure))
    threshold = float(compact_pressure_threshold)
    compact_effective = bool(
        compact_polish_requested and compact_polish_effective
    )
    compact_metadata = {
        "compact_polish_requested": bool(compact_polish_requested),
        "compact_polish_effective": compact_effective,
        "downstream_pressure": pressure,
        "compact_pressure_threshold": threshold,
        "max_polish_stages": 0,
        "compact_reason": (
            "active"
            if compact_effective
            else (
                "requested_but_inactive"
                if compact_polish_requested
                else "not_requested"
            )
        ),
    }
    summary = initial_result.get("summary", {})
    score = summary.get("quality_score") or {}
    candidates = int(summary.get("candidates", 0) or 0)
    misses = int(score.get("not_choice_eligible", candidates) or 0)
    unassigned = int(score.get("unassigned", misses) or 0)
    choice_excess = float(score.get("choice_excess_min", 0.0) or 0.0)
    near_deadline_risk = float(score.get("near_deadline_risk_min", 0.0) or 0.0)
    near_deadline_hard = near_deadline_risk > max(4.0, 0.02 * candidates)

    if misses == 0 and unassigned == 0 and choice_excess <= 1e-12:
        compact_low_pressure = compact_effective and pressure < threshold
        compact_reason = (
            "compact_low_downstream_pressure"
            if compact_low_pressure
            else (
                "compact_high_downstream_pressure"
                if compact_effective
                else compact_metadata["compact_reason"]
            )
        )
        return {
            "tier": "progressive_polish",
            "initial_replicas": 1,
            "initial_budget_fraction": 0.20,
            "fallback_required": False,
            "progressive_fractions": [] if compact_low_pressure else [0.35, 0.45],
            "progressive_replicas": (
                []
                if compact_low_pressure
                else [1, max(1, min(2, int(full_replicas)))]
            ),
            "reason": (
                "all_requests_choice_eligible_compact_low_pressure"
                if compact_low_pressure
                else (
                    "all_requests_choice_eligible_compact_high_pressure"
                    if compact_effective
                    else "all_requests_choice_eligible"
                )
            ),
            **compact_metadata,
            "max_polish_stages": 1 if compact_low_pressure else 3,
            "compact_reason": compact_reason,
        }
    if misses <= 4 and not near_deadline_hard:
        return {
            "tier": "targeted_then_full_fallback",
            "initial_replicas": 1,
            "initial_budget_fraction": 0.35,
            "fallback_required": True,
            "progressive_fractions": [],
            "progressive_replicas": [],
            "reason": "small_live_miss_set",
            **compact_metadata,
        }
    return {
        "tier": "full",
        "initial_replicas": max(1, int(full_replicas)),
        "initial_budget_fraction": 1.0,
        "fallback_required": False,
        "progressive_fractions": [],
        "progressive_replicas": [],
        "reason": (
            "near_deadline_pressure" if near_deadline_hard else "hard_live_miss_set"
        ),
        **compact_metadata,
    }


def _quality_key_without_replica(result: Dict[str, Any]) -> Tuple:
    """Return the solver's deterministic lexicographic key without tie-breaking."""
    return _result_quality_key(result, 0)[:-1]


def _quality_state_is_resolved(result: Dict[str, Any]) -> bool:
    """Whether every request is assigned and Stage2-choice eligible."""
    score = result.get("summary", {}).get("quality_score") or {}
    return (
        int(score.get("not_choice_eligible", 0) or 0) == 0
        and int(score.get("unassigned", 0) or 0) == 0
        and float(score.get("choice_excess_min", 0.0) or 0.0) <= 1e-12
    )


def _execute_adaptive_global_rescue(
    initial_result: Dict[str, Any],
    *,
    full_replicas: int,
    total_budget_sec: float,
    run_wave: Any,
    compact_polish_requested: bool = False,
    compact_polish_effective: bool = False,
    downstream_pressure: float = 0.0,
    compact_pressure_threshold: float = 3.0,
    fail_on_error: bool = False,
) -> Dict[str, Any]:
    """Execute the adaptive rescue state machine through ``run_wave``.

    ``run_wave`` receives ``(stage, replicas, budget_sec, initial_routes,
    seed_generation)`` and returns a mapping with ``results`` as
    ``[(replica, result), ...]`` plus optional ``errors`` and timing fields.
    Keeping the state machine independent from the process-pool implementation
    makes the full-fallback and progressive-stop guarantees deterministic and
    cheap to test.

    The small-miss fallback intentionally receives the complete historical
    rescue budget and starts from the original local routes.  Thus, if the
    inexpensive targeted attempt does not resolve the live miss set, the exact
    pre-adaptive search breadth, seeds, warm start, and nominal time budget are
    still exercised.  The targeted attempt is the only possible extra cost.
    """
    plan = _adaptive_global_rescue_plan(
        initial_result,
        full_replicas=max(1, int(full_replicas)),
        compact_polish_requested=compact_polish_requested,
        compact_polish_effective=compact_polish_effective,
        downstream_pressure=downstream_pressure,
        compact_pressure_threshold=compact_pressure_threshold,
    )
    total_budget_sec = max(0.02, float(total_budget_sec))
    original_routes = initial_result.get("route_plan") or {}
    incumbent = initial_result
    accepted_result: Optional[Dict[str, Any]] = None
    accepted_stage: Optional[str] = None
    stages: List[Dict[str, Any]] = []
    fallback_triggered = False

    def invoke(
        stage: str,
        replicas: int,
        budget_fraction: float,
        *,
        routes: Dict[int, List[Tuple[str, int]]],
        seed_generation: int,
        accept_equal: bool,
    ) -> Tuple[bool, bool]:
        nonlocal incumbent, accepted_result, accepted_stage
        before = incumbent
        allocated_budget_sec = max(
            0.02, total_budget_sec * max(0.0, float(budget_fraction))
        )
        try:
            wave = run_wave(
                stage,
                max(1, int(replicas)),
                allocated_budget_sec,
                routes,
                int(seed_generation),
            )
        except Exception as exc:
            if fail_on_error:
                raise
            # A failed targeted wave must still reach its guaranteed full
            # fallback.  Hard/full and polish failures safely retain the
            # incumbent local solution.
            wave = {"results": [], "errors": [exc]}
        results = list(wave.get("results") or [])
        if fail_on_error and (wave.get("errors") or len(results) != max(1, int(replicas))):
            raise RuntimeError("incomplete or failed adaptive rescue wave")
        selected_replica: Optional[int] = None
        candidate: Optional[Dict[str, Any]] = None
        if results:
            selected_replica, candidate = min(
                results,
                key=lambda item: _result_quality_key(item[1], item[0]),
            )

        improved = False
        accepted = False
        if candidate is not None:
            candidate_key = _quality_key_without_replica(candidate)
            before_key = _quality_key_without_replica(before)
            improved = candidate_key < before_key
            accepted = improved or (accept_equal and candidate_key == before_key)
            if accepted:
                incumbent = candidate
                accepted_result = candidate
                accepted_stage = stage

        stages.append(
            {
                "stage": stage,
                "replicas": max(1, int(replicas)),
                "allocated_budget_sec": allocated_budget_sec,
                "used_budget_sec": float(wave.get("wall_runtime_sec", 0.0) or 0.0),
                "actual_wall_runtime_sec": float(wave.get("actual_wall_runtime_sec", wave.get("wall_runtime_sec", 0.0)) or 0.0),
                "solver_runtime_sum_sec": float(
                    wave.get("solver_runtime_sum_sec", 0.0) or 0.0
                ),
                "selected_replica": selected_replica,
                "before": before,
                "candidate": candidate,
                "after": incumbent,
                "improved": improved,
                "accepted": accepted,
                "results": results,
                "errors": list(wave.get("errors") or []),
                "status": "ok" if candidate is not None else "failed",
            }
        )
        return improved, candidate is not None

    tier = str(plan["tier"])
    if tier == "full":
        invoke(
            "full",
            int(plan["initial_replicas"]),
            float(plan["initial_budget_fraction"]),
            routes=original_routes,
            seed_generation=0,
            accept_equal=True,
        )
    elif tier == "targeted_then_full_fallback":
        invoke(
            "targeted",
            int(plan["initial_replicas"]),
            float(plan["initial_budget_fraction"]),
            routes=original_routes,
            seed_generation=0,
            accept_equal=False,
        )
        if not _quality_state_is_resolved(incumbent):
            fallback_triggered = True
            # Restart from the unmodified local portfolio solution so this is
            # the exact legacy full-rescue search, independent of the targeted
            # wave's trajectory.
            invoke(
                "full_fallback",
                max(1, int(full_replicas)),
                1.0,
                routes=original_routes,
                seed_generation=0,
                accept_equal=True,
            )
    elif tier == "progressive_polish":
        fractions = [float(plan["initial_budget_fraction"]), *map(float, plan["progressive_fractions"])]
        replicas = [int(plan["initial_replicas"]), *map(int, plan["progressive_replicas"])]
        for stage_index, (budget_fraction, replica_count) in enumerate(
            zip(fractions, replicas)
        ):
            improved, succeeded = invoke(
                f"polish_{stage_index + 1}",
                replica_count,
                budget_fraction,
                routes=incumbent.get("route_plan") or {},
                seed_generation=stage_index,
                accept_equal=False,
            )
            # Later polish is purchased only by a strict lexicographic gain.
            if not succeeded or not improved:
                break
    else:  # pragma: no cover - defensive guard for future policy extensions.
        raise ValueError(f"unsupported adaptive rescue tier: {tier!r}")

    return {
        "plan": plan,
        "winner": incumbent,
        "accepted_result": accepted_result,
        "accepted_stage": accepted_stage,
        "stages": stages,
        "fallback_triggered": fallback_triggered,
        "used_budget_sec": sum(float(stage["used_budget_sec"]) for stage in stages),
        "solver_runtime_sum_sec": sum(
            float(stage["solver_runtime_sum_sec"]) for stage in stages
        ),
    }


def _merge_route_plans(results: List[Dict[str, Any]]) -> Dict[int, List[Tuple[str, int]]]:
    merged: Dict[int, List[Tuple[str, int]]] = {}
    assigned_requests: set[int] = set()
    for result in results:
        for raw_vid, raw_nodes in (result.get("route_plan") or {}).items():
            vid = int(raw_vid)
            if vid in merged:
                raise ValueError(f"vehicle {vid} appears in multiple solver results")
            nodes = [(str(kind), int(rid)) for kind, rid in raw_nodes]
            request_ids = {rid for _, rid in nodes}
            overlap = assigned_requests.intersection(request_ids)
            if overlap:
                raise ValueError(f"requests duplicated across solver results: {sorted(overlap)}")
            assigned_requests.update(request_ids)
            merged[vid] = nodes
    return merged


def _compute_hour_budget(
    global_start: float,
    global_limit_sec: float,
    hour_idx: int,
    inside_demand_by_hour: Dict[int, int],
) -> float:
    """남은 전체 시간 한도에서, 남은 수요 대비 현재 시간대 수요 비율만큼 hour 예산을 배분한다."""
    elapsed = time.time() - global_start
    remaining = max(0.0, global_limit_sec - elapsed)
    remaining_demand = sum(max(0, int(inside_demand_by_hour.get(h, 0))) for h in range(hour_idx, 24))
    current_demand = max(0, int(inside_demand_by_hour.get(hour_idx, 0)))
    if remaining_demand <= 0 or current_demand <= 0:
        return 0.0
    return remaining * (current_demand / remaining_demand)


def _next_service_hour_pressure(
    *,
    hour: int,
    service_hour_end: int,
    inside_demand_by_hour: Dict[int, int],
    fleet_size: int,
) -> float:
    """Return next-hour demand per vehicle, or zero at service completion."""
    if int(hour) >= int(service_hour_end):
        return 0.0
    next_service_hour = int(hour) + 1
    next_demand = max(0, int(inside_demand_by_hour.get(next_service_hour, 0)))
    return next_demand / max(1, int(fleet_size))


def _od_budgets_for_hour(hour_budget: float, requests_by_od: Dict[Tuple[RegionId, RegionId], List[Dict[str, Any]]]) -> Dict[Tuple[RegionId, RegionId], float]:
    total = sum(len(requests_by_od.get(od, [])) for od in OD_LIST)
    budgets = {}
    if total == 0:
        return {od: 0.0 for od in OD_LIST}
    for od in OD_LIST:
        d = len(requests_by_od.get(od, []))
        if d == 0:
            budgets[od] = 0.0
        else:
            budgets[od] = hour_budget * (d / total)
            # 너무 작은 budget은 trace가 거의 없을 수 있으므로 최소 0.02초 보장
            budgets[od] = max(0.02, budgets[od])
    return budgets

# ──────────────────────────────────────────────────────────────────────
#  Post-solve Cross Exchange
# ──────────────────────────────────────────────────────────────────────

def _cross_exchange_phase(
    vehicles: List["VehicleState"],
    hour: int,
    unassigned_reqs: List[Dict[str, Any]],
    speed_kmph: float,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Post-solve Cross Exchange: solver group 격벽을 넘어 유휴 차량에 미배정 요청을 삽입.

    21개 OD solver가 독립적으로 풀린 후 호출된다.
    - unassigned_reqs: solver에 의해 거절된(drt_time_min == -1) 요청 목록
    - 유휴 차량: finish_time이 이른(= 남은 시간이 많은) 차량

    Returns:
        (veh_updates, req_updates) -- 기존 solver output과 동일한 형식.
        veh_updates의 VMT/time 필드는 **추가분**만 포함 (기존 값에 += 해야 함).
        finish_time/lat/lon은 **절대값** (기존 값을 덮어쓰기).
        req_updates는 배정에 성공한 요청만 포함 (기존 reject record를 대체).
    """
    if not unassigned_reqs:
        return [], []

    set_speed_kmph(speed_kmph)

    hour_end_min = (hour + 1) * 60.0
    horizon = float(HORIZON_MIN)

    # 유휴 차량 식별: 남은 시간이 5분 이상인 차량
    idle_vehicles = []
    for v in vehicles:
        remaining = hour_end_min - v.finish_time_min
        if remaining >= 5.0:
            idle_vehicles.append(v)

    if not idle_vehicles:
        return [], []

    # 유휴 시간이 많은 차량 순으로 정렬 (가장 여유 있는 차량 우선)
    idle_vehicles.sort(key=lambda v: v.finish_time_min)

    # 차량별 현재 상태 스냅샷 (cross exchange 중 갱신됨)
    veh_state: Dict[int, Dict[str, Any]] = {}
    for v in idle_vehicles:
        veh_state[v.veh_id] = {
            "time": v.finish_time_min,
            "lat": v.finish_lat,
            "lon": v.finish_lon,
            "served": [],
            "vmt_total": 0.0,
            "vmt_deadhead": 0.0,
            "vmt_service": 0.0,
            "time_wait": 0.0,
            "time_deadhead": 0.0,
            "time_service": 0.0,
        }

    # 요청을 call_time 순 정렬
    sorted_reqs = sorted(unassigned_reqs, key=lambda r: float(r["call_time_min"]))

    req_updates: List[Dict[str, Any]] = []

    for req in sorted_reqs:
        rid = int(req["req_id"])
        call_t = float(req["call_time_min"])
        latest_t = float(req["latest_min"])
        pu_lat = float(req["pickup_lat"])
        pu_lon = float(req["pickup_lon"])
        do_lat = float(req["drop_lat"])
        do_lon = float(req["drop_lon"])

        best_vid: Optional[int] = None
        best_cost = float("inf")

        MAX_CX_DEADHEAD_MIN = 10.0  # 10분 이내 차량만 배정 (서비스 품질 유지)

        for v in idle_vehicles:
            vs = veh_state[v.veh_id]

            # 차량 현재 위치에서 pickup까지
            _, tt_to_pu = travel(vs["lat"], vs["lon"], pu_lat, pu_lon)

            # ── 거리 제한: pickup까지 10분 초과 시 skip ──
            if tt_to_pu > MAX_CX_DEADHEAD_MIN:
                continue

            arrival_pu = vs["time"] + tt_to_pu
            pu_t = max(arrival_pu, call_t)

            # pickup에서 dropoff까지
            _, tt_pu_do = travel(pu_lat, pu_lon, do_lat, do_lon)
            do_t = pu_t + tt_pu_do

            # Horizon 체크
            if do_t > horizon:
                continue

            # 비용 계산 (ALNS 기본 + deadhead 패널티)
            drt_time = do_t - call_t
            lateness = max(0.0, do_t - latest_t)
            cost = drt_time + 4.0 * lateness + 2.0 * tt_to_pu  # deadhead 패널티: 가까운 차량 우선

            if cost < best_cost:
                best_cost = cost
                best_vid = v.veh_id

        # 삽입 비용이 REJECTION_PENALTY(300)보다 크면 reject 유지
        if best_vid is None or best_cost >= 300.0:
            continue

        # 배정 확정: 최적 차량에 대해 재계산
        vs = veh_state[best_vid]
        dist_to_pu, tt_to_pu = travel(vs["lat"], vs["lon"], pu_lat, pu_lon)
        arrival_pu = vs["time"] + tt_to_pu
        pu_t = max(arrival_pu, call_t)
        dist_pu_do, tt_pu_do = travel(pu_lat, pu_lon, do_lat, do_lon)
        do_t = pu_t + tt_pu_do
        drt_time = do_t - call_t
        lateness = max(0.0, do_t - latest_t)
        wait_time = max(0.0, call_t - arrival_pu)

        # 차량 상태 갱신
        vs["vmt_total"] += dist_to_pu + dist_pu_do
        vs["vmt_deadhead"] += dist_to_pu
        vs["vmt_service"] += dist_pu_do
        vs["time_wait"] += wait_time
        vs["time_deadhead"] += tt_to_pu
        vs["time_service"] += tt_pu_do
        vs["time"] = do_t
        vs["lat"] = do_lat
        vs["lon"] = do_lon
        vs["served"].append(rid)

        req_updates.append({
            "req_id": rid,
            "vehicle_id": best_vid,
            "pickup_time_min": pu_t,
            "dropoff_time_min": do_t,
            "drt_time_min": drt_time,
            "onboard_dist_km": dist_pu_do,
            "lateness_min": lateness,
            "obj_base": drt_time,
            "obj_penalty": 4.0 * lateness,
            "obj_total": drt_time + 4.0 * lateness,
        })

    # veh_updates 생성 (변경된 차량만)
    veh_updates: List[Dict[str, Any]] = []
    for v in idle_vehicles:
        vs = veh_state[v.veh_id]
        if not vs["served"]:
            continue  # 이 차량은 cross exchange에서 아무것도 안 함
        veh_updates.append({
            "veh_id": v.veh_id,
            # 절대값 (덮어쓰기)
            "finish_time_min": vs["time"],
            "finish_lat": vs["lat"],
            "finish_lon": vs["lon"],
            # 추가분 (기존 값에 += 해야 함)
            "vmt_total_km": vs["vmt_total"],
            "vmt_deadhead_km": vs["vmt_deadhead"],
            "vmt_service_km": vs["vmt_service"],
            "time_wait_min": vs["time_wait"],
            "time_deadhead_min": vs["time_deadhead"],
            "time_service_min": vs["time_service"],
            "served_count_delta": len(vs["served"]),
        })

    return veh_updates, req_updates


def _group_budgets_for_hour(
    hour_budget: float,
    requests_by_group: Dict[Tuple[RegionId, RegionId], List[Dict[str, Any]]],
) -> Dict[Tuple[RegionId, RegionId], float]:
    total = sum(len(requests_by_group.get(group, [])) for group in SOLVER_GROUP_LIST)
    budgets: Dict[Tuple[RegionId, RegionId], float] = {}
    if total == 0:
        return {group: 0.0 for group in SOLVER_GROUP_LIST}
    for group in SOLVER_GROUP_LIST:
        demand = len(requests_by_group.get(group, []))
        if demand == 0:
            budgets[group] = 0.0
        else:
            budgets[group] = max(0.02, hour_budget * (demand / total))
    return budgets


def _write_drt_alt(output: Dict[str, Any], req_update: Dict[str, Any], req_meta: Dict[str, Any]) -> None:
    """output.json의 해당 Trip.alternativeModes['DRT']에 time/distance를 기록."""
    agent_id = req_meta["agent_id"]
    day = req_meta["day"]
    plan_index = req_meta["plan_index"]
    leg_id = req_meta["trip_leg_id"]

    # agent 찾기
    agent = None
    for a in output.get("agents", []):
        if int(a.get("id")) == agent_id:
            agent = a
            break
    if agent is None:
        raise KeyError(f"agent_id not found: {agent_id}")

    dayPlan = None
    for md in agent.get("mobilityDemand", []):
        if int(md.get("day")) == day:
            dayPlan = md
            break
    if dayPlan is None:
        raise KeyError(f"dayPlan not found: agent={agent_id}, day={day}")

    plan = dayPlan.get("plan", [])
    if plan_index < 0 or plan_index >= len(plan):
        raise IndexError(f"plan_index out of range: {plan_index}")

    trip = plan[plan_index]
    if int(trip.get("legID")) != leg_id:
        raise ValueError(f"legID mismatch for TripKey: expected={leg_id}, got={trip.get('legID')}")

    # DRT altmode 찾기
    alts = trip.get("alternativeModes", [])
    for alt in alts:
        if alt.get("mode") == "DRT":
            alt["timeMinute"] = float(req_update["drt_time_min"])
            alt["distanceKilometer"] = float(req_update["onboard_dist_km"])
            alt["selected"] = False
            return
    raise ValueError("DRT alternativeModes missing (Stage0 stub가 선행되어야 함)")



def _write_run_parameters_md(path: Path, config: Dict[str, Any]) -> None:
    lines = [
        "# Simulation Run Parameters",
        "",
        "| key | value |",
        "|---|---|",
    ]
    for k in sorted(config.keys()):
        lines.append(f"| `{k}` | `{config[k]}` |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class SimulationSubproblemError(RuntimeError):
    def __init__(self, message: str, diagnostics: Dict[str, Any]):
        super().__init__(message)
        self.diagnostics = diagnostics


class SimulationRunFailed(RuntimeError):
    def __init__(self, message: str, failed_run_dir: Path, failure_report_path: Path):
        super().__init__(message)
        self.failed_run_dir = Path(failed_run_dir)
        self.failure_report_path = Path(failure_report_path)


def _safe_stats(values: List[float]) -> Dict[str, Optional[float]]:
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return {"min": None, "avg": None, "max": None}
    return {
        "min": min(vals),
        "avg": sum(vals) / len(vals),
        "max": max(vals),
    }


def _estimate_append_initializer(
    requests_od: List[Dict[str, Any]],
    vehicles_od: List[Dict[str, Any]],
) -> Dict[str, Any]:
    if not requests_od or not vehicles_od:
        return {
            "estimated_request_count": len(requests_od),
            "estimated_vehicle_count": len(vehicles_od),
            "projected_finish_time_min_stats": {"min": None, "avg": None, "max": None},
            "projected_deadhead_to_pickup_min_stats": {"min": None, "avg": None, "max": None},
            "projected_direct_trip_min_stats": {"min": None, "avg": None, "max": None},
            "vehicles_over_horizon_count": 0,
            "vehicles_over_horizon_sample": [],
            "requests_per_vehicle_top": [],
        }

    state_by_vid: Dict[int, Dict[str, float]] = {}
    served_by_vid: Dict[int, int] = {}
    deadhead_times: List[float] = []
    direct_trip_times: List[float] = []

    for v in vehicles_od:
        vid = int(v["veh_id"])
        state_by_vid[vid] = {
            "time_min": float(v["start_time_min"]),
            "lat": float(v["start_lat"]),
            "lon": float(v["start_lon"]),
        }
        served_by_vid[vid] = 0

    reqs_sorted = sorted(requests_od, key=lambda r: float(r["call_time_min"]))
    for req in reqs_sorted:
        best_vid: Optional[int] = None
        best_finish = float("inf")
        best_tt_to_pu = 0.0
        best_tt_pu_do = 0.0
        best_drop_lat = float(req["drop_lat"])
        best_drop_lon = float(req["drop_lon"])

        for v in vehicles_od:
            vid = int(v["veh_id"])
            st = state_by_vid[vid]
            _, tt_to_pu = travel(st["lat"], st["lon"], float(req["pickup_lat"]), float(req["pickup_lon"]))
            arrival = st["time_min"] + tt_to_pu
            pu_t = max(arrival, float(req["call_time_min"]))
            _, tt_pu_do = travel(float(req["pickup_lat"]), float(req["pickup_lon"]), best_drop_lat, best_drop_lon)
            do_t = pu_t + tt_pu_do
            if do_t < best_finish:
                best_finish = do_t
                best_vid = vid
                best_tt_to_pu = tt_to_pu
                best_tt_pu_do = tt_pu_do

        if best_vid is None:
            continue

        deadhead_times.append(best_tt_to_pu)
        direct_trip_times.append(best_tt_pu_do)
        served_by_vid[best_vid] += 1
        state_by_vid[best_vid] = {
            "time_min": best_finish,
            "lat": best_drop_lat,
            "lon": best_drop_lon,
        }

    finish_rows = []
    for vid, st in state_by_vid.items():
        overflow = float(st["time_min"]) - float(HORIZON_MIN)
        if overflow > 1e-9:
            finish_rows.append(
                {
                    "veh_id": vid,
                    "projected_finish_time_min": float(st["time_min"]),
                    "overflow_min": overflow,
                    "assigned_requests": served_by_vid.get(vid, 0),
                }
            )

    finish_rows.sort(key=lambda row: (-row["overflow_min"], -row["assigned_requests"], row["veh_id"]))
    served_rows = [
        {"veh_id": vid, "assigned_requests": cnt}
        for vid, cnt in served_by_vid.items()
        if cnt > 0
    ]
    served_rows.sort(key=lambda row: (-row["assigned_requests"], row["veh_id"]))

    return {
        "estimated_request_count": len(requests_od),
        "estimated_vehicle_count": len(vehicles_od),
        "projected_finish_time_min_stats": _safe_stats([st["time_min"] for st in state_by_vid.values()]),
        "projected_deadhead_to_pickup_min_stats": _safe_stats(deadhead_times),
        "projected_direct_trip_min_stats": _safe_stats(direct_trip_times),
        "vehicles_over_horizon_count": len(finish_rows),
        "vehicles_over_horizon_sample": finish_rows[:5],
        "requests_per_vehicle_top": served_rows[:10],
    }


def _build_subproblem_diagnostics(
    *,
    hour: int,
    od: Tuple[RegionId, RegionId],
    requests_od: List[Dict[str, Any]],
    vehicles_od: List[Dict[str, Any]],
    vehicles_target: int,
    time_budget_sec: float,
    speed_kmph: float,
    init_strategy: str,
    exc: Exception,
) -> Dict[str, Any]:
    direct_times = [
        travel(float(r["pickup_lat"]), float(r["pickup_lon"]), float(r["drop_lat"]), float(r["drop_lon"]))[1]
        for r in requests_od
    ]
    latest_slacks = [float(r["latest_min"]) - float(r["call_time_min"]) for r in requests_od]
    start_times = [float(v["start_time_min"]) for v in vehicles_od]
    remaining_horizon = [float(HORIZON_MIN) - float(v["start_time_min"]) for v in vehicles_od]
    group_mode = solver_group_mode(od)
    request_origin_regions = {r["o_region"] for r in requests_od}
    group_endpoint_regions = {od[0], od[1]}
    origin_region_vehicle_count = sum(
        1
        for v in vehicles_od
        if map_to_region(float(v["start_lat"]), float(v["start_lon"])) in request_origin_regions
    )
    group_endpoint_vehicle_count = sum(
        1
        for v in vehicles_od
        if map_to_region(float(v["start_lat"]), float(v["start_lon"])) in group_endpoint_regions
    )
    forward_candidates, reverse_candidates = _group_direction_counts(od, requests_od)
    append_est = _estimate_append_initializer(requests_od, vehicles_od)

    worker_message = str(exc)
    if "초기해가 infeasible" in worker_message and append_est["vehicles_over_horizon_count"] > 0:
        likely_cause = "horizon_overflow_in_initial_solution"
        likely_detail = (
            f"append-only 초기해 추정에서 horizon({HORIZON_MIN} min)을 넘는 차량이 "
            f"{append_est['vehicles_over_horizon_count']}대 발생"
        )
    elif "삽입 실패" in worker_message:
        likely_cause = "no_feasible_insertion_found"
        likely_detail = "초기해 또는 복구 단계에서 모든 요청을 제약 내에 삽입하지 못함"
    else:
        likely_cause = "solver_infeasible_or_runtime_error"
        likely_detail = "worker 예외 메시지를 직접 확인 필요"

    return {
        "hour": hour,
        "o_region": od[0].value,
        "d_region": od[1].value,
        "group_mode": group_mode,
        "group_label": solver_group_label(od),
        "candidates": len(requests_od),
        "forward_candidates": int(forward_candidates),
        "reverse_candidates": int(reverse_candidates),
        "vehicles_target": int(vehicles_target),
        "vehicles_assigned": len(vehicles_od),
        "vehicles_in_origin_region": origin_region_vehicle_count,
        "vehicles_in_group_endpoint_regions": group_endpoint_vehicle_count,
        "vehicles_outside_origin_region": len(vehicles_od) - origin_region_vehicle_count,
        "time_budget_sec": float(time_budget_sec),
        "speed_kmph": float(speed_kmph),
        "init_strategy": str(init_strategy),
        "worker_exception_type": type(exc).__name__,
        "worker_exception_message": worker_message,
        "likely_cause_code": likely_cause,
        "likely_cause_detail": likely_detail,
        "request_call_time_min_stats": _safe_stats([float(r["call_time_min"]) for r in requests_od]),
        "request_latest_min_stats": _safe_stats([float(r["latest_min"]) for r in requests_od]),
        "request_latest_slack_min_stats": _safe_stats(latest_slacks),
        "request_direct_travel_min_stats": _safe_stats(direct_times),
        "vehicle_start_time_min_stats": _safe_stats(start_times),
        "vehicle_remaining_horizon_min_stats": _safe_stats(remaining_horizon),
        "request_id_sample": [int(r["req_id"]) for r in requests_od[:10]],
        "vehicle_id_sample": [int(v["veh_id"]) for v in vehicles_od[:10]],
        "append_initializer_estimate": append_est,
        "notes": [
            "현재 solver에서 latest_min은 hard feasibility 제약이 아니라 objective penalty로 처리됨",
            "따라서 여기서의 infeasible은 주로 horizon/capacity/삽입 불가와 관련됨",
        ],
    }


def _make_failed_run_path(runs_base_dir: Path, start_stamp: str) -> Path:
    base = runs_base_dir / f"run_{start_stamp}__FAILED"
    if not base.exists():
        return base
    seq = 2
    while True:
        cand = runs_base_dir / f"run_{start_stamp}__FAILED_{seq:02d}"
        if not cand.exists():
            return cand
        seq += 1

def _run_simulation_from_stage0_legacy(
    stage0_dir: Path,
    runs_base_dir: Path,
    workers: int = 18,
    global_time_limit_sec: float = 10800.0,
    fleet_size: int = 300,
    cap: int = 15,
    alns_iter_cap: int = 12000,
    init_strategy: str = "fast",
    init_multistart: int = 8,
    init_regret_k: int = 3,
    init_vehicle_top_k: int = 12,
    init_pos_samples: int = 160,
    init_candidate_pool: int = 24,
    init_time_limit_sec: float = 0.0,
    speed_kmph: float = 45.0,
    run_invocation: Optional[str] = None,
) -> Path:
    """Stage0 산출물을 입력으로 받아 시뮬레이션을 수행하고 run 폴더를 반환."""

    stage0_dir = Path(stage0_dir)
    set_speed_kmph(speed_kmph)
    stage0_requests_csv = stage0_dir / "stage0_requests.csv"
    stub_output_path = stage0_dir / "output_with_drt_stub.json"

    if not stage0_requests_csv.exists():
        raise FileNotFoundError(f"stage0_requests.csv not found: {stage0_requests_csv}")
    if not stub_output_path.exists():
        raise FileNotFoundError(f"output_with_drt_stub.json not found: {stub_output_path}")

    # run 폴더 준비
    run_wall_start = time.time()
    start_stamp = now_stamp()
    run_tmp = runs_base_dir / f"run_{start_stamp}__RUNNING"
    metrics_dir = run_tmp / "metrics"
    kpi_dir = run_tmp / "kpi"
    tmp_dir = run_tmp / "tmp"
    for d in [metrics_dir, kpi_dir, tmp_dir]:
        d.mkdir(parents=True, exist_ok=True)

    # output base 로드 (이미 DRT altmode가 모든 Trip에 들어간 stub)
    output = read_json(stub_output_path)

    # Stage0 requests 로드
    reqs_all = load_stage0_requests(stage0_requests_csv)
    req_meta_by_id = {int(r["req_id"]): r for r in reqs_all}

    # 결과 누적: requests.csv 작성을 위해 req_id -> 결과 dict 저장
    req_result_by_id: Dict[int, Dict[str, Any]] = {}
    # 차량별 서빙 개수(요청 수 기반)
    vehicle_served_count: Dict[int, int] = {}

    # Fleet 초기화
    # reqs_all은 dict이므로, init_fleet_from_requests가 기대하는 속성 인터페이스를 맞추기 위해 wrapper 사용
    class _R:
        def __init__(self, d):
            self.o_region = d["o_region"]
            self.d_region = d["d_region"]

    vehicles = init_fleet_from_requests([_R(r) for r in reqs_all], fleet_size=fleet_size, cap=cap)

    # KPI 파일 헤더
    write_csv_header(metrics_dir / "alns_trace.csv", ALNS_TRACE_HEADER)
    write_csv_header(metrics_dir / "hour_kpi.csv", HOUR_KPI_HEADER)
    write_csv_header(metrics_dir / "od_kpi_hourly.csv", OD_KPI_HEADER)
    write_csv_header(kpi_dir / "requests.csv", REQUESTS_HEADER)
    write_csv_header(kpi_dir / "vehicles.csv", VEHICLES_HEADER)
    write_csv_header(kpi_dir / "vehicle_events.csv", VEHICLE_EVENTS_HEADER)

    # stage0_inputs도 복사해 두기(실험 고립)
    append_file(run_tmp / "stage0_requests.csv", stage0_requests_csv)

    # hour별 요청 버킷
    reqs_by_hour: Dict[int, List[Dict[str, Any]]] = {h: [] for h in range(24)}
    for r in reqs_all:
        reqs_by_hour[int(r["hour"])].append(r)
    inside_demand_by_hour: Dict[int, int] = {h: 0 for h in range(24)}
    for h in range(24):
        inside_demand_by_hour[h] = sum(
            1
            for r in reqs_by_hour[h]
            if r["o_region"] != RegionId.OUTSIDE and r["d_region"] != RegionId.OUTSIDE
        )

    global_start = time.time()

    # hour 순차
    for hour in range(24):
        # 글로벌 타임리밋 체크
        if (time.time() - global_start) > global_time_limit_sec:
            raise TimeoutError("Global time limit exceeded")

        hour_start_clock = time.time()

        # OD별 요청 구성(OUTSIDE는 라우팅 제외)
        requests_by_od: Dict[Tuple[RegionId, RegionId], List[Dict[str, Any]]] = {od: [] for od in OD_LIST}
        outside_excluded = 0
        for r in reqs_by_hour[hour]:
            if r["o_region"] == RegionId.OUTSIDE or r["d_region"] == RegionId.OUTSIDE:
                outside_excluded += 1
                continue
            requests_by_od[(r["o_region"], r["d_region"])].append(r)

        # carry-over: start = 이전 finish
        for v in vehicles:
            v.start_time_min = v.finish_time_min
            v.start_lat = v.finish_lat
            v.start_lon = v.finish_lon

        # OD targets(F=300)
        od_targets = compute_od_targets(requests_by_od, fleet_size=fleet_size)
        assignments = assign_vehicles_to_ods(vehicles, od_targets, requests_by_od)

        # hour budget + od budgets
        hour_budget = _compute_hour_budget(
            global_start=global_start,
            global_limit_sec=global_time_limit_sec,
            hour_idx=hour,
            inside_demand_by_hour=inside_demand_by_hour,
        )
        od_budget = _od_budgets_for_hour(hour_budget, requests_by_od)

        # od 실행 순서 및 waves
        od_order = _order_ods_for_hour(requests_by_od)
        wave1 = od_order[:18]
        wave2 = od_order[18:]

        # helper to run one wave
        def run_wave(od_list: List[Tuple[RegionId, RegionId]]) -> List[Dict[str, Any]]:
            results = []
            with ProcessPoolExecutor(max_workers=workers) as ex:
                futs = []
                for od in od_list:
                    req_list = requests_by_od.get(od, [])
                    veh_ids = assignments.get(od, [])
                    if len(req_list) > 0 and len(veh_ids) == 0:
                        raise RuntimeError(f"OD {od} has demand but no vehicles assigned")
                    if len(veh_ids) == 0:
                        # 수요 0이면서 배정도 0인 경우: worker를 돌릴 필요는 없음
                        continue
                    veh_list = [
                        {
                            "veh_id": vehicles[vid].veh_id,
                            "cap": vehicles[vid].cap,
                            "start_time_min": vehicles[vid].start_time_min,
                            "start_lat": vehicles[vid].start_lat,
                            "start_lon": vehicles[vid].start_lon,
                        }
                        for vid in veh_ids
                    ]
                    futs.append(
                        ex.submit(
                            solve_od_alns_sa,
                            hour,
                            od,
                            req_list,
                            veh_list,
                            float(od_budget.get(od, 0.0)),
                            seed_base + hour * 1000 + OD_INDEX[od],
                            str(tmp_dir),
                            init_strategy,
                            init_multistart,
                            init_regret_k,
                            init_vehicle_top_k,
                            init_pos_samples,
                            init_candidate_pool,
                            init_time_limit_sec,
                            alns_iter_cap,
                            speed_kmph,
                        )
                    )

                for fut in as_completed(futs):
                    results.append(fut.result())
            return results

        # Wave1, Wave2
        results_all = []
        results_all.extend(run_wave(wave1))
        results_all.extend(run_wave(wave2))

        ods_ran = set()

        # hour KPI accum
        hour_obj_total = 0.0
        hour_base = 0.0
        hour_pen = 0.0
        hour_vmt_total = 0.0
        hour_vmt_dead = 0.0
        hour_vmt_serv = 0.0
        hour_served = 0

        # 결과 merge
        for res in results_all:
            summ = res["summary"]
            od = (_parse_region(str(summ["o_region"])), _parse_region(str(summ["d_region"])))
            ods_ran.add(od)

            # hour objective 누적(해당 OD의 best_cost를 hour 총합으로 사용)
            hour_obj_total += float(summ.get("best_cost", 0.0))

            # od_kpi row
            append_csv_row(
                metrics_dir / "od_kpi_hourly.csv",
                [
                    summ["hour"],
                    summ["o_region"],
                    summ["d_region"],
                    summ["candidates"],
                    summ["served"],
                    od_targets.get(od, 0),
                    len(assignments.get(od, [])),
                    summ["iters"],
                    summ["time_budget_sec"],
                    summ["runtime_sec"],
                    summ["initial_cost"],
                    summ["final_cost"],
                    summ["best_cost"],
                    summ["accept_rate"],
                ],
            )

            # trace merge (skip header)
            append_file(metrics_dir / "alns_trace.csv", res["trace_path"], skip_header=True)
            # events merge (skip header)
            append_file(kpi_dir / "vehicle_events.csv", res["events_path"], skip_header=True)

            # vehicle update
            for vu in res["veh_updates"]:
                vid = int(vu["veh_id"])
                v = vehicles[vid]
                v.finish_time_min = float(vu["finish_time_min"])
                v.finish_lat = float(vu["finish_lat"])
                v.finish_lon = float(vu["finish_lon"])
                v.vmt_total_km += float(vu["vmt_total_km"])
                v.vmt_deadhead_km += float(vu["vmt_deadhead_km"])
                v.vmt_service_km += float(vu["vmt_service_km"])
                v.time_wait_min += float(vu["time_wait_min"])
                v.time_deadhead_min += float(vu["time_deadhead_min"])
                v.time_service_min += float(vu["time_service_min"])

                hour_vmt_total += float(vu["vmt_total_km"])
                hour_vmt_dead += float(vu["vmt_deadhead_km"])
                hour_vmt_serv += float(vu["vmt_service_km"])

            # request update + write-back
            for ru in res["req_updates"]:
                rid = int(ru["req_id"])
                req_meta = req_meta_by_id[rid]
                _write_drt_alt(output, ru, req_meta)

                # 결과 누적(요청별 KPI 완전 채움)
                req_result_by_id[rid] = ru
                vid = int(ru["vehicle_id"])
                vehicle_served_count[vid] = vehicle_served_count.get(vid, 0) + 1

                # 목적함수 분해 누적
                hour_base += float(ru.get("obj_base", 0.0))
                hour_pen += float(ru.get("obj_penalty", 0.0))

                hour_served += 1

        # 결과가 없었던 OD(수요 0 또는 배정 0)는 trace/od_kpi에 최소 기록을 남긴다.
        for od in OD_LIST:
            if od in ods_ran:
                continue
            # od_kpi: candidates=0, served=0
            append_csv_row(
                metrics_dir / "od_kpi_hourly.csv",
                [hour, od[0].value, od[1].value, 0, 0, od_targets.get(od, 0), len(assignments.get(od, [])), 0, float(od_budget.get(od, 0.0)), 0.0, 0.0, 0.0, 0.0, 0.0],
            )
            # trace: iter=1 한 줄 (1-based)
            append_csv_row(
                metrics_dir / "alns_trace.csv",
                [hour, od[0].value, od[1].value, 1, 0.0, 0.0, 0.0, 0.0, 0.0, 0, 0, "NONE", "NONE", 0],
            )

        # hour KPI row
        runtime_sec = time.time() - hour_start_clock
        append_csv_row(
            metrics_dir / "hour_kpi.csv",
            [
                hour,
                len(reqs_by_hour[hour]),
                outside_excluded,
                hour_served,
                hour_obj_total,
                hour_base,
                hour_pen,
                hour_vmt_total,
                hour_vmt_dead,
                hour_vmt_serv,
                runtime_sec,
            ],
        )

    # requests.csv 작성: Stage0 meta + ALNS 결과(req_result_by_id)를 합쳐서 '승객별 KPI'를 완전하게 기록
    for r in reqs_all:
        rid = int(r["req_id"])
        ru = req_result_by_id.get(rid)

        if ru is None:
            # OUTSIDE 제외 또는 (이론상) 서빙 실패
            vehicle_id = ""
            pickup_time_min = ""
            dropoff_time_min = ""
            drt_time_min = -1
            onboard_dist_km = None
            lateness_min = ""
            obj_base = ""
            obj_penalty = ""
            obj_total = ""
        else:
            vehicle_id = int(ru["vehicle_id"])
            pickup_time_min = float(ru["pickup_time_min"])
            dropoff_time_min = float(ru["dropoff_time_min"])
            drt_time_min = float(ru["drt_time_min"])
            onboard_dist_km = float(ru["onboard_dist_km"])
            lateness_min = float(ru["lateness_min"])
            obj_base = float(ru["obj_base"])
            obj_penalty = float(ru["obj_penalty"])
            obj_total = float(ru["obj_total"])

        append_csv_row(
            kpi_dir / "requests.csv",
            [
                rid,
                r["agent_id"],
                r["day"],
                r["plan_index"],
                r["trip_leg_id"],
                r["call_time_min"],
                r["pickup_lat"],
                r["pickup_lon"],
                r["drop_lat"],
                r["drop_lon"],
                r["o_region"].value,
                r["d_region"].value,
                r["hour"],
                r["tt_pt_min"],
                r["latest_min"],
                vehicle_id,
                pickup_time_min,
                dropoff_time_min,
                drt_time_min,
                onboard_dist_km,
                lateness_min,
                obj_base,
                obj_penalty,
                obj_total,
            ],
        )

    # vehicles.csv 작성
    for v in vehicles:
        v.served_count = vehicle_served_count.get(v.veh_id, 0)
        append_csv_row(
            kpi_dir / "vehicles.csv",
            [
                v.veh_id,
                v.cap,
                v.start_time_min,
                v.start_lat,
                v.start_lon,
                v.finish_time_min,
                v.finish_lat,
                v.finish_lon,
                v.vmt_total_km,
                v.vmt_deadhead_km,
                v.vmt_service_km,
                v.time_wait_min,
                v.time_deadhead_min,
                v.time_service_min,
                v.served_count,
            ],
        )

    # output_withDRT.json 저장
    write_json_atomic(run_tmp / "output_withDRT.json", output, indent=None)

    # 실행 config 저장(재현성)
    end_stamp = now_stamp()
    wall_clock_runtime_sec = time.time() - run_wall_start

    run_config = {
        "stage0_dir": str(stage0_dir),
        "stage0_requests_csv": str(stage0_requests_csv),
        "workers": workers,
        "global_time_limit_sec": global_time_limit_sec,
        "fleet_size": fleet_size,
        "vehicle_capacity": cap,
        "alns_iter_cap": alns_iter_cap,
        "init_strategy": init_strategy,
        "init_multistart": init_multistart,
        "init_regret_k": init_regret_k,
        "init_vehicle_top_k": init_vehicle_top_k,
        "init_pos_samples": init_pos_samples,
        "init_candidate_pool": init_candidate_pool,
        "init_time_limit_sec": init_time_limit_sec,
        "hour_budget_policy": "proportional_to_remaining_inside_demand",
        "od_budget_min_sec": 0.02,
        "horizon_min": 1800,
        "speed_kmph": speed_kmph,
        "seed_base": seed_base,
        "run_start_stamp": start_stamp,
        "run_end_stamp": end_stamp,
        "wall_clock_runtime_sec": wall_clock_runtime_sec,
        "run_invocation": run_invocation or "",
    }
    write_json_atomic(metrics_dir / "run_config.json", run_config, indent=2)
    _write_run_parameters_md(run_tmp / "run_parameters.md", run_config)

    run_path = runs_base_dir / f"run_{start_stamp}__{end_stamp}"
    if run_path.exists():
        raise FileExistsError(f"run_path already exists: {run_path}")
    os.replace(run_tmp, run_path)

    return run_path


def run_simulation_from_stage0(
    stage0_dir: Path,
    runs_base_dir: Path,
    workers: int = 18,
    global_time_limit_sec: float = 10800.0,
    fleet_size: int = 300,
    cap: int = 15,
    alns_iter_cap: int = 12000,
    init_strategy: str = "fast",
    init_multistart: int = 8,
    init_regret_k: int = 3,
    init_vehicle_top_k: int = 12,
    init_pos_samples: int = 160,
    init_candidate_pool: int = 24,
    init_time_limit_sec: float = 0.0,
    speed_kmph: float = 45.0,
    rejection_penalty: float = 300.0,
    run_invocation: Optional[str] = None,
    service_hour_start: int = 0,
    service_hour_end: int = 23,
    rebalancing_strategy: str = "default",
    seed_base: int = 42,
    alns_selective_route_reevaluation: bool = True,
    alns_lightweight_eval_only: bool = True,
    alns_true_regret2: bool = True,
    sa_cooling_rate: float = 0.998,
    sa_initial_acceptance_probability: float = 0.36787944117144233,
    sa_stagnation_patience_iters: int = 4000,
    sa_stagnation_min_iters: int = 1000,
    quality_stagnation_stop: bool = False,
    sa_accept_rate_window_iters: int = 200,
    sa_high_accept_rate_threshold: float = 0.90,
    car_time_ratio_cap: float = DEFAULT_CAR_TIME_RATIO_CAP,
    alns_quality_profile: bool = False,
    quality_choice_miss_penalty: float = 10000.0,
    quality_choice_excess_weight: float = 25.0,
    quality_near_deadline_buffer_min: float = 1.0,
    quality_regret_k: int = 3,
    quality_unassigned_repair_limit: int = 16,
    quality_reheat_interval_iters: int = 500,
    quality_max_reheats: int = 4,
    quality_choice_loss_acceptance_cap: float = 0.02,
    alns_noop_operator_adaptation: bool = False,
    alns_t0_samples: int = 0,
    global_rescue_t0_samples: int = 0,
    fleet_target_strategy: str = "demand",
    portfolio_replicas: int = 1,
    global_rescue: bool = False,
    global_rescue_replicas: int = 1,
    global_rescue_time_fraction: float = 0.15,
    adaptive_global_rescue: bool = False,
    adaptive_global_rescue_compact_polish: bool = False,
    adaptive_global_rescue_compact_pressure_threshold: float = 3.0,
    od_cache_path: Optional[str] = None,
    od_cache_strict: bool = False,
    solver_travel_cache: Optional[bool] = None,
    solver_travel_cache_max_entries: int = 524_288,
    winner_only_materialization: Optional[bool] = None,
    shared_executor_factory: Any = None,
) -> Path:
    """Run one simulation from Stage0 cache and return the finalized run directory.

    Parameters
    ----------
    service_hour_start : int
        First hour of DRT service (inclusive, 0-23). Default 0.
    service_hour_end : int
        Last hour of DRT service (inclusive, 0-23). Default 23.
        Example: service_hour_start=6, service_hour_end=21 means DRT runs 06:00-21:59.
    rebalancing_strategy : str
        'default' = existing proportional assignment (assign_vehicles_to_solver_groups)
        'sticky'  = locality-preserving sticky assignment (assign_vehicles_to_solver_groups_sticky)
        'hungarian' = minimum-cost vehicle-to-group assignment
    """

    stage0_dir = Path(stage0_dir)
    effective_solver_travel_cache = (
        bool(alns_quality_profile)
        if solver_travel_cache is None
        else bool(solver_travel_cache)
    )
    effective_winner_only_materialization = (
        bool(alns_quality_profile)
        if winner_only_materialization is None
        else bool(winner_only_materialization)
    )
    effective_adaptive_global_rescue = bool(
        adaptive_global_rescue and global_rescue and alns_quality_profile
    )
    effective_adaptive_global_rescue_compact_polish = bool(
        adaptive_global_rescue_compact_polish
        and effective_adaptive_global_rescue
    )
    if int(solver_travel_cache_max_entries) < 1:
        raise ValueError("solver_travel_cache_max_entries must be >= 1")
    if int(alns_t0_samples) < 0 or int(global_rescue_t0_samples) < 0:
        raise ValueError("ALNS T0 sample counts must be >= 0")
    effective_global_rescue_t0_samples = (
        int(global_rescue_t0_samples)
        if int(global_rescue_t0_samples) > 0
        else int(alns_t0_samples)
    )
    local_t0_samples_policy = (
        f"fixed:{int(alns_t0_samples)}"
        if int(alns_t0_samples) > 0
        else "auto:min(8,max(2,request_count//10))"
    )
    global_t0_samples_policy = (
        f"fixed:{effective_global_rescue_t0_samples}"
        if effective_global_rescue_t0_samples > 0
        else "auto:min(8,max(2,request_count//10))"
    )
    configure_process_memory_cache(
        effective_solver_travel_cache,
        max_entries=int(solver_travel_cache_max_entries),
    )
    set_speed_kmph(speed_kmph)
    if od_cache_path:
        load_od_cache(str(od_cache_path), strict=bool(od_cache_strict))
    valid_rebalancing_strategies = {"default", "sticky", "hungarian"}
    if rebalancing_strategy not in valid_rebalancing_strategies:
        choices = ", ".join(sorted(valid_rebalancing_strategies))
        raise ValueError(f"rebalancing_strategy must be one of: {choices}")
    if fleet_target_strategy not in {"demand", "workload_deadline"}:
        raise ValueError("fleet_target_strategy must be demand or workload_deadline")
    if int(portfolio_replicas) < 1 or int(global_rescue_replicas) < 1:
        raise ValueError("portfolio replica counts must be >= 1")
    if not 0.0 <= float(global_rescue_time_fraction) < 1.0:
        raise ValueError("global_rescue_time_fraction must be in [0, 1)")
    compact_pressure_threshold = float(
        adaptive_global_rescue_compact_pressure_threshold
    )
    if not 0.0 < compact_pressure_threshold < float("inf"):
        raise ValueError(
            "adaptive_global_rescue_compact_pressure_threshold must be finite and > 0"
        )
    if not (0 <= int(service_hour_start) <= int(service_hour_end) <= 23):
        raise ValueError("service hours must satisfy 0 <= start <= end <= 23")
    stage0_requests_csv = stage0_dir / "stage0_requests.csv"
    stub_output_path = stage0_dir / "output_with_drt_stub.json"

    if not stage0_requests_csv.exists():
        raise FileNotFoundError(f"stage0_requests.csv not found: {stage0_requests_csv}")
    if not stub_output_path.exists():
        raise FileNotFoundError(f"output_with_drt_stub.json not found: {stub_output_path}")

    input_identity = _stage0_input_identity(
        stage0_dir, stage0_requests_csv, stub_output_path
    )
    effective_stagnation_patience_iters = (
        int(sa_stagnation_patience_iters)
        if (not alns_quality_profile or quality_stagnation_stop)
        else 0
    )

    run_wall_start = time.time()
    start_stamp = now_stamp()
    run_tmp = runs_base_dir / f"run_{start_stamp}__RUNNING"
    metrics_dir = run_tmp / "metrics"
    kpi_dir = run_tmp / "kpi"
    tmp_dir = run_tmp / "tmp"
    for d in [metrics_dir, kpi_dir, tmp_dir]:
        d.mkdir(parents=True, exist_ok=True)

    def phase_executor(*, stage, hour, **kwargs):
        if shared_executor_factory is not None:
            return shared_executor_factory(stage=stage, hour=hour, **kwargs)
        return ProcessPoolExecutor(**kwargs)

    run_config_base = {
        "stage0_dir": str(stage0_dir),
        "stage0_requests_csv": str(stage0_requests_csv),
        "workers": workers,
        "reference_workers": workers,
        "budget_mode": "reference_parallel_v1" if shared_executor_factory else "wall_clock",
        "shared_pool": shared_executor_factory is not None,
        "global_time_limit_sec": global_time_limit_sec,
        "fleet_size": fleet_size,
        "vehicle_capacity": cap,
        "alns_iter_cap": alns_iter_cap,
        "init_strategy": init_strategy,
        "init_multistart": init_multistart,
        "init_regret_k": init_regret_k,
        "init_vehicle_top_k": init_vehicle_top_k,
        "init_pos_samples": init_pos_samples,
        "init_candidate_pool": init_candidate_pool,
        "init_time_limit_sec": init_time_limit_sec,
        "hour_budget_policy": "proportional_to_remaining_inside_demand",
        "od_budget_min_sec": 0.02,
        "horizon_min": float(HORIZON_MIN),
        "speed_kmph": speed_kmph,
        "rejection_penalty": float(rejection_penalty),
        "alns_selective_route_reevaluation": bool(alns_selective_route_reevaluation),
        "alns_lightweight_eval_only": bool(alns_lightweight_eval_only),
        "alns_true_regret2": bool(alns_true_regret2),
        "sa_cooling_rate": float(sa_cooling_rate),
        "sa_initial_acceptance_probability": float(sa_initial_acceptance_probability),
        "sa_stagnation_patience_iters": int(sa_stagnation_patience_iters),
        "sa_stagnation_patience_iters_effective": effective_stagnation_patience_iters,
        "sa_stagnation_policy": (
            "configured_conservative_quality"
            if (alns_quality_profile and quality_stagnation_stop)
            else (
                "disabled_for_quality_budget" if alns_quality_profile else "configured"
            )
        ),
        "quality_stagnation_stop": bool(quality_stagnation_stop),
        "sa_stagnation_min_iters": int(sa_stagnation_min_iters),
        "sa_accept_rate_window_iters": int(sa_accept_rate_window_iters),
        "sa_high_accept_rate_threshold": float(sa_high_accept_rate_threshold),
        "car_time_ratio_cap": float(car_time_ratio_cap),
        "alns_quality_profile": bool(alns_quality_profile),
        "quality_objective": "stage2_eligible_lexicographic_v1",
        "quality_choice_miss_penalty": float(quality_choice_miss_penalty),
        "quality_choice_excess_weight": float(quality_choice_excess_weight),
        "quality_near_deadline_buffer_min": float(quality_near_deadline_buffer_min),
        "quality_near_deadline_weight": 3.0,
        "quality_deadhead_weight": 0.15,
        "quality_total_vmt_weight": 0.02,
        "quality_regret_k": int(quality_regret_k),
        "quality_unassigned_repair_limit": int(quality_unassigned_repair_limit),
        "quality_reheat_interval_iters": int(quality_reheat_interval_iters),
        "quality_max_reheats": int(quality_max_reheats),
        "quality_choice_loss_acceptance_cap": float(quality_choice_loss_acceptance_cap),
        "alns_noop_operator_adaptation": bool(alns_noop_operator_adaptation),
        "alns_t0_samples": int(alns_t0_samples),
        "alns_t0_samples_effective_policy": local_t0_samples_policy,
        "global_rescue_t0_samples": int(global_rescue_t0_samples),
        "global_rescue_t0_samples_effective_override": (
            effective_global_rescue_t0_samples or None
        ),
        "global_rescue_t0_samples_effective_policy": global_t0_samples_policy,
        "alns_operator_diagnostics_csv": "metrics/alns_operator_diagnostics.csv",
        "alns_operator_diagnostics_schema_version": 1,
        "fleet_target_strategy": fleet_target_strategy,
        "portfolio_replicas_requested": int(portfolio_replicas),
        "global_rescue": bool(global_rescue),
        "global_rescue_replicas": int(global_rescue_replicas),
        "global_rescue_time_fraction": float(global_rescue_time_fraction),
        "adaptive_global_rescue": bool(adaptive_global_rescue),
        "adaptive_global_rescue_effective": effective_adaptive_global_rescue,
        "adaptive_global_rescue_compact_polish": bool(
            adaptive_global_rescue_compact_polish
        ),
        "adaptive_global_rescue_compact_polish_requested": bool(
            adaptive_global_rescue_compact_polish
        ),
        "adaptive_global_rescue_compact_polish_effective": (
            effective_adaptive_global_rescue_compact_polish
        ),
        "adaptive_global_rescue_compact_pressure_threshold": (
            compact_pressure_threshold
        ),
        "adaptive_global_rescue_compact_low_pressure_max_stages": 1,
        "adaptive_global_rescue_compact_high_pressure_max_stages": 3,
        "adaptive_global_rescue_policy": (
            "live_quality_tiered_targeted_full_fallback_progressive_polish_v1"
        ),
        "adaptive_global_rescue_metrics_csv": "metrics/adaptive_global_rescue.csv",
        "parallel_group_scheduling": "single_pool_portfolio",
        "od_kpi_scope": "selected_local_portfolio_before_global_rescue",
        "final_group_kpi_scope": "final_after_global_rescue_grouped_by_original_request_od",
        "final_group_kpi_csv": "metrics/final_group_kpi_hourly.csv",
        "hour_kpi_scope": "final_after_global_rescue",
        "global_rescue_metrics_csv": "metrics/global_rescue.csv",
        "od_cache_key_format": "canonical_fixed_4dp_with_legacy_lookup",
        "od_cache_directionality": "directed_for_new_rows_legacy_mixed_compatible",
        "od_cache_worker_connections": "pid_thread_local_readonly",
        "solver_travel_cache_requested": solver_travel_cache,
        "solver_travel_cache_effective": effective_solver_travel_cache,
        "solver_travel_cache_max_entries": int(solver_travel_cache_max_entries),
        "solver_travel_cache_key": "exact_directed_raw_coordinate_4tuple",
        "solver_travel_cache_policy": "bounded_process_local_lru",
        "travel_cache_diagnostics_csv": "metrics/travel_cache_diagnostics.csv",
        "winner_only_materialization_requested": winner_only_materialization,
        "winner_only_materialization_effective": effective_winner_only_materialization,
        "winner_only_materialization_policy": (
            "search_payload_then_selected_route_plan_full_evaluation"
            if effective_winner_only_materialization
            else "materialize_every_worker_result"
        ),
        "od_cache_path": str(od_cache_path or ""),
        "od_cache_strict": bool(od_cache_strict),
        "public_objective_semantics": "drt_call_to_drop_plus_4x_pt_lateness_plus_rejection_penalty",
        "alns_trace_cost_semantics": (
            "quality_search_energy" if alns_quality_profile else "legacy_objective"
        ),
        "summary_cost_semantics": "legacy_objective_with_additive_search_cost_fields",
        "alns_diagnostics_csv": "metrics/alns_diagnostics.csv",
        "alns_diagnostics_schema_version": 1,
        "solver_group_policy": "bidirectional_interregion_pairs",
        "solver_group_count": len(SOLVER_GROUP_LIST),
        "seed_base": seed_base,
        "service_hour_start": service_hour_start,
        "service_hour_end": service_hour_end,
        "rebalancing_strategy": rebalancing_strategy,
        "run_start_stamp": start_stamp,
        "run_invocation": run_invocation or "",
    }
    run_config_base.update(input_identity)

    current_hour: Optional[int] = None

    try:
        output = read_json(stub_output_path)
        reqs_all = load_stage0_requests(stage0_requests_csv)
        choice_metadata_stats = enrich_requests_with_choice_deadlines(
            reqs_all,
            output,
            car_time_ratio_cap=car_time_ratio_cap,
        )
        run_config_base["choice_metadata"] = choice_metadata_stats
        req_meta_by_id = {int(r["req_id"]): r for r in reqs_all}
        req_result_by_id: Dict[int, Dict[str, Any]] = {}
        vehicle_served_count: Dict[int, int] = {}

        class _R:
            def __init__(self, d):
                self.o_region = d["o_region"]
                self.d_region = d["d_region"]

        vehicles = init_fleet_from_requests([_R(r) for r in reqs_all], fleet_size=fleet_size, cap=cap)

        write_csv_header(metrics_dir / "alns_trace.csv", ALNS_TRACE_HEADER)
        write_csv_header(metrics_dir / "alns_diagnostics.csv", ALNS_DIAGNOSTICS_HEADER)
        write_csv_header(
            metrics_dir / "alns_portfolio.csv",
            [
                "hour", "o_region", "d_region", "replica", "seed",
                "selected", "status", "runtime_sec", "iters", "best_cost",
                "served", "choice_eligible", "not_choice_eligible", "unassigned",
                "cache_hits", "cache_legacy_hits", "cache_misses",
                "cache_fallbacks", "cache_lookup_errors",
            ],
        )
        write_csv_header(
            metrics_dir / "global_rescue.csv",
            [
                "hour", "enabled", "replicas", "selected_replica", "runtime_sec",
                "initial_choice_eligible", "final_choice_eligible",
                "initial_unassigned", "final_unassigned", "best_cost", "status",
            ],
        )
        write_csv_header(
            metrics_dir / "adaptive_global_rescue.csv",
            [
                "hour", "enabled", "tier", "reason", "stage_index", "stage",
                "replicas", "allocated_budget_sec", "used_budget_sec",
                "solver_runtime_sum_sec", "selected_replica",
                "before_quality_key", "after_quality_key",
                "before_choice_eligible", "after_choice_eligible",
                "before_not_choice_eligible", "after_not_choice_eligible",
                "before_unassigned", "after_unassigned",
                "before_choice_excess_min", "after_choice_excess_min",
                "before_near_deadline_risk_min", "after_near_deadline_risk_min",
                "before_passenger_time_min", "after_passenger_time_min",
                "before_deadhead_km", "after_deadhead_km",
                "before_total_vmt_km", "after_total_vmt_km",
                "before_best_cost", "after_best_cost", "improved", "accepted",
                "fallback_required", "fallback_triggered", "status",
            ],
        )
        write_csv_header(
            metrics_dir / "travel_cache_diagnostics.csv",
            [
                "phase", "hour", "o_region", "d_region", "replica", "selected",
                "pid", "backing_hits", "canonical_hits", "legacy_key_hits",
                "backing_misses", "fallbacks", "lookup_errors", "memory_hits",
                "memory_misses", "memory_hit_rate", "memory_size",
                "memory_max_entries", "memory_evictions",
            ],
        )
        write_csv_header(
            metrics_dir / "alns_operator_diagnostics.csv",
            [
                "phase", "hour", "o_region", "d_region", "replica", "selected",
                "accepted_stage", "operator_kind", "operator", "calls", "no_ops",
                "accepted", "improved_current", "best_improved", "runtime_sec",
                "final_weight", "t0_runtime_sec", "t0_samples_effective", "pid",
            ],
        )
        write_csv_header(metrics_dir / "alns_global_trace.csv", ALNS_TRACE_HEADER)
        write_csv_header(metrics_dir / "hour_kpi.csv", HOUR_KPI_HEADER)
        write_csv_header(metrics_dir / "od_kpi_hourly.csv", OD_KPI_HEADER)
        write_csv_header(
            metrics_dir / "final_group_kpi_hourly.csv",
            [
                "hour", "o_region", "d_region", "group_mode", "group_label",
                "candidates", "served", "choice_eligible", "unassigned",
                "obj_total", "obj_base", "obj_penalty",
            ],
        )
        write_csv_header(kpi_dir / "requests.csv", REQUESTS_HEADER)
        write_csv_header(kpi_dir / "vehicles.csv", VEHICLES_HEADER)
        write_csv_header(kpi_dir / "vehicle_events.csv", VEHICLE_EVENTS_HEADER)
        append_file(run_tmp / "stage0_requests.csv", stage0_requests_csv)

        reqs_by_hour: Dict[int, List[Dict[str, Any]]] = {h: [] for h in range(24)}
        for r in reqs_all:
            reqs_by_hour[int(r["hour"])].append(r)

        inside_demand_by_hour: Dict[int, int] = {h: 0 for h in range(24)}
        for h in range(24):
            inside_demand_by_hour[h] = sum(
                1
                for r in reqs_by_hour[h]
                if r["o_region"] != RegionId.OUTSIDE and r["d_region"] != RegionId.OUTSIDE
            )

        global_start = time.time()
        if shared_executor_factory is not None:
            from .batch_pool import ReferenceClock
            shared_executor_factory.clock = ReferenceClock()
        def budget_elapsed():
            return shared_executor_factory.clock.elapsed() if shared_executor_factory else time.time() - global_start
        travel_cache_totals: Counter[str] = Counter()
        travel_cache_max_size = 0
        operator_totals: Counter[str] = Counter()
        operator_runtime_totals: Counter[str] = Counter()
        t0_runtime_total_sec = 0.0
        t0_samples_observed: Dict[str, Counter[int]] = {
            "local": Counter(),
            "global": Counter(),
        }

        def record_travel_cache_stats(
            *,
            phase: str,
            hour_value: int,
            group: Tuple[RegionId, RegionId],
            replica: int,
            selected: bool,
            stats: Dict[str, Any],
        ) -> None:
            nonlocal travel_cache_max_size
            memory_hits = int(stats.get("memory_hits", 0) or 0)
            memory_misses = int(stats.get("memory_misses", 0) or 0)
            memory_total = memory_hits + memory_misses
            memory_size = int(stats.get("memory_size", 0) or 0)
            travel_cache_max_size = max(travel_cache_max_size, memory_size)
            counter_keys = (
                "hits", "canonical_hits", "legacy_key_hits", "misses",
                "fallbacks", "lookup_errors", "memory_hits", "memory_misses",
                "memory_evictions",
            )
            for key in counter_keys:
                travel_cache_totals[key] += int(stats.get(key, 0) or 0)
            append_csv_row(
                metrics_dir / "travel_cache_diagnostics.csv",
                [
                    phase, hour_value, group[0].value, group[1].value, replica,
                    int(selected), stats.get("pid", ""), stats.get("hits", 0),
                    stats.get("canonical_hits", 0), stats.get("legacy_key_hits", 0),
                    stats.get("misses", 0), stats.get("fallbacks", 0),
                    stats.get("lookup_errors", 0), memory_hits, memory_misses,
                    memory_hits / memory_total if memory_total else 0.0,
                    memory_size, stats.get("memory_max_entries", 0),
                    stats.get("memory_evictions", 0),
                ],
            )

        def record_operator_stats(
            *,
            phase: str,
            hour_value: int,
            group: Tuple[RegionId, RegionId],
            replica: int,
            selected: bool,
            accepted_stage: bool,
            summary: Dict[str, Any],
        ) -> None:
            nonlocal t0_runtime_total_sec
            t0_runtime = float(summary.get("t0_runtime_sec", 0.0) or 0.0)
            t0_samples_effective = int(summary.get("t0_samples_effective", 0) or 0)
            t0_runtime_total_sec += t0_runtime
            phase_family = "local" if phase == "local" else "global"
            t0_samples_observed[phase_family][t0_samples_effective] += 1
            pid = (summary.get("od_cache_stats") or {}).get("pid", "")
            append_csv_row(
                metrics_dir / "alns_operator_diagnostics.csv",
                [
                    phase, hour_value, group[0].value, group[1].value, replica,
                    int(selected), int(accepted_stage), "meta", "T0", 0, 0, 0, 0,
                    0, 0.0, "", t0_runtime, t0_samples_effective, pid,
                ],
            )
            nested_stats = summary.get("operator_stats") or {}
            for operator_kind in ("destroy", "repair"):
                for operator, stats in sorted((nested_stats.get(operator_kind) or {}).items()):
                    calls = int(stats.get("calls", 0) or 0)
                    no_ops = int(stats.get("no_ops", 0) or 0)
                    accepted = int(stats.get("accepted", 0) or 0)
                    improved = int(stats.get("improved_current", 0) or 0)
                    best_improved = int(stats.get("best_improved", 0) or 0)
                    runtime = float(stats.get("runtime_sec", 0.0) or 0.0)
                    prefix = f"{operator_kind}:{operator}"
                    operator_totals[f"{prefix}:calls"] += calls
                    operator_totals[f"{prefix}:no_ops"] += no_ops
                    operator_totals[f"{prefix}:accepted"] += accepted
                    operator_totals[f"{prefix}:improved_current"] += improved
                    operator_totals[f"{prefix}:best_improved"] += best_improved
                    operator_runtime_totals[prefix] += runtime
                    append_csv_row(
                        metrics_dir / "alns_operator_diagnostics.csv",
                        [
                            phase, hour_value, group[0].value, group[1].value,
                            replica, int(selected), int(accepted_stage), operator_kind,
                            operator, calls, no_ops, accepted, improved, best_improved,
                            runtime, stats.get("final_weight", ""), t0_runtime,
                            t0_samples_effective, pid,
                        ],
                    )

        # ── Sticky assignment: track previous hour assignments ──
        prev_assignments: Dict[int, Tuple[RegionId, RegionId]] = {}

        for hour in range(24):
            current_hour = hour
            if budget_elapsed() > global_time_limit_sec:
                raise TimeoutError("Global time limit exceeded")

            hour_start_clock = time.time()
            hour_inside_requests: List[Dict[str, Any]] = []
            outside_excluded = 0
            for r in reqs_by_hour[hour]:
                if r["o_region"] == RegionId.OUTSIDE or r["d_region"] == RegionId.OUTSIDE:
                    outside_excluded += 1
                    continue
                hour_inside_requests.append(r)

            requests_by_group = _build_requests_by_solver_group(hour_inside_requests)

            for v in vehicles:
                v.start_time_min = v.finish_time_min
                v.start_lat = v.finish_lat
                v.start_lon = v.finish_lon

            hour_vehicle_snapshot = [
                {
                    "veh_id": v.veh_id,
                    "cap": v.cap,
                    "start_time_min": v.start_time_min,
                    "start_lat": v.start_lat,
                    "start_lon": v.start_lon,
                }
                for v in vehicles
            ]

            if shared_executor_factory is not None:
                import json
                with (metrics_dir / "hour_state.jsonl").open("a") as state_log:
                    state_log.write(json.dumps({"hour": hour, "vehicles": hour_vehicle_snapshot,
                        "budget_elapsed_sec": budget_elapsed(), "monotonic": time.monotonic()}) + "\n")

            # ── Service hour check: skip dispatch for out-of-service hours ──
            if hour < service_hour_start or hour > service_hour_end:
                # Vehicles stay in place, no dispatch.  Record empty hour KPI.
                runtime_sec = time.time() - hour_start_clock
                append_csv_row(
                    metrics_dir / "hour_kpi.csv",
                    [hour, len(reqs_by_hour[hour]), outside_excluded, 0,
                     0.0, 0.0, 0.0, 0.0, 0.0, 0.0, runtime_sec],
                )
                for group in SOLVER_GROUP_LIST:
                    req_list = requests_by_group.get(group, [])
                    fwd, rev = _group_direction_counts(group, req_list)
                    append_csv_row(
                        metrics_dir / "od_kpi_hourly.csv",
                        [hour, group[0].value, group[1].value,
                         solver_group_mode(group), solver_group_label(group),
                         len(req_list), 0, 0, 0, fwd, rev,
                         0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
                    )
                    append_csv_row(
                        metrics_dir / "alns_trace.csv",
                        [hour, group[0].value, group[1].value, 1,
                         0.0, 0.0, 0.0, 0.0, 0.0, 0, 0, "NONE", "NONE", 0],
                    )
                    append_csv_row(
                        metrics_dir / "final_group_kpi_hourly.csv",
                        [
                            hour, group[0].value, group[1].value,
                            solver_group_mode(group), solver_group_label(group),
                            len(req_list), 0, 0, len(req_list),
                            0.0, 0.0, 0.0,
                        ],
                    )
                continue

            group_targets = compute_solver_group_targets(
                requests_by_group,
                fleet_size=fleet_size,
                strategy=fleet_target_strategy,
            )
            if rebalancing_strategy == "sticky":
                assignments = assign_vehicles_to_solver_groups_sticky(
                    vehicles, group_targets, requests_by_group, prev_assignments
                )
            elif rebalancing_strategy == "hungarian":
                assignments = assign_vehicles_to_solver_groups_hungarian(
                    vehicles,
                    group_targets,
                    requests_by_group,
                    deadline_aware=(fleet_target_strategy == "workload_deadline"),
                )
            elif rebalancing_strategy == "default":
                assignments = assign_vehicles_to_solver_groups(vehicles, group_targets, requests_by_group)
            else:  # Defensive guard for future programmatic callers.
                raise ValueError(f"unsupported rebalancing_strategy: {rebalancing_strategy!r}")
            hour_budget = _compute_hour_budget(
                global_start=time.time() - budget_elapsed(),
                global_limit_sec=global_time_limit_sec,
                hour_idx=hour,
                inside_demand_by_hour=inside_demand_by_hour,
            )
            group_budget = _group_budgets_for_hour(hour_budget, requests_by_group)

            group_order = _order_solver_groups_for_hour(requests_by_group)
            active_groups = [
                group for group in group_order
                if requests_by_group.get(group) and assignments.get(group)
            ]
            for group in group_order:
                req_list = requests_by_group.get(group, [])
                if req_list and not assignments.get(group):
                    err = RuntimeError(
                        f"solver group {solver_group_label(group)} has demand but no vehicles assigned"
                    )
                    raise SimulationSubproblemError(
                        f"INFEASIBLE at hour={hour}, group={solver_group_label(group)}",
                        _build_subproblem_diagnostics(
                            hour=hour,
                            od=group,
                            requests_od=req_list,
                            vehicles_od=[],
                            vehicles_target=int(group_targets.get(group, 0)),
                            time_budget_sec=float(group_budget.get(group, 0.0)),
                            speed_kmph=speed_kmph,
                            init_strategy=init_strategy,
                            exc=err,
                        ),
                    ) from err

            max_parallel_replicas = max(
                1, int(workers) // max(1, len(active_groups))
            )
            replicas_this_hour = min(
                max(1, int(portfolio_replicas)), max_parallel_replicas
            )
            local_budget_fraction = (
                1.0 - float(global_rescue_time_fraction) if global_rescue else 1.0
            )
            successes: Dict[Tuple[RegionId, RegionId], List[Tuple[int, int, Dict[str, Any]]]] = {
                group: [] for group in active_groups
            }
            failures: Dict[Tuple[RegionId, RegionId], List[Tuple[int, Exception]]] = {
                group: [] for group in active_groups
            }
            task_count = len(active_groups) * replicas_this_hour
            if task_count:
                with phase_executor(
                    stage="local", hour=hour,
                    max_workers=min(int(workers), task_count),
                    initializer=_initialize_solver_worker,
                    initargs=(
                        str(od_cache_path or ""), bool(od_cache_strict),
                        effective_solver_travel_cache,
                        int(solver_travel_cache_max_entries),
                    ),
                ) as ex:
                    fut_to_ctx = {}
                    for group in active_groups:
                        req_list = requests_by_group[group]
                        veh_ids = assignments[group]
                        veh_list = [hour_vehicle_snapshot[vid] for vid in veh_ids]
                        for replica in range(replicas_this_hour):
                            replica_seed = _portfolio_seed(seed_base, hour, group, replica)
                            fut = ex.submit(
                                solve_od_alns_sa,
                                hour,
                                group,
                                req_list,
                                veh_list,
                                float(group_budget.get(group, 0.0)) * local_budget_fraction,
                                replica_seed,
                                str(tmp_dir),
                                init_strategy,
                                init_multistart,
                                init_regret_k,
                                init_vehicle_top_k,
                                init_pos_samples,
                                init_candidate_pool,
                                init_time_limit_sec,
                                alns_iter_cap,
                                speed_kmph,
                                rejection_penalty,
                                alns_selective_route_reevaluation=alns_selective_route_reevaluation,
                                alns_lightweight_eval_only=alns_lightweight_eval_only,
                                alns_true_regret2=alns_true_regret2,
                                sa_cooling_rate=sa_cooling_rate,
                                sa_initial_acceptance_probability=sa_initial_acceptance_probability,
                                sa_stagnation_patience_iters=effective_stagnation_patience_iters,
                                sa_stagnation_min_iters=sa_stagnation_min_iters,
                                sa_accept_rate_window_iters=sa_accept_rate_window_iters,
                                sa_high_accept_rate_threshold=sa_high_accept_rate_threshold,
                                alns_quality_profile=alns_quality_profile,
                                quality_choice_miss_penalty=quality_choice_miss_penalty,
                                quality_choice_excess_weight=quality_choice_excess_weight,
                                quality_near_deadline_buffer_min=quality_near_deadline_buffer_min,
                                quality_regret_k=quality_regret_k,
                                quality_unassigned_repair_limit=quality_unassigned_repair_limit,
                                quality_reheat_interval_iters=quality_reheat_interval_iters,
                                quality_max_reheats=quality_max_reheats,
                                quality_choice_loss_acceptance_cap=quality_choice_loss_acceptance_cap,
                                alns_noop_operator_adaptation=alns_noop_operator_adaptation,
                                t0_samples=int(alns_t0_samples),
                                artifact_tag=f"p{replica}",
                                materialize_output=(
                                    not effective_winner_only_materialization
                                ),
                            )
                            fut_to_ctx[fut] = (group, replica, replica_seed, req_list, veh_list)

                    if shared_executor_factory is not None:
                        ex.seal()
                    for fut in as_completed(fut_to_ctx):
                        group, replica, replica_seed, req_list, veh_list = fut_to_ctx[fut]
                        try:
                            successes[group].append((replica, replica_seed, fut.result()))
                        except Exception as exc:
                            failures[group].append((replica, exc))
                            append_csv_row(
                                metrics_dir / "alns_portfolio.csv",
                                [hour, group[0].value, group[1].value, replica,
                                 replica_seed, 0, f"failed:{type(exc).__name__}",
                                 0.0, 0, "", 0, 0, len(req_list), len(req_list),
                                 "", "", "", "", ""],
                            )

            if shared_executor_factory is not None and any(failures.values()):
                raise RuntimeError("at least one local replica failed")
            results_all: List[Dict[str, Any]] = []
            for group in active_groups:
                group_results = successes[group]
                if not group_results:
                    replica, exc = failures[group][0]
                    raise SimulationSubproblemError(
                        f"all portfolio replicas failed at hour={hour}, group={solver_group_label(group)}",
                        _build_subproblem_diagnostics(
                            hour=hour,
                            od=group,
                            requests_od=requests_by_group[group],
                            vehicles_od=[hour_vehicle_snapshot[vid] for vid in assignments[group]],
                            vehicles_target=int(group_targets.get(group, 0)),
                            time_budget_sec=float(group_budget.get(group, 0.0)),
                            speed_kmph=speed_kmph,
                            init_strategy=init_strategy,
                            exc=exc,
                        ),
                    ) from exc
                winner_replica, _, winner = min(
                    group_results,
                    key=lambda item: _result_quality_key(item[2], item[0]),
                )
                results_all.append(winner)
                for replica, replica_seed, result in sorted(group_results):
                    summ = result["summary"]
                    score = summ.get("quality_score") or {}
                    cache_stats = summ.get("od_cache_stats") or {}
                    record_travel_cache_stats(
                        phase="local",
                        hour_value=hour,
                        group=group,
                        replica=replica,
                        selected=(replica == winner_replica),
                        stats=cache_stats,
                    )
                    record_operator_stats(
                        phase="local",
                        hour_value=hour,
                        group=group,
                        replica=replica,
                        selected=(replica == winner_replica),
                        accepted_stage=(replica == winner_replica),
                        summary=summ,
                    )
                    append_csv_row(
                        metrics_dir / "alns_portfolio.csv",
                        [
                            hour, group[0].value, group[1].value, replica,
                            replica_seed, int(replica == winner_replica), "ok",
                            summ.get("runtime_sec", 0.0), summ.get("iters", 0),
                            summ.get("best_cost", 0.0), summ.get("served", 0),
                            score.get("choice_eligible", summ.get("served", 0)),
                            score.get("not_choice_eligible", 0),
                            score.get("unassigned", 0),
                            cache_stats.get("hits", 0),
                            cache_stats.get("legacy_key_hits", 0),
                            cache_stats.get("misses", 0),
                            cache_stats.get("fallbacks", 0),
                            cache_stats.get("lookup_errors", 0),
                        ],
                    )

            groups_ran = set()
            hour_obj_total = 0.0
            hour_base = 0.0
            hour_pen = 0.0
            hour_vmt_total = 0.0
            hour_vmt_dead = 0.0
            hour_vmt_serv = 0.0
            hour_served = 0

            application_results = results_all
            global_best_cost: Optional[float] = None
            remaining_global_sec = max(
                0.0, global_time_limit_sec - budget_elapsed()
            )
            if (
                global_rescue
                and float(global_rescue_time_fraction) > 0.0
                and remaining_global_sec > 0.02
                and results_all
                and hour_inside_requests
            ):
                merged_initial_routes = _merge_route_plans(results_all)
                rescue_budget = min(
                    max(0.02, hour_budget * float(global_rescue_time_fraction)),
                    remaining_global_sec,
                )
                rescue_count = min(max(1, int(global_rescue_replicas)), max(1, int(workers)))

                def run_rescue_wave(
                    stage: str,
                    replica_count: int,
                    budget_sec: float,
                    initial_routes: Dict[int, List[Tuple[str, int]]],
                    seed_generation: int,
                ) -> Dict[str, Any]:
                    wave_start = time.perf_counter()
                    wave_results: List[Tuple[int, Dict[str, Any]]] = []
                    wave_errors: List[Exception] = []
                    with phase_executor(
                        stage="rescue:" + stage, hour=hour,
                        max_workers=max(1, int(replica_count)),
                        initializer=_initialize_solver_worker,
                        initargs=(
                            str(od_cache_path or ""), bool(od_cache_strict),
                            effective_solver_travel_cache,
                            int(solver_travel_cache_max_entries),
                        ),
                    ) as ex:
                        rescue_futures = {}
                        for replica in range(max(1, int(replica_count))):
                            rescue_seed = (
                                seed_base
                                + hour * 1000
                                + 900_001
                                + int(seed_generation) * 10_000_019
                                + replica * 1_000_003
                            )
                            artifact_tag = (
                                f"global_r{replica}"
                                if stage == "legacy_full"
                                else f"adaptive_{stage}_r{replica}"
                            )
                            fut = ex.submit(
                                solve_od_alns_sa,
                                hour,
                                SOLVER_GROUP_LIST[0],
                                hour_inside_requests,
                                hour_vehicle_snapshot,
                                float(budget_sec),
                                rescue_seed,
                                str(tmp_dir),
                                init_strategy,
                                init_multistart,
                                init_regret_k,
                                init_vehicle_top_k,
                                init_pos_samples,
                                init_candidate_pool,
                                init_time_limit_sec,
                                alns_iter_cap,
                                speed_kmph,
                                rejection_penalty,
                                alns_selective_route_reevaluation=alns_selective_route_reevaluation,
                                alns_lightweight_eval_only=alns_lightweight_eval_only,
                                alns_true_regret2=alns_true_regret2,
                                sa_cooling_rate=sa_cooling_rate,
                                sa_initial_acceptance_probability=sa_initial_acceptance_probability,
                                sa_stagnation_patience_iters=effective_stagnation_patience_iters,
                                sa_stagnation_min_iters=sa_stagnation_min_iters,
                                sa_accept_rate_window_iters=sa_accept_rate_window_iters,
                                sa_high_accept_rate_threshold=sa_high_accept_rate_threshold,
                                alns_quality_profile=alns_quality_profile,
                                quality_choice_miss_penalty=quality_choice_miss_penalty,
                                quality_choice_excess_weight=quality_choice_excess_weight,
                                quality_near_deadline_buffer_min=quality_near_deadline_buffer_min,
                                quality_regret_k=quality_regret_k,
                                quality_unassigned_repair_limit=quality_unassigned_repair_limit,
                                quality_reheat_interval_iters=quality_reheat_interval_iters,
                                quality_max_reheats=quality_max_reheats,
                                quality_choice_loss_acceptance_cap=quality_choice_loss_acceptance_cap,
                                alns_noop_operator_adaptation=alns_noop_operator_adaptation,
                                t0_samples=effective_global_rescue_t0_samples,
                                artifact_tag=artifact_tag,
                                initial_routes=initial_routes,
                                materialize_output=(
                                    not effective_winner_only_materialization
                                ),
                            )
                            rescue_futures[fut] = replica
                        if shared_executor_factory is not None:
                            ex.seal()
                        for fut in as_completed(rescue_futures):
                            replica = rescue_futures[fut]
                            try:
                                wave_results.append((replica, fut.result()))
                            except Exception as exc:
                                wave_errors.append(exc)
                    return {
                        "results": wave_results,
                        "errors": wave_errors,
                        "wall_runtime_sec": (ex.timing["reference_sec"] if shared_executor_factory else time.perf_counter() - wave_start),
                        "actual_wall_runtime_sec": time.perf_counter() - wave_start,
                        "solver_runtime_sum_sec": sum(
                            float(result.get("summary", {}).get("runtime_sec", 0.0) or 0.0)
                            for _, result in wave_results
                        ),
                    }

                if effective_adaptive_global_rescue:
                    initial_aggregate = _aggregate_result_quality(results_all)
                    downstream_pressure = _next_service_hour_pressure(
                        hour=hour,
                        service_hour_end=service_hour_end,
                        inside_demand_by_hour=inside_demand_by_hour,
                        fleet_size=fleet_size,
                    )
                    adaptive = _execute_adaptive_global_rescue(
                        initial_aggregate,
                        full_replicas=rescue_count,
                        total_budget_sec=rescue_budget,
                        run_wave=run_rescue_wave,
                        compact_polish_requested=(
                            adaptive_global_rescue_compact_polish
                        ),
                        compact_polish_effective=(
                            effective_adaptive_global_rescue_compact_polish
                        ),
                        downstream_pressure=downstream_pressure,
                        compact_pressure_threshold=compact_pressure_threshold,
                        fail_on_error=shared_executor_factory is not None,
                    )
                    plan = adaptive["plan"]
                    accepted_result = adaptive.get("accepted_result")
                    if accepted_result is not None:
                        application_results = [accepted_result]
                        global_best_cost = float(
                            accepted_result.get("summary", {}).get("best_cost", 0.0)
                        )

                    for stage_index, stage_record in enumerate(adaptive["stages"], 1):
                        before_summary = stage_record["before"].get("summary", {})
                        after_summary = stage_record["after"].get("summary", {})
                        before_score = before_summary.get("quality_score") or {}
                        after_score = after_summary.get("quality_score") or {}
                        append_csv_row(
                            metrics_dir / "adaptive_global_rescue.csv",
                            [
                                hour, 1, plan["tier"], plan["reason"], stage_index,
                                stage_record["stage"], stage_record["replicas"],
                                stage_record["allocated_budget_sec"],
                                stage_record["used_budget_sec"],
                                stage_record["solver_runtime_sum_sec"],
                                stage_record["selected_replica"]
                                if stage_record["selected_replica"] is not None else "",
                                repr(_quality_key_without_replica(stage_record["before"])),
                                repr(_quality_key_without_replica(stage_record["after"])),
                                before_score.get("choice_eligible", 0),
                                after_score.get("choice_eligible", 0),
                                before_score.get("not_choice_eligible", 0),
                                after_score.get("not_choice_eligible", 0),
                                before_score.get("unassigned", 0),
                                after_score.get("unassigned", 0),
                                before_score.get("choice_excess_min", 0.0),
                                after_score.get("choice_excess_min", 0.0),
                                before_score.get("near_deadline_risk_min", 0.0),
                                after_score.get("near_deadline_risk_min", 0.0),
                                before_score.get("passenger_time_min", 0.0),
                                after_score.get("passenger_time_min", 0.0),
                                before_score.get("deadhead_km", 0.0),
                                after_score.get("deadhead_km", 0.0),
                                before_score.get("total_vmt_km", 0.0),
                                after_score.get("total_vmt_km", 0.0),
                                before_summary.get("best_cost", 0.0),
                                after_summary.get("best_cost", 0.0),
                                int(stage_record["improved"]),
                                int(stage_record["accepted"]),
                                int(bool(plan.get("fallback_required", False))),
                                int(bool(adaptive["fallback_triggered"])),
                                stage_record["status"],
                            ],
                        )
                        selected_replica = stage_record["selected_replica"]
                        candidate = stage_record.get("candidate")
                        if candidate is not None:
                            append_file(
                                metrics_dir / "alns_global_trace.csv",
                                candidate["trace_path"],
                                skip_header=True,
                            )
                        for replica, result in sorted(stage_record["results"]):
                            record_travel_cache_stats(
                                phase=f"global_{stage_record['stage']}",
                                hour_value=hour,
                                group=SOLVER_GROUP_LIST[0],
                                replica=replica,
                                selected=(replica == selected_replica),
                                stats=result.get("summary", {}).get("od_cache_stats") or {},
                            )
                            record_operator_stats(
                                phase=f"global_{stage_record['stage']}",
                                hour_value=hour,
                                group=SOLVER_GROUP_LIST[0],
                                replica=replica,
                                selected=(replica == selected_replica),
                                accepted_stage=(
                                    bool(stage_record.get("accepted"))
                                    and replica == selected_replica
                                ),
                                summary=result.get("summary", {}),
                            )

                    initial_score = initial_aggregate["summary"].get("quality_score") or {}
                    final_summary = adaptive["winner"].get("summary", {})
                    final_score = final_summary.get("quality_score") or {}
                    accepted_stage = next(
                        (
                            stage
                            for stage in reversed(adaptive["stages"])
                            if stage["stage"] == adaptive.get("accepted_stage")
                            and stage["accepted"]
                        ),
                        None,
                    )
                    any_success = any(stage["candidate"] is not None for stage in adaptive["stages"])
                    # Preserve the established global_rescue.csv status
                    # vocabulary; adaptive details live in the additive CSV.
                    status = "ok"
                    if not any_success:
                        first_error = next(
                            (
                                error
                                for stage in adaptive["stages"]
                                for error in stage["errors"]
                            ),
                            None,
                        )
                        status = (
                            "fallback:"
                            f"{type(first_error).__name__ if first_error else 'unknown'}"
                        )
                    append_csv_row(
                        metrics_dir / "global_rescue.csv",
                        [
                            hour, 1,
                            sum(int(stage["replicas"]) for stage in adaptive["stages"]),
                            (
                                accepted_stage["selected_replica"]
                                if accepted_stage is not None else ""
                            ),
                            adaptive["used_budget_sec"],
                            initial_score.get("choice_eligible", 0),
                            final_score.get("choice_eligible", 0),
                            initial_score.get("unassigned", 0),
                            final_score.get("unassigned", 0),
                            final_summary.get("best_cost", 0.0), status,
                        ],
                    )
                else:
                    legacy_wave = run_rescue_wave(
                        "legacy_full",
                        rescue_count,
                        rescue_budget,
                        merged_initial_routes,
                        0,
                    )
                    rescue_results = legacy_wave["results"]
                    rescue_errors = legacy_wave["errors"]
                    if rescue_results:
                        selected_replica, rescue_winner = min(
                            rescue_results,
                            key=lambda item: _result_quality_key(item[1], item[0]),
                        )
                        application_results = [rescue_winner]
                        rs = rescue_winner["summary"]
                        initial_score = rs.get("initial_quality_score") or {}
                        final_score = rs.get("quality_score") or {}
                        append_csv_row(
                            metrics_dir / "global_rescue.csv",
                            [
                                hour, 1, rescue_count, selected_replica,
                                rs.get("runtime_sec", 0.0),
                                initial_score.get("choice_eligible", 0),
                                final_score.get("choice_eligible", 0),
                                initial_score.get("unassigned", 0),
                                final_score.get("unassigned", 0),
                                rs.get("best_cost", 0.0), "ok",
                            ],
                        )
                        append_file(
                            metrics_dir / "alns_global_trace.csv",
                            rescue_winner["trace_path"],
                            skip_header=True,
                        )
                        global_best_cost = float(rs.get("best_cost", 0.0))
                        for replica, result in sorted(rescue_results):
                            record_travel_cache_stats(
                                phase="global",
                                hour_value=hour,
                                group=SOLVER_GROUP_LIST[0],
                                replica=replica,
                                selected=(replica == selected_replica),
                                stats=result.get("summary", {}).get("od_cache_stats") or {},
                            )
                            record_operator_stats(
                                phase="global",
                                hour_value=hour,
                                group=SOLVER_GROUP_LIST[0],
                                replica=replica,
                                selected=(replica == selected_replica),
                                accepted_stage=(replica == selected_replica),
                                summary=result.get("summary", {}),
                            )
                    else:
                        append_csv_row(
                            metrics_dir / "global_rescue.csv",
                            [hour, 1, rescue_count, "", 0.0, "", "", "", "", "",
                             f"fallback:{type(rescue_errors[0]).__name__ if rescue_errors else 'unknown'}"],
                        )
            else:
                append_csv_row(
                    metrics_dir / "global_rescue.csv",
                    [hour, int(global_rescue), 0, "", 0.0, "", "", "", "", "", "skipped"],
                )

            for res in results_all:
                summ = res["summary"]
                group = (_parse_region(str(summ["o_region"])), _parse_region(str(summ["d_region"])))
                groups_ran.add(group)
                hour_obj_total += float(summ.get("best_cost", 0.0))
                forward_candidates = int(summ.get("forward_candidates", 0))
                reverse_candidates = int(summ.get("reverse_candidates", 0))

                append_csv_row(
                    metrics_dir / "od_kpi_hourly.csv",
                    [
                        summ["hour"],
                        summ["o_region"],
                        summ["d_region"],
                        summ.get("group_mode", solver_group_mode(group)),
                        summ.get("group_label", solver_group_label(group)),
                        summ["candidates"],
                        summ["served"],
                        group_targets.get(group, 0),
                        len(assignments.get(group, [])),
                        forward_candidates,
                        reverse_candidates,
                        summ["iters"],
                        summ["time_budget_sec"],
                        summ["runtime_sec"],
                        summ["initial_cost"],
                        summ["final_cost"],
                        summ["best_cost"],
                        summ["accept_rate"],
                    ],
                )

                append_csv_row(
                    metrics_dir / "alns_diagnostics.csv",
                    [
                        summ["hour"],
                        summ["o_region"],
                        summ["d_region"],
                        summ.get("group_mode", solver_group_mode(group)),
                        summ.get("group_label", solver_group_label(group)),
                        summ["candidates"],
                        summ["iters"],
                        summ["time_budget_sec"],
                        summ["runtime_sec"],
                        summ.get("initial_temperature", 0.0),
                        summ.get("accept_rate", 0.0),
                        summ.get("uphill_accept_rate", 0.0),
                        summ.get("zero_delta_rate", 0.0),
                        summ.get("accept_rate_window_final", 0.0),
                        summ.get("accept_rate_window_peak", 0.0),
                        summ.get("high_accept_window_count", 0),
                        summ.get("last_improved_iter", 0),
                        summ.get("iterations_since_improvement", 0),
                        summ.get("stopped_early", False),
                        summ.get("stop_reason", "unknown"),
                    ],
                )

                append_file(metrics_dir / "alns_trace.csv", res["trace_path"], skip_header=True)

            if global_best_cost is not None:
                hour_obj_total = global_best_cost

            if effective_winner_only_materialization and application_results:
                materialized_results: List[Dict[str, Any]] = []
                is_global_application = global_best_cost is not None
                for result_index, search_result in enumerate(application_results):
                    if is_global_application:
                        materialize_requests = hour_inside_requests
                        materialize_vehicles = hour_vehicle_snapshot
                        tag = f"global_{result_index}"
                    else:
                        search_summary = search_result["summary"]
                        result_group = (
                            _parse_region(str(search_summary["o_region"])),
                            _parse_region(str(search_summary["d_region"])),
                        )
                        materialize_requests = requests_by_group[result_group]
                        materialize_vehicles = [
                            hour_vehicle_snapshot[vid]
                            for vid in assignments[result_group]
                        ]
                        tag = f"local_{result_group[0].value}_{result_group[1].value}"

                    event_path = tmp_dir / f"events_selected_h{hour}_{tag}.csv"
                    materialized = materialize_route_plan(
                        search_result["route_plan"],
                        materialize_requests,
                        materialize_vehicles,
                        events_path=event_path,
                        speed_kmph=speed_kmph,
                        rejection_penalty=rejection_penalty,
                        alns_quality_profile=alns_quality_profile,
                        quality_choice_miss_penalty=quality_choice_miss_penalty,
                        quality_choice_excess_weight=quality_choice_excess_weight,
                        quality_near_deadline_buffer_min=quality_near_deadline_buffer_min,
                    )
                    search_score = search_result["summary"].get("quality_score") or {}
                    materialized_score = materialized["summary"].get("quality_score") or {}
                    for score_key in (
                        "not_choice_eligible", "choice_eligible", "unassigned",
                        "choice_excess_min", "near_deadline_risk_min",
                        "passenger_time_min", "deadhead_km", "total_vmt_km",
                        "finish_time_sum_min",
                    ):
                        before = float(search_score.get(score_key, 0.0) or 0.0)
                        after = float(materialized_score.get(score_key, 0.0) or 0.0)
                        if abs(before - after) > 1e-9:
                            raise RuntimeError(
                                "winner-only materialization quality mismatch "
                                f"for {score_key}: search={before}, full={after}"
                            )
                    expanded = dict(search_result)
                    expanded["events_path"] = materialized["events_path"]
                    expanded["req_updates"] = materialized["req_updates"]
                    expanded["veh_updates"] = materialized["veh_updates"]
                    expanded["route_plan"] = materialized["route_plan"]
                    materialized_results.append(expanded)
                application_results = materialized_results

            # Only the selected portfolio/global solution is materialized into
            # final vehicle state, request alternatives, and event CSVs.
            hour_final_updates: Dict[int, Dict[str, Any]] = {}
            for res in application_results:
                append_file(kpi_dir / "vehicle_events.csv", res["events_path"], skip_header=True)
                for vu in res["veh_updates"]:
                    vid = int(vu["veh_id"])
                    v = vehicles[vid]
                    v.finish_time_min = float(vu["finish_time_min"])
                    v.finish_lat = float(vu["finish_lat"])
                    v.finish_lon = float(vu["finish_lon"])
                    v.vmt_total_km += float(vu["vmt_total_km"])
                    v.vmt_deadhead_km += float(vu["vmt_deadhead_km"])
                    v.vmt_service_km += float(vu["vmt_service_km"])
                    v.time_wait_min += float(vu["time_wait_min"])
                    v.time_deadhead_min += float(vu["time_deadhead_min"])
                    v.time_service_min += float(vu["time_service_min"])
                    hour_vmt_total += float(vu["vmt_total_km"])
                    hour_vmt_dead += float(vu["vmt_deadhead_km"])
                    hour_vmt_serv += float(vu["vmt_service_km"])

                for ru in res["req_updates"]:
                    rid = int(ru["req_id"])
                    hour_final_updates[rid] = ru
                    req_meta = req_meta_by_id[rid]
                    _write_drt_alt(output, ru, req_meta)
                    req_result_by_id[rid] = ru
                    vid_str = str(ru["vehicle_id"]).strip()
                    if vid_str:
                        vid = int(vid_str)
                        vehicle_served_count[vid] = vehicle_served_count.get(vid, 0) + 1
                        hour_served += 1
                    hour_base += float(ru.get("obj_base", 0.0))
                    hour_pen += float(ru.get("obj_penalty", 0.0))

            for group in SOLVER_GROUP_LIST:
                req_list = requests_by_group.get(group, [])
                group_updates = [
                    hour_final_updates[int(req["req_id"])]
                    for req in req_list
                    if int(req["req_id"]) in hour_final_updates
                ]
                served_updates = [
                    update
                    for update in group_updates
                    if str(update.get("vehicle_id", "")).strip()
                ]
                choice_eligible = sum(
                    1
                    for update in served_updates
                    if float(update["dropoff_time_min"])
                    <= float(req_meta_by_id[int(update["req_id"])]["choice_deadline_min"])
                )
                append_csv_row(
                    metrics_dir / "final_group_kpi_hourly.csv",
                    [
                        hour, group[0].value, group[1].value,
                        solver_group_mode(group), solver_group_label(group),
                        len(req_list), len(served_updates), choice_eligible,
                        len(req_list) - len(served_updates),
                        sum(float(update.get("obj_total", 0.0)) for update in group_updates),
                        sum(float(update.get("obj_base", 0.0)) for update in group_updates),
                        sum(float(update.get("obj_penalty", 0.0)) for update in group_updates),
                    ],
                )

            for group in SOLVER_GROUP_LIST:
                if group in groups_ran:
                    continue
                req_list = requests_by_group.get(group, [])
                forward_candidates, reverse_candidates = _group_direction_counts(group, req_list)
                append_csv_row(
                    metrics_dir / "od_kpi_hourly.csv",
                    [
                        hour,
                        group[0].value,
                        group[1].value,
                        solver_group_mode(group),
                        solver_group_label(group),
                        len(req_list),
                        0,
                        group_targets.get(group, 0),
                        len(assignments.get(group, [])),
                        forward_candidates,
                        reverse_candidates,
                        0,
                        float(group_budget.get(group, 0.0)),
                        0.0,
                        0.0,
                        0.0,
                        0.0,
                        0.0,
                    ],
                )
                append_csv_row(
                    metrics_dir / "alns_trace.csv",
                    [hour, group[0].value, group[1].value, 1, 0.0, 0.0, 0.0, 0.0, 0.0, 0, 0, "NONE", "NONE", 0],
                )

            # ── Update prev_assignments for sticky rebalancing ──
            for group, veh_ids in assignments.items():
                for vid in veh_ids:
                    prev_assignments[vid] = group

            runtime_sec = time.time() - hour_start_clock
            append_csv_row(
                metrics_dir / "hour_kpi.csv",
                [
                    hour,
                    len(reqs_by_hour[hour]),
                    outside_excluded,
                    hour_served,
                    hour_obj_total,
                    hour_base,
                    hour_pen,
                    hour_vmt_total,
                    hour_vmt_dead,
                    hour_vmt_serv,
                    runtime_sec,
                ],
            )

        for r in reqs_all:
            rid = int(r["req_id"])
            ru = req_result_by_id.get(rid)
            if ru is None:
                vehicle_id = ""
                pickup_time_min = ""
                dropoff_time_min = ""
                drt_time_min = -1
                onboard_dist_km = None
                lateness_min = ""
                obj_base = ""
                obj_penalty = ""
                obj_total = ""
            else:
                v_id_str = str(ru["vehicle_id"]).strip()
                vehicle_id = int(v_id_str) if v_id_str else ""
                pickup_time_min = float(ru["pickup_time_min"])
                dropoff_time_min = float(ru["dropoff_time_min"])
                drt_time_min = float(ru["drt_time_min"])
                onboard_dist_km = float(ru["onboard_dist_km"])
                lateness_min = float(ru["lateness_min"])
                obj_base = float(ru["obj_base"])
                obj_penalty = float(ru["obj_penalty"])
                obj_total = float(ru["obj_total"])

            append_csv_row(
                kpi_dir / "requests.csv",
                [
                    rid,
                    r["agent_id"],
                    r["day"],
                    r["plan_index"],
                    r["trip_leg_id"],
                    r["call_time_min"],
                    r["pickup_lat"],
                    r["pickup_lon"],
                    r["drop_lat"],
                    r["drop_lon"],
                    r["o_region"].value,
                    r["d_region"].value,
                    r["hour"],
                    r["tt_pt_min"],
                    r["latest_min"],
                    vehicle_id,
                    pickup_time_min,
                    dropoff_time_min,
                    drt_time_min,
                    onboard_dist_km,
                    lateness_min,
                    obj_base,
                    obj_penalty,
                    obj_total,
                ],
            )

        for v in vehicles:
            v.served_count = vehicle_served_count.get(v.veh_id, 0)
            append_csv_row(
                kpi_dir / "vehicles.csv",
                [
                    v.veh_id,
                    v.cap,
                    v.start_time_min,
                    v.start_lat,
                    v.start_lon,
                    v.finish_time_min,
                    v.finish_lat,
                    v.finish_lon,
                    v.vmt_total_km,
                    v.vmt_deadhead_km,
                    v.vmt_service_km,
                    v.time_wait_min,
                    v.time_deadhead_min,
                    v.time_service_min,
                    v.served_count,
                ],
            )

        write_json_atomic(run_tmp / "output_withDRT.json", output, indent=None)

        end_stamp = now_stamp()
        wall_clock_runtime_sec = time.time() - run_wall_start
        run_config = dict(run_config_base)
        if shared_executor_factory is not None:
            run_config["reference_elapsed_sec"] = budget_elapsed()
            run_config["phase_budget_records"] = shared_executor_factory.clock.phases
            run_config["processed_hours"] = list(range(24))
        run_config["solver_travel_cache_stats"] = {
            **{key: int(value) for key, value in sorted(travel_cache_totals.items())},
            "max_reported_worker_size": int(travel_cache_max_size),
            "main_process_final": get_od_cache_stats(),
        }
        run_config["alns_operator_stats"] = {
            "counters": {
                key: int(value) for key, value in sorted(operator_totals.items())
            },
            "runtime_sec_by_operator": {
                key: float(value)
                for key, value in sorted(operator_runtime_totals.items())
            },
            "t0_runtime_total_sec": float(t0_runtime_total_sec),
            "t0_samples_observed_by_phase": {
                phase: {
                    str(samples): int(count)
                    for samples, count in sorted(histogram.items())
                }
                for phase, histogram in sorted(t0_samples_observed.items())
            },
        }
        run_config.update(
            {
                "run_end_stamp": end_stamp,
                "wall_clock_runtime_sec": wall_clock_runtime_sec,
                "status": "completed",
            }
        )
        write_json_atomic(metrics_dir / "run_config.json", run_config, indent=2)
        _write_run_parameters_md(run_tmp / "run_parameters.md", run_config)

        run_path = runs_base_dir / f"run_{start_stamp}__{end_stamp}"
        if run_path.exists():
            raise FileExistsError(f"run_path already exists: {run_path}")
        _finalize_run_directory(run_tmp, run_path)
        return run_path

    except Exception as exc:
        end_stamp = now_stamp()
        wall_clock_runtime_sec = time.time() - run_wall_start
        failed_run_path = _make_failed_run_path(runs_base_dir, start_stamp)
        failure_report_path = failed_run_path / "metrics" / "run_failure.json"
        failure_payload: Dict[str, Any] = {
            "status": "failed",
            "error_type": type(exc).__name__,
            "error_message": str(exc),
            "traceback": traceback.format_exc(),
            "stage0_dir": str(stage0_dir),
            "stage0_requests_csv": str(stage0_requests_csv),
            "run_start_stamp": start_stamp,
            "run_end_stamp": end_stamp,
            "wall_clock_runtime_sec": wall_clock_runtime_sec,
            "last_hour_started": current_hour,
            "failed_run_dir": str(failed_run_path),
            "failure_report_json": str(failure_report_path),
        }
        if isinstance(exc, SimulationSubproblemError):
            failure_payload["subproblem_diagnostics"] = exc.diagnostics

        write_json_atomic(metrics_dir / "run_failure.json", failure_payload, indent=2)

        failed_run_config = dict(run_config_base)
        if "travel_cache_totals" in locals():
            failed_run_config["solver_travel_cache_stats"] = {
                **{
                    key: int(value)
                    for key, value in sorted(travel_cache_totals.items())
                },
                "max_reported_worker_size": int(travel_cache_max_size),
                "main_process_final": get_od_cache_stats(),
            }
        if "operator_totals" in locals():
            failed_run_config["alns_operator_stats"] = {
                "counters": {
                    key: int(value)
                    for key, value in sorted(operator_totals.items())
                },
                "runtime_sec_by_operator": {
                    key: float(value)
                    for key, value in sorted(operator_runtime_totals.items())
                },
                "t0_runtime_total_sec": float(t0_runtime_total_sec),
                "t0_samples_observed_by_phase": {
                    phase: {
                        str(samples): int(count)
                        for samples, count in sorted(histogram.items())
                    }
                    for phase, histogram in sorted(t0_samples_observed.items())
                },
            }
        failed_run_config.update(
            {
                "run_end_stamp": end_stamp,
                "wall_clock_runtime_sec": wall_clock_runtime_sec,
                "status": "failed",
                "last_hour_started": current_hour,
                "failed_run_dir": str(failed_run_path),
                "failure_report_json": str(failure_report_path),
            }
        )
        write_json_atomic(metrics_dir / "run_config.json", failed_run_config, indent=2)
        _write_run_parameters_md(run_tmp / "run_parameters.md", failed_run_config)

        if run_tmp.exists():
            _finalize_run_directory(run_tmp, failed_run_path)
        raise SimulationRunFailed(
            f"Simulation failed; diagnostics saved to {failure_report_path}",
            failed_run_dir=failed_run_path,
            failure_report_path=failure_report_path,
        ) from exc
