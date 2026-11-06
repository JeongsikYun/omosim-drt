"""Deterministic portfolio/global-rescue integration checks.

This is intentionally a standalone test, consistent with the project's other
``scripts/test_*.py`` checks.  It exercises the real process-pool path on a
tiny two-group Stage0 fixture and writes all temporary artefacts below /tmp.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from drt_pipeline.constants import (  # noqa: E402
    ALNS_TRACE_HEADER,
    HOUR_KPI_HEADER,
    OD_KPI_HEADER,
    REQUESTS_HEADER,
    SOLVER_GROUP_INDEX,
    VEHICLE_EVENTS_HEADER,
    VEHICLES_HEADER,
    RegionId,
    canonical_solver_group,
)
from drt_pipeline.simulate import (  # noqa: E402
    _adaptive_global_rescue_plan,
    _execute_adaptive_global_rescue,
    _merge_route_plans,
    _next_service_hour_pressure,
    _portfolio_seed,
    _result_quality_key,
    run_simulation_from_stage0,
)


STAGE0_REQUEST_HEADER = REQUESTS_HEADER[:15]


def _assert_raises_value_error(fn, message_fragment: str) -> None:
    try:
        fn()
    except ValueError as exc:
        assert message_fragment in str(exc), (message_fragment, str(exc))
    else:
        raise AssertionError(f"expected ValueError containing {message_fragment!r}")


def _read_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), list(reader)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _quality_result(
    *,
    eligible: int,
    unassigned: int,
    excess: float,
    passenger_time: float,
    best_cost: float,
    finish_time: float = 500.0,
) -> dict:
    return {
        "summary": {
            "best_cost": best_cost,
            "quality_profile": True,
            "quality_score": {
                "not_choice_eligible": unassigned + int(excess > 0.0),
                "choice_eligible": eligible,
                "unassigned": unassigned,
                "choice_excess_min": excess,
                "near_deadline_risk_min": 0.0,
                "passenger_time_min": passenger_time,
                "deadhead_km": 1.0,
                "total_vmt_km": 2.0,
                "finish_time_sum_min": finish_time,
            },
        }
    }


def test_seed_and_completion_order() -> None:
    seed_base = 42
    hour = 17
    group = canonical_solver_group(RegionId.DJ_NE, RegionId.DJ_SE)
    legacy_seed = seed_base + hour * 1000 + SOLVER_GROUP_INDEX[group]
    assert _portfolio_seed(seed_base, hour, group, 0) == legacy_seed
    assert _portfolio_seed(seed_base, hour, group, 1) == legacy_seed + 1_000_003

    results = [
        (0, _quality_result(eligible=1, unassigned=1, excess=0.0, passenger_time=8.0, best_cost=1.0)),
        (1, _quality_result(eligible=2, unassigned=0, excess=0.0, passenger_time=12.0, best_cost=99.0)),
        (2, _quality_result(eligible=2, unassigned=0, excess=0.0, passenger_time=13.0, best_cost=0.5)),
    ]

    def winner(rows: list[tuple[int, dict]]) -> int:
        return min(rows, key=lambda item: _result_quality_key(item[1], item[0]))[0]

    # Simulate three different completion orders.  The lexicographic result,
    # not as_completed() arrival order or scalar legacy cost, must win.
    assert winner(results) == 1
    assert winner(list(reversed(results))) == 1
    assert winner([results[2], results[0], results[1]]) == 1

    tied = _quality_result(
        eligible=2, unassigned=0, excess=0.0, passenger_time=12.0, best_cost=99.0
    )
    assert winner([(3, tied), (0, tied), (2, tied)]) == 0

    # Portfolio/global selection must use the exact same field ordering as
    # alns_sa.SolutionScore: choice excess precedes the unassigned tie-break.
    late_assigned = _quality_result(
        eligible=0, unassigned=0, excess=10.0, passenger_time=10.0, best_cost=1.0
    )
    cleanly_unassigned = _quality_result(
        eligible=0, unassigned=1, excess=0.0, passenger_time=0.0, best_cost=99.0
    )
    assert winner([(0, late_assigned), (1, cleanly_unassigned)]) == 1

    # The final deterministic SolutionScore field must also precede replica id.
    later_finish = _quality_result(
        eligible=2,
        unassigned=0,
        excess=0.0,
        passenger_time=12.0,
        best_cost=1.0,
        finish_time=510.0,
    )
    earlier_finish = _quality_result(
        eligible=2,
        unassigned=0,
        excess=0.0,
        passenger_time=12.0,
        best_cost=1.0,
        finish_time=500.0,
    )
    assert winner([(0, later_finish), (1, earlier_finish)]) == 1


def test_route_merge_duplicate_guards() -> None:
    first = {"route_plan": {0: [("PU", 10), ("DO", 10)]}}
    second = {"route_plan": {1: [("PU", 20), ("DO", 20)]}}
    assert _merge_route_plans([first, second]) == {
        0: [("PU", 10), ("DO", 10)],
        1: [("PU", 20), ("DO", 20)],
    }

    _assert_raises_value_error(
        lambda: _merge_route_plans(
            [first, {"route_plan": {0: [("PU", 20), ("DO", 20)]}}]
        ),
        "vehicle 0 appears",
    )
    _assert_raises_value_error(
        lambda: _merge_route_plans(
            [first, {"route_plan": {1: [("PU", 10), ("DO", 10)]}}]
        ),
        "requests duplicated",
    )


def _policy_result(
    *,
    misses: int,
    unassigned: int,
    passenger_time: float,
    near_deadline_risk: float = 0.0,
    route_request: int = 1,
) -> dict:
    result = _quality_result(
        eligible=max(0, 10 - misses),
        unassigned=unassigned,
        excess=float(max(0, misses - unassigned)),
        passenger_time=passenger_time,
        best_cost=passenger_time,
    )
    result["summary"]["candidates"] = 10
    result["summary"]["quality_score"].update(
        {
            "not_choice_eligible": misses,
            "choice_eligible": max(0, 10 - misses),
            "unassigned": unassigned,
            "choice_excess_min": float(max(0, misses - unassigned)),
            "near_deadline_risk_min": near_deadline_risk,
        }
    )
    result["route_plan"] = {0: [("PU", route_request), ("DO", route_request)]}
    return result


def test_adaptive_policy_and_full_fallback() -> None:
    hard = _policy_result(misses=5, unassigned=5, passenger_time=100.0)
    hard_plan = _adaptive_global_rescue_plan(hard, full_replicas=4)
    assert hard_plan["tier"] == "full"
    assert hard_plan["initial_replicas"] == 4
    hard_calls: list[tuple[str, int, float, dict, int]] = []

    def hard_wave(stage, replicas, budget, routes, seed_generation):
        hard_calls.append((stage, replicas, budget, routes, seed_generation))
        return {"results": [(0, hard)], "errors": []}

    hard_execution = _execute_adaptive_global_rescue(
        hard,
        full_replicas=4,
        total_budget_sec=100.0,
        run_wave=hard_wave,
        compact_polish_requested=True,
        compact_polish_effective=True,
        downstream_pressure=0.0,
    )
    assert hard_calls == [("full", 4, 100.0, hard["route_plan"], 0)]
    assert hard_execution["accepted_stage"] == "full"
    near_deadline = _policy_result(
        misses=2,
        unassigned=1,
        passenger_time=100.0,
        near_deadline_risk=5.0,
    )
    assert _adaptive_global_rescue_plan(
        near_deadline, full_replicas=4
    )["tier"] == "full"

    initial = _policy_result(
        misses=2, unassigned=2, passenger_time=100.0, route_request=1
    )
    targeted = _policy_result(
        misses=1, unassigned=1, passenger_time=95.0, route_request=2
    )
    resolved = _policy_result(
        misses=0, unassigned=0, passenger_time=110.0, route_request=3
    )
    calls: list[tuple[str, int, float, dict, int]] = []

    def fallback_wave(stage, replicas, budget, routes, seed_generation):
        calls.append((stage, replicas, budget, routes, seed_generation))
        result = targeted if stage == "targeted" else resolved
        return {
            "results": [(0, result)],
            "errors": [],
            "wall_runtime_sec": budget / 10.0,
            "solver_runtime_sum_sec": budget / 10.0,
        }

    execution = _execute_adaptive_global_rescue(
        initial,
        full_replicas=4,
        total_budget_sec=100.0,
        run_wave=fallback_wave,
        compact_polish_requested=True,
        compact_polish_effective=True,
        downstream_pressure=0.0,
    )
    assert [call[0] for call in calls] == ["targeted", "full_fallback"]
    assert calls[0][1:3] == (1, 35.0)
    assert calls[1][1:3] == (4, 100.0)
    # Both stages restart from the original local plan; the fallback therefore
    # retains the historical warm start regardless of the targeted trajectory.
    assert calls[0][3] == initial["route_plan"]
    assert calls[1][3] == initial["route_plan"]
    assert calls[0][4] == calls[1][4] == 0
    assert execution["fallback_triggered"] is True
    assert execution["accepted_stage"] == "full_fallback"
    assert execution["winner"] is resolved
    assert execution["stages"][0]["improved"] is True
    assert execution["stages"][1]["improved"] is True

    failed_calls: list[str] = []

    def failed_target_wave(stage, replicas, budget, routes, seed_generation):
        failed_calls.append(stage)
        if stage == "targeted":
            raise RuntimeError("synthetic targeted failure")
        return {"results": [(0, resolved)], "errors": []}

    recovered = _execute_adaptive_global_rescue(
        initial,
        full_replicas=4,
        total_budget_sec=100.0,
        run_wave=failed_target_wave,
    )
    assert failed_calls == ["targeted", "full_fallback"]
    assert recovered["fallback_triggered"] is True
    assert recovered["winner"] is resolved


def test_adaptive_progressive_polish_stops_without_improvement() -> None:
    initial = _policy_result(
        misses=0, unassigned=0, passenger_time=100.0, route_request=10
    )
    improved = _policy_result(
        misses=0, unassigned=0, passenger_time=90.0, route_request=11
    )
    regressed = _policy_result(
        misses=0, unassigned=0, passenger_time=95.0, route_request=12
    )
    calls: list[tuple[str, int, float, dict, int]] = []

    def polish_wave(stage, replicas, budget, routes, seed_generation):
        calls.append((stage, replicas, budget, routes, seed_generation))
        result = improved if stage == "polish_1" else regressed
        return {"results": [(0, result)], "errors": []}

    execution = _execute_adaptive_global_rescue(
        initial,
        full_replicas=4,
        total_budget_sec=100.0,
        run_wave=polish_wave,
    )
    assert [call[0] for call in calls] == ["polish_1", "polish_2"]
    assert calls[0][1:3] == (1, 20.0)
    assert calls[1][1:3] == (1, 35.0)
    assert calls[1][3] == improved["route_plan"]
    assert calls[0][4] == 0 and calls[1][4] == 1
    assert execution["accepted_stage"] == "polish_1"
    assert execution["winner"] is improved
    assert execution["stages"][0]["accepted"] is True
    assert execution["stages"][1]["accepted"] is False
    assert execution["fallback_triggered"] is False


def test_adaptive_compact_polish_pressure_policy() -> None:
    def run_policy(*, compact_requested: bool, compact_effective: bool, pressure: float):
        initial = _policy_result(
            misses=0, unassigned=0, passenger_time=100.0, route_request=20
        )
        candidates = [
            _policy_result(
                misses=0,
                unassigned=0,
                passenger_time=passenger_time,
                route_request=route_request,
            )
            for passenger_time, route_request in ((90.0, 21), (80.0, 22), (70.0, 23))
        ]
        calls: list[tuple[str, int, float, dict, int]] = []

        def improving_wave(stage, replicas, budget, routes, seed_generation):
            calls.append((stage, replicas, budget, routes, seed_generation))
            return {"results": [(0, candidates[len(calls) - 1])], "errors": []}

        execution = _execute_adaptive_global_rescue(
            initial,
            full_replicas=4,
            total_budget_sec=100.0,
            run_wave=improving_wave,
            compact_polish_requested=compact_requested,
            compact_polish_effective=compact_effective,
            downstream_pressure=pressure,
            compact_pressure_threshold=3.0,
        )
        return initial, candidates, calls, execution

    initial, candidates, default_calls, default_execution = run_policy(
        compact_requested=False,
        compact_effective=False,
        pressure=0.0,
    )
    assert [call[:3] for call in default_calls] == [
        ("polish_1", 1, 20.0),
        ("polish_2", 1, 35.0),
        ("polish_3", 2, 45.0),
    ]
    assert [call[4] for call in default_calls] == [0, 1, 2]
    assert [call[3] for call in default_calls] == [
        initial["route_plan"],
        candidates[0]["route_plan"],
        candidates[1]["route_plan"],
    ]
    assert default_execution["plan"]["max_polish_stages"] == 3

    _, _, inactive_calls, inactive_execution = run_policy(
        compact_requested=True,
        compact_effective=False,
        pressure=0.0,
    )
    assert [call[0] for call in inactive_calls] == [
        "polish_1", "polish_2", "polish_3"
    ]
    assert inactive_execution["plan"]["compact_polish_requested"] is True
    assert inactive_execution["plan"]["compact_polish_effective"] is False

    _, _, low_calls, low_execution = run_policy(
        compact_requested=True,
        compact_effective=True,
        pressure=2.999,
    )
    assert [call[:3] for call in low_calls] == [("polish_1", 1, 20.0)]
    low_plan = low_execution["plan"]
    assert low_plan["compact_polish_requested"] is True
    assert low_plan["compact_polish_effective"] is True
    assert low_plan["downstream_pressure"] == 2.999
    assert low_plan["compact_pressure_threshold"] == 3.0
    assert low_plan["max_polish_stages"] == 1
    assert "low" in low_plan["reason"]

    high_initial, high_candidates, high_calls, high_execution = run_policy(
        compact_requested=True,
        compact_effective=True,
        pressure=3.0,
    )
    assert [call[:3] for call in high_calls] == [
        ("polish_1", 1, 20.0),
        ("polish_2", 1, 35.0),
        ("polish_3", 2, 45.0),
    ]
    assert [call[4] for call in high_calls] == [0, 1, 2]
    assert [call[3] for call in high_calls] == [
        high_initial["route_plan"],
        high_candidates[0]["route_plan"],
        high_candidates[1]["route_plan"],
    ]
    assert high_execution["plan"]["max_polish_stages"] == 3
    assert "high" in high_execution["plan"]["reason"]

    # Absolute hour labels do not affect the decision; only next-service-hour
    # demand per vehicle does. The final service hour always has zero pressure.
    morning_pressure = _next_service_hour_pressure(
        hour=7,
        service_hour_end=20,
        inside_demand_by_hour={8: 150},
        fleet_size=50,
    )
    evening_pressure = _next_service_hour_pressure(
        hour=17,
        service_hour_end=20,
        inside_demand_by_hour={18: 150},
        fleet_size=50,
    )
    assert morning_pressure == evening_pressure == 3.0
    hour_invariant_initial = _policy_result(
        misses=0, unassigned=0, passenger_time=100.0, route_request=30
    )
    morning_plan = _adaptive_global_rescue_plan(
        hour_invariant_initial,
        full_replicas=4,
        compact_polish_requested=True,
        compact_polish_effective=True,
        downstream_pressure=morning_pressure,
        compact_pressure_threshold=3.0,
    )
    evening_plan = _adaptive_global_rescue_plan(
        hour_invariant_initial,
        full_replicas=4,
        compact_polish_requested=True,
        compact_polish_effective=True,
        downstream_pressure=evening_pressure,
        compact_pressure_threshold=3.0,
    )
    assert morning_plan == evening_plan
    assert _next_service_hour_pressure(
        hour=20,
        service_hour_end=20,
        inside_demand_by_hour={21: 999},
        fleet_size=50,
    ) == 0.0


def _trip(agent_id: int, leg_id: int) -> dict:
    return {
        "type": "Trip",
        "legID": leg_id,
        "mode": "PUBLIC_TRANSIT",
        "alternativeModes": [
            {
                "mode": "PUBLIC_TRANSIT",
                "timeMinute": 40.0,
                "distanceKilometer": 5.0,
                "selected": True,
            },
            {
                "mode": "DRT",
                "timeMinute": -1.0,
                "distanceKilometer": 0.0,
                "selected": False,
            },
        ],
        "testAgent": agent_id,
    }


def _write_tiny_stage0(stage0_dir: Path) -> None:
    stage0_dir.mkdir(parents=True)
    rows = [
        {
            "req_id": 10,
            "agent_id": 101,
            "day": 1,
            "plan_index": 0,
            "trip_leg_id": 1,
            "call_time_min": 480.0,
            "pickup_lat": 36.2750,
            "pickup_lon": 127.4600,
            "drop_lat": 36.2780,
            "drop_lon": 127.4630,
            "o_region": "DJ_SE",
            "d_region": "DJ_SE",
            "hour": 8,
            "tt_pt_min": 40.0,
            "latest_min": 520.0,
        },
        {
            "req_id": 20,
            "agent_id": 202,
            "day": 1,
            "plan_index": 0,
            "trip_leg_id": 1,
            "call_time_min": 482.0,
            "pickup_lat": 36.4250,
            "pickup_lon": 127.4600,
            "drop_lat": 36.4220,
            "drop_lon": 127.4570,
            "o_region": "DJ_NE",
            "d_region": "DJ_NE",
            "hour": 8,
            "tt_pt_min": 40.0,
            "latest_min": 522.0,
        },
    ]
    with (stage0_dir / "stage0_requests.csv").open(
        "w", newline="", encoding="utf-8"
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=STAGE0_REQUEST_HEADER)
        writer.writeheader()
        writer.writerows(rows)

    stub = {
        "agents": [
            {
                "id": 101,
                "mobilityGroup": "PUBLIC_TRANSIT_USER",
                "mobilityDemand": [{"day": 1, "plan": [_trip(101, 1)]}],
            },
            {
                "id": 202,
                "mobilityGroup": "PUBLIC_TRANSIT_USER",
                "mobilityDemand": [{"day": 1, "plan": [_trip(202, 1)]}],
            },
        ]
    }
    with (stage0_dir / "output_with_drt_stub.json").open(
        "w", encoding="utf-8"
    ) as handle:
        json.dump(stub, handle)


def test_tiny_end_to_end() -> None:
    with tempfile.TemporaryDirectory(prefix="drt_quality_portfolio_") as raw_tmp:
        root = Path(raw_tmp)
        stage0_dir = root / "stage0"
        runs_dir = root / "runs"
        runs_dir.mkdir()
        _write_tiny_stage0(stage0_dir)

        run_dir = run_simulation_from_stage0(
            stage0_dir=stage0_dir,
            runs_base_dir=runs_dir,
            workers=4,
            global_time_limit_sec=10.0,
            fleet_size=2,
            cap=2,
            alns_iter_cap=30,
            init_strategy="fast",
            init_multistart=1,
            init_regret_k=2,
            init_vehicle_top_k=2,
            init_pos_samples=16,
            init_candidate_pool=4,
            init_time_limit_sec=0.0,
            speed_kmph=30.0,
            rejection_penalty=100.0,
            service_hour_start=8,
            service_hour_end=8,
            rebalancing_strategy="default",
            seed_base=42,
            alns_quality_profile=True,
            quality_regret_k=3,
            quality_unassigned_repair_limit=2,
            quality_reheat_interval_iters=10,
            quality_max_reheats=1,
            portfolio_replicas=2,
            global_rescue=True,
            global_rescue_replicas=2,
            global_rescue_time_fraction=0.25,
        )

        expected_headers = {
            run_dir / "metrics" / "alns_trace.csv": ALNS_TRACE_HEADER,
            run_dir / "metrics" / "hour_kpi.csv": HOUR_KPI_HEADER,
            run_dir / "metrics" / "od_kpi_hourly.csv": OD_KPI_HEADER,
            run_dir / "kpi" / "requests.csv": REQUESTS_HEADER,
            run_dir / "kpi" / "vehicles.csv": VEHICLES_HEADER,
            run_dir / "kpi" / "vehicle_events.csv": VEHICLE_EVENTS_HEADER,
        }
        for path, expected in expected_headers.items():
            header, _ = _read_rows(path)
            assert header == expected, (path.name, header, expected)

        _, requests = _read_rows(run_dir / "kpi" / "requests.csv")
        _, vehicles = _read_rows(run_dir / "kpi" / "vehicles.csv")
        _, events = _read_rows(run_dir / "kpi" / "vehicle_events.csv")
        assert len(requests) == 2
        assert len(vehicles) == 2
        assert {int(row["req_id"]) for row in requests} == {10, 20}
        assert {int(row["veh_id"]) for row in vehicles} == {0, 1}

        request_by_id = {int(row["req_id"]): row for row in requests}
        served = {
            rid: row for rid, row in request_by_id.items() if row["vehicle_id"].strip()
        }
        assert len(served) == 2, requests

        event_type_counts: dict[int, Counter[str]] = defaultdict(Counter)
        event_vehicle: dict[int, set[int]] = defaultdict(set)
        for row in events:
            rid = int(row["req_id"])
            event_type_counts[rid][row["event_type"]] += 1
            event_vehicle[rid].add(int(row["veh_id"]))
        for rid, row in served.items():
            assert event_type_counts[rid]["PICKUP"] == 1
            assert event_type_counts[rid]["DROPOFF"] == 1
            assert event_vehicle[rid] == {int(row["vehicle_id"])}

        served_by_vehicle = Counter(int(row["vehicle_id"]) for row in served.values())
        for row in vehicles:
            assert int(row["served_count"]) == served_by_vehicle[int(row["veh_id"])]

        _, hour_rows = _read_rows(run_dir / "metrics" / "hour_kpi.csv")
        hour8 = next(row for row in hour_rows if int(row["hour"]) == 8)
        assert int(hour8["served_in_hour"]) == len(served)
        assert math.isclose(
            float(hour8["obj_total"]),
            float(hour8["obj_base"]) + float(hour8["obj_penalty"]),
            rel_tol=1e-12,
            abs_tol=1e-12,
        )
        vehicle_total_vmt = sum(float(row["vmt_total_km"]) for row in vehicles)
        assert math.isclose(
            float(hour8["vmt_total_km"]), vehicle_total_vmt, rel_tol=1e-9, abs_tol=1e-9
        )

        final_group_header, final_group_rows = _read_rows(
            run_dir / "metrics" / "final_group_kpi_hourly.csv"
        )
        assert final_group_header == [
            "hour", "o_region", "d_region", "group_mode", "group_label",
            "candidates", "served", "choice_eligible", "unassigned",
            "obj_total", "obj_base", "obj_penalty",
        ]
        final_h8 = [row for row in final_group_rows if int(row["hour"]) == 8]
        assert sum(int(row["candidates"]) for row in final_h8) == 2
        assert sum(int(row["served"]) for row in final_h8) == len(served)
        assert math.isclose(
            sum(float(row["obj_total"]) for row in final_h8),
            float(hour8["obj_total"]),
            rel_tol=1e-12,
            abs_tol=1e-12,
        )

        config = json.loads(
            (run_dir / "metrics" / "run_config.json").read_text(encoding="utf-8")
        )
        assert config["stage0_requests_sha256"] == _sha256(
            stage0_dir / "stage0_requests.csv"
        )
        assert config["stage0_stub_sha256"] == _sha256(
            stage0_dir / "output_with_drt_stub.json"
        )
        assert config["sa_stagnation_patience_iters_effective"] == 0
        assert config["summary_cost_semantics"].startswith("legacy_objective")
        assert config["adaptive_global_rescue"] is False
        assert config["adaptive_global_rescue_effective"] is False
        assert config["adaptive_global_rescue_compact_polish_requested"] is False
        assert config["adaptive_global_rescue_compact_polish_effective"] is False
        assert config["adaptive_global_rescue_compact_pressure_threshold"] == 3.0
        assert config["adaptive_global_rescue_compact_low_pressure_max_stages"] == 1
        assert config["adaptive_global_rescue_compact_high_pressure_max_stages"] == 3
        assert config["solver_travel_cache_requested"] is None
        assert config["solver_travel_cache_effective"] is True
        assert config["winner_only_materialization_requested"] is None
        assert config["winner_only_materialization_effective"] is True
        assert config["alns_noop_operator_adaptation"] is False
        assert config["alns_t0_samples"] == 0
        assert config["alns_t0_samples_effective_policy"].startswith("auto:")
        assert config["global_rescue_t0_samples_effective_override"] is None
        assert config["global_rescue_t0_samples_effective_policy"].startswith(
            "auto:"
        )
        assert "alns_operator_stats" in config
        assert config["alns_operator_stats"]["t0_samples_observed_by_phase"][
            "local"
        ]
        parameters_text = (run_dir / "run_parameters.md").read_text(
            encoding="utf-8"
        )
        for setting in (
            "solver_travel_cache_effective",
            "winner_only_materialization_effective",
            "alns_noop_operator_adaptation",
            "adaptive_global_rescue_effective",
            "adaptive_global_rescue_compact_polish_requested",
            "adaptive_global_rescue_compact_polish_effective",
            "adaptive_global_rescue_compact_pressure_threshold",
            "adaptive_global_rescue_compact_low_pressure_max_stages",
            "adaptive_global_rescue_compact_high_pressure_max_stages",
        ):
            assert setting in parameters_text

        operator_header, operator_rows = _read_rows(
            run_dir / "metrics" / "alns_operator_diagnostics.csv"
        )
        assert operator_header == [
            "phase", "hour", "o_region", "d_region", "replica", "selected",
            "accepted_stage", "operator_kind", "operator", "calls", "no_ops",
            "accepted", "improved_current", "best_improved", "runtime_sec",
            "final_weight", "t0_runtime_sec", "t0_samples_effective", "pid",
        ]
        assert operator_rows
        assert any(row["operator_kind"] == "meta" for row in operator_rows)

        cache_header, cache_rows = _read_rows(
            run_dir / "metrics" / "travel_cache_diagnostics.csv"
        )
        assert cache_header == [
            "phase", "hour", "o_region", "d_region", "replica", "selected",
            "pid", "backing_hits", "canonical_hits", "legacy_key_hits",
            "backing_misses", "fallbacks", "lookup_errors", "memory_hits",
            "memory_misses", "memory_hit_rate", "memory_size",
            "memory_max_entries", "memory_evictions",
        ]
        assert cache_rows

        adaptive_header, adaptive_rows = _read_rows(
            run_dir / "metrics" / "adaptive_global_rescue.csv"
        )
        assert "allocated_budget_sec" in adaptive_header
        assert adaptive_rows == []

        _, portfolio = _read_rows(run_dir / "metrics" / "alns_portfolio.csv")
        portfolio_h8 = [row for row in portfolio if int(row["hour"]) == 8]
        assert len(portfolio_h8) == 4, portfolio_h8
        by_group: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
        for row in portfolio_h8:
            assert row["status"] == "ok"
            by_group[(row["o_region"], row["d_region"])].append(row)
        assert len(by_group) == 2
        for (o_region, d_region), rows in by_group.items():
            assert {int(row["replica"]) for row in rows} == {0, 1}
            assert sum(int(row["selected"]) for row in rows) == 1
            group = canonical_solver_group(RegionId(o_region), RegionId(d_region))
            replica0 = next(row for row in rows if int(row["replica"]) == 0)
            assert int(replica0["seed"]) == _portfolio_seed(42, 8, group, 0)

        _, rescue = _read_rows(run_dir / "metrics" / "global_rescue.csv")
        rescue_h8 = next(row for row in rescue if int(row["hour"]) == 8)
        assert rescue_h8["status"] == "ok", rescue_h8
        assert int(rescue_h8["replicas"]) == 2
        assert int(rescue_h8["selected_replica"]) in {0, 1}

        with (run_dir / "output_withDRT.json").open(encoding="utf-8") as handle:
            output = json.load(handle)
        output_drt_times = {}
        for agent in output["agents"]:
            trip = agent["mobilityDemand"][0]["plan"][0]
            drt = next(alt for alt in trip["alternativeModes"] if alt["mode"] == "DRT")
            output_drt_times[int(agent["id"])] = float(drt["timeMinute"])
        for row in served.values():
            assert math.isclose(
                output_drt_times[int(row["agent_id"])],
                float(row["drt_time_min"]),
                rel_tol=1e-12,
                abs_tol=1e-12,
            )


def test_tiny_adaptive_end_to_end() -> None:
    with tempfile.TemporaryDirectory(prefix="drt_adaptive_rescue_") as raw_tmp:
        root = Path(raw_tmp)
        stage0_dir = root / "stage0"
        runs_dir = root / "runs"
        runs_dir.mkdir()
        _write_tiny_stage0(stage0_dir)

        run_dir = run_simulation_from_stage0(
            stage0_dir=stage0_dir,
            runs_base_dir=runs_dir,
            workers=2,
            global_time_limit_sec=8.0,
            fleet_size=2,
            cap=2,
            alns_iter_cap=20,
            init_strategy="fast",
            init_multistart=1,
            init_regret_k=2,
            init_vehicle_top_k=2,
            init_pos_samples=16,
            init_candidate_pool=4,
            speed_kmph=30.0,
            rejection_penalty=100.0,
            service_hour_start=8,
            service_hour_end=8,
            seed_base=42,
            alns_quality_profile=True,
            portfolio_replicas=1,
            global_rescue=True,
            global_rescue_replicas=2,
            global_rescue_time_fraction=0.25,
            adaptive_global_rescue=True,
            adaptive_global_rescue_compact_polish=True,
            adaptive_global_rescue_compact_pressure_threshold=3.0,
        )

        config = json.loads(
            (run_dir / "metrics" / "run_config.json").read_text(encoding="utf-8")
        )
        assert config["adaptive_global_rescue"] is True
        assert config["adaptive_global_rescue_effective"] is True
        assert config["adaptive_global_rescue_compact_polish_requested"] is True
        assert config["adaptive_global_rescue_compact_polish_effective"] is True
        assert config["adaptive_global_rescue_compact_pressure_threshold"] == 3.0
        parameters_text = (run_dir / "run_parameters.md").read_text(
            encoding="utf-8"
        )
        assert "adaptive_global_rescue_compact_polish_effective" in parameters_text

        _, adaptive_rows = _read_rows(
            run_dir / "metrics" / "adaptive_global_rescue.csv"
        )
        hour8 = [row for row in adaptive_rows if int(row["hour"]) == 8]
        assert hour8
        assert {row["tier"] for row in hour8} == {"progressive_polish"}
        assert len(hour8) == 1
        assert "low" in hour8[0]["reason"]
        assert all(row["fallback_triggered"] == "0" for row in hour8)
        assert [row["stage"] for row in hour8] == [
            f"polish_{idx}" for idx in range(1, len(hour8) + 1)
        ]
        for prior in hour8[:-1]:
            assert prior["improved"] == "1", hour8

        _, rescue_rows = _read_rows(run_dir / "metrics" / "global_rescue.csv")
        rescue_h8 = next(row for row in rescue_rows if int(row["hour"]) == 8)
        assert rescue_h8["status"] == "ok"

        _, requests = _read_rows(run_dir / "kpi" / "requests.csv")
        assert sum(bool(row["vehicle_id"].strip()) for row in requests) == 2


def main() -> None:
    test_seed_and_completion_order()
    test_route_merge_duplicate_guards()
    test_adaptive_policy_and_full_fallback()
    test_adaptive_progressive_polish_stops_without_improvement()
    test_adaptive_compact_polish_pressure_policy()
    test_tiny_end_to_end()
    test_tiny_adaptive_end_to_end()
    print("quality portfolio/global-rescue tests: PASS")


if __name__ == "__main__":
    main()
