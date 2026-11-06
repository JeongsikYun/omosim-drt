"""ALNS + SA 기반 OD 서브문제 해결(Worker용).

핵심 요구사항 반영:
- Pickup/Dropoff가 있는 DARP(공유승차) 형태
- 계산시간 우선: 단순 초기해 + 작은 연산자 세트(Random/Shaw/Worst + Greedy/Regret2)
- Acceptance: Simulated Annealing(SA)
- 로그: alns_trace.csv는 (hour,OD)별 전 iter 저장
- vehicle_events.csv: WAIT/PICKUP/DROPOFF만 기록 + 이동은 합계로 이벤트에 부착
- hard constraints:
  - capacity <= 15
  - pickup_time >= call_time
  - finish_time <= 1800 (30h hard cap)
  - reject 금지(모든 요청 서빙)

주의:
- Option A: 한 OD 서브문제는 해당 OD 요청만 처리 (차량/요청 교차 없음)
- 워커 프로세스는 파일에 trace/events를 쓰고, 메인 프로세스가 합친다.
"""

from __future__ import annotations

import csv
import math
import random
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Deque, Dict, List, Optional, Set, Tuple

import contextlib

from .constants import ALNS_TRACE_HEADER, VEHICLE_EVENTS_HEADER, RegionId, solver_group_label, solver_group_mode
from .travel import (
    get_od_cache_stats,
    process_memory_cache_enabled,
    reset_od_cache_stats,
    set_speed_kmph,
    travel,
)
HORIZON_MIN = 1800.0  # 30h
REJECTION_PENALTY = 300.0  # 승객 1명 미배정 시 부과되는 패널티 (지연 방지용)

# Worker 프로세스마다 solve_od_alns_sa가 한 번에 하나의 서브문제를 실행한다.
# 아래 플래그는 기존 REJECTION_PENALTY와 같은 방식으로 해당 worker의 알고리즘
# 경로를 설정하며, run_config에도 동일한 값이 기록된다.
_SELECTIVE_ROUTE_REEVALUATION = True
_LIGHTWEIGHT_EVAL_ONLY = True
_TRUE_REGRET2 = True
_QUALITY_OBJECTIVE = False
_CHOICE_MISS_PENALTY = 10000.0
_CHOICE_EXCESS_WEIGHT = 25.0
_NEAR_DEADLINE_BUFFER_MIN = 1.0
_NEAR_DEADLINE_WEIGHT = 3.0
_DEADHEAD_WEIGHT = 0.15
_TOTAL_VMT_WEIGHT = 0.02


@dataclass(frozen=True, order=True)
class SolutionScore:
    """Lexicographic quality score used for the best-solution archive.

    Stage2-eligible DRT trips dominate every secondary KPI.  The scalar SA
    energy remains available as ``Solution.total_cost`` so the search can
    temporarily cross worse states without weakening the best archive.
    """

    not_choice_eligible: int
    choice_excess_min: float
    unassigned: int
    near_deadline_risk_min: float
    passenger_time_min: float
    deadhead_km: float
    total_vmt_km: float
    finish_time_sum_min: float

def _open_with_retry(path, mode="a", retries=10, delay=0.3, **kwargs):
    """OneDrive/Windows Defender 파일 잠금으로 인한 PermissionError를 재시도로 우회."""
    import time as _time
    last_exc = None
    for attempt in range(retries):
        try:
            return open(path, mode, **kwargs)
        except PermissionError as exc:
            last_exc = exc
            _time.sleep(delay * (attempt + 1))
    raise last_exc

@dataclass
class Req:
    req_id: int
    call_time_min: float
    pickup_lat: float
    pickup_lon: float
    drop_lat: float
    drop_lon: float
    latest_min: float
    # Absolute drop-off deadline implied by the exact Stage2 mode-choice rule.
    # Older callers/tests may omit it; PT latest_min is then used.
    choice_deadline_min: Optional[float] = None

@dataclass
class Veh:
    veh_id: int
    cap: int
    start_time_min: float
    start_lat: float
    start_lon: float

@dataclass
class StopNode:
    kind: str  # 'PU' or 'DO'
    req_id: int
    lat: float
    lon: float

@dataclass
class RouteEval:
    feasible: bool
    obj_total: float
    obj_base: float
    obj_penalty: float
    lateness_sum: float

    finish_time_min: float
    finish_lat: float
    finish_lon: float

    vmt_total_km: float
    vmt_deadhead_km: float
    vmt_service_km: float
    time_wait_min: float
    time_deadhead_min: float
    time_service_min: float

    pickup_time: Dict[int, float]
    dropoff_time: Dict[int, float]
    onboard_dist: Dict[int, float]
    req_obj: Dict[int, float]
    req_lateness: Dict[int, float]

    events: List[List]

    # Search-only aggregates.  They are intentionally not written into the
    # historical request/vehicle CSV schemas.
    search_cost: float = 0.0
    choice_miss_count: int = 0
    choice_excess_min: float = 0.0
    near_deadline_risk_min: float = 0.0
    passenger_time_min: float = 0.0
    search_deadhead_km: float = 0.0
    search_total_vmt_km: float = 0.0

@dataclass
class Solution:
    routes: Dict[int, List[StopNode]]
    unassigned: Set[int]
    route_eval: Dict[int, RouteEval]
    total_cost: float


def _choice_deadline(req: Req) -> float:
    deadline = req.choice_deadline_min
    return req.latest_min if deadline is None or not math.isfinite(deadline) else deadline


def _unassigned_search_cost(count: int) -> float:
    if not _QUALITY_OBJECTIVE:
        return count * REJECTION_PENALTY
    return count * (_CHOICE_MISS_PENALTY + REJECTION_PENALTY)


def solution_score(sol: Solution) -> SolutionScore:
    """Return the deterministic lexicographic score for result selection."""
    route_evals = sol.route_eval.values()
    misses = sum(ev.choice_miss_count for ev in route_evals)
    return SolutionScore(
        not_choice_eligible=len(sol.unassigned) + misses,
        choice_excess_min=sum(ev.choice_excess_min for ev in sol.route_eval.values()),
        unassigned=len(sol.unassigned),
        near_deadline_risk_min=sum(ev.near_deadline_risk_min for ev in sol.route_eval.values()),
        passenger_time_min=sum(ev.passenger_time_min for ev in sol.route_eval.values()),
        deadhead_km=sum(ev.search_deadhead_km for ev in sol.route_eval.values()),
        total_vmt_km=sum(ev.search_total_vmt_km for ev in sol.route_eval.values()),
        finish_time_sum_min=sum(ev.finish_time_min for ev in sol.route_eval.values()),
    )


def _is_better_solution(candidate: Solution, incumbent: Solution) -> bool:
    if _QUALITY_OBJECTIVE:
        return solution_score(candidate) < solution_score(incumbent)
    return candidate.total_cost < incumbent.total_cost


def _solution_order_key(sol: Solution) -> Tuple:
    """Deterministic ordering shared by insertion selection and regret-k."""
    if _QUALITY_OBJECTIVE:
        return (solution_score(sol), sol.total_cost, _solution_signature(sol))
    return (sol.total_cost, _solution_signature(sol))


def _quality_regret_gap(best: Solution, alternative: Solution) -> float:
    """Map the first lexicographic quality loss to a positive regret gap.

    The mapping is used only to rank requests in regret repair.  It does not
    replace the lexicographic archive or the scalar SA energy.  Considering
    only the first differing score component preserves the quality ordering
    and prevents lower-priority distance terms from cancelling a choice loss.
    """
    best_score = solution_score(best)
    alt_score = solution_score(alternative)
    weighted_fields = (
        ("not_choice_eligible", _CHOICE_MISS_PENALTY),
        ("choice_excess_min", _CHOICE_EXCESS_WEIGHT),
        ("unassigned", _unassigned_search_cost(1)),
        ("near_deadline_risk_min", _NEAR_DEADLINE_WEIGHT),
        ("passenger_time_min", 1.0),
        ("deadhead_km", _DEADHEAD_WEIGHT),
        ("total_vmt_km", _TOTAL_VMT_WEIGHT),
        ("finish_time_sum_min", 1e-6),
    )
    for field_name, weight in weighted_fields:
        delta = float(getattr(alt_score, field_name)) - float(
            getattr(best_score, field_name)
        )
        if abs(delta) > 1e-12:
            # ``alternative`` is sorted after ``best``.  Guard against tiny
            # floating point surprises so regret never becomes negative.
            return max(1e-9, delta * float(weight))
    return max(0.0, alternative.total_cost - best.total_cost)


def _regret_cost_pair(
    vehicle_candidates: List[Solution], regret_k: int
) -> Tuple[Solution, float, float]:
    """Return best solution and a best/k-th cost pair for regret repair."""
    ordered = sorted(vehicle_candidates, key=_solution_order_key)
    best = ordered[0]
    best_cost = best.total_cost
    kth_idx = max(1, int(regret_k)) - 1
    if kth_idx >= len(ordered):
        return best, best_cost, best_cost + _unassigned_search_cost(1)
    kth = ordered[kth_idx]
    if _QUALITY_OBJECTIVE:
        return best, best_cost, best_cost + _quality_regret_gap(best, kth)
    return best, best_cost, kth.total_cost


def _legacy_objective(sol: Solution) -> float:
    return (
        sum(ev.obj_total for ev in sol.route_eval.values())
        + len(sol.unassigned) * REJECTION_PENALTY
    )


def _solution_signature(sol: Solution) -> Tuple:
    return (
        tuple(
            (vid, tuple((node.kind, node.req_id) for node in sol.routes[vid]))
            for vid in sorted(sol.routes)
        ),
        tuple(sorted(sol.unassigned)),
    )

def _infeasible_route_eval(
    *,
    finish_time_min: float,
    finish_lat: float,
    finish_lon: float,
    pickup_time: Dict[int, float],
    dropoff_time: Dict[int, float],
    onboard_dist: Dict[int, float],
    vmt_total_km: float,
    vmt_deadhead_km: float,
    vmt_service_km: float,
    time_wait_min: float,
    time_deadhead_min: float,
    time_service_min: float,
) -> RouteEval:
    return RouteEval(
        feasible=False,
        obj_total=float("inf"),
        obj_base=float("inf"),
        obj_penalty=float("inf"),
        lateness_sum=float("inf"),
        finish_time_min=finish_time_min,
        finish_lat=finish_lat,
        finish_lon=finish_lon,
        vmt_total_km=vmt_total_km,
        vmt_deadhead_km=vmt_deadhead_km,
        vmt_service_km=vmt_service_km,
        time_wait_min=time_wait_min,
        time_deadhead_min=time_deadhead_min,
        time_service_min=time_service_min,
        pickup_time=pickup_time,
        dropoff_time=dropoff_time,
        onboard_dist=onboard_dist,
        req_obj={},
        req_lateness={},
        events=[],
        search_cost=float("inf"),
        choice_miss_count=10**9,
        choice_excess_min=float("inf"),
        near_deadline_risk_min=float("inf"),
        passenger_time_min=float("inf"),
        search_deadhead_km=float("inf"),
        search_total_vmt_km=float("inf"),
    )


def simulate_route(
    veh: Veh,
    nodes: List[StopNode],
    req_map: Dict[int, Req],
    eval_only: bool = False,
    travel_cache: Optional[
        Dict[Tuple[float, float, float, float], Tuple[float, float]]
    ] = None,
) -> RouteEval:
    """Route를 시뮬레이션하여 비용/feasibility를 평가한다.

    ``eval_only=True``인 후보 탐색에서는 목적함수와 다음 삽입에 필요한
    finish/pickup/dropoff/request 비용만 유지한다. VMT, onboard distance,
    events는 최종 산출 직전의 full evaluation에서 계산한다.
    """
    lightweight = eval_only and _LIGHTWEIGHT_EVAL_ONLY
    t = veh.start_time_min
    lat, lon = veh.start_lat, veh.start_lon
    onboard: Set[int] = set()
    load = 0
    pickup_time: Dict[int, float] = {}
    dropoff_time: Dict[int, float] = {}
    onboard_dist: Dict[int, float] = (
        {} if lightweight else {rid: 0.0 for rid in req_map}
    )
    vmt_total = 0.0
    vmt_deadhead = 0.0
    vmt_service = 0.0
    # Cheap scalar VMT accumulation remains enabled for the quality objective
    # even when detailed eval-only dictionaries/events are skipped.
    search_vmt_total = 0.0
    search_vmt_deadhead = 0.0
    time_deadhead = 0.0
    time_service = 0.0
    time_wait = 0.0
    events: List[List] = []
    prev_event_lat, prev_event_lon = lat, lon

    for node in nodes:
        if travel_cache is not None:
            dist_km, tt_min = _travel_cached(
                travel_cache, lat, lon, node.lat, node.lon
            )
        else:
            dist_km, tt_min = travel(lat, lon, node.lat, node.lon)

        if _QUALITY_OBJECTIVE:
            search_vmt_total += dist_km
            if not onboard:
                search_vmt_deadhead += dist_km

        if not lightweight:
            vmt_total += dist_km
            if onboard:
                vmt_service += dist_km
                time_service += tt_min
                for rid in onboard:
                    onboard_dist[rid] += dist_km
            else:
                vmt_deadhead += dist_km
                time_deadhead += tt_min

        arrival = t + tt_min
        lat, lon = node.lat, node.lon

        if node.kind == "PU":
            req = req_map[node.req_id]
            earliest = req.call_time_min
            if arrival < earliest:
                if not eval_only:
                    move_dist, move_time = travel(
                        prev_event_lat, prev_event_lon, lat, lon
                    )
                    events.append(
                        [
                            veh.veh_id,
                            "WAIT",
                            req.req_id,
                            arrival,
                            earliest,
                            lat,
                            lon,
                            move_dist,
                            move_time,
                        ]
                    )
                if not lightweight:
                    time_wait += earliest - arrival
                t = earliest
                if not eval_only:
                    prev_event_lat, prev_event_lon = lat, lon
                    events.append(
                        [veh.veh_id, "PICKUP", req.req_id, t, t, lat, lon, 0.0, 0.0]
                    )
            else:
                t = arrival
                if not eval_only:
                    move_dist, move_time = travel(
                        prev_event_lat, prev_event_lon, lat, lon
                    )
                    events.append(
                        [
                            veh.veh_id,
                            "PICKUP",
                            req.req_id,
                            t,
                            t,
                            lat,
                            lon,
                            move_dist,
                            move_time,
                        ]
                    )
                    prev_event_lat, prev_event_lon = lat, lon

            pickup_time[req.req_id] = t
            load += 1
            if load > veh.cap:
                return _infeasible_route_eval(
                    finish_time_min=float("inf"),
                    finish_lat=lat,
                    finish_lon=lon,
                    pickup_time=pickup_time,
                    dropoff_time=dropoff_time,
                    onboard_dist=onboard_dist,
                    vmt_total_km=vmt_total,
                    vmt_deadhead_km=vmt_deadhead,
                    vmt_service_km=vmt_service,
                    time_wait_min=time_wait,
                    time_deadhead_min=time_deadhead,
                    time_service_min=time_service,
                )
            onboard.add(req.req_id)
        else:
            req = req_map[node.req_id]
            t = arrival
            if not eval_only:
                move_dist, move_time = travel(
                    prev_event_lat, prev_event_lon, lat, lon
                )
                events.append(
                    [
                        veh.veh_id,
                        "DROPOFF",
                        req.req_id,
                        t,
                        t,
                        lat,
                        lon,
                        move_dist,
                        move_time,
                    ]
                )
                prev_event_lat, prev_event_lon = lat, lon
            dropoff_time[req.req_id] = t
            onboard.discard(req.req_id)
            load -= 1

        if t > HORIZON_MIN:
            return _infeasible_route_eval(
                finish_time_min=t,
                finish_lat=lat,
                finish_lon=lon,
                pickup_time=pickup_time,
                dropoff_time=dropoff_time,
                onboard_dist=onboard_dist,
                vmt_total_km=vmt_total,
                vmt_deadhead_km=vmt_deadhead,
                vmt_service_km=vmt_service,
                time_wait_min=time_wait,
                time_deadhead_min=time_deadhead,
                time_service_min=time_service,
            )

    obj_total = 0.0
    obj_base = 0.0
    obj_penalty = 0.0
    lateness_sum = 0.0
    req_obj: Dict[int, float] = {}
    req_lateness: Dict[int, float] = {}
    choice_miss_count = 0
    choice_excess_min = 0.0
    near_deadline_risk_min = 0.0
    passenger_time_min = 0.0

    for rid, do_t in dropoff_time.items():
        req = req_map[rid]
        base = do_t - req.call_time_min
        late = max(0.0, do_t - req.latest_min)
        pen = 4.0 * late
        total = base + pen
        obj_total += total
        obj_base += base
        obj_penalty += pen
        lateness_sum += late
        req_obj[rid] = total
        req_lateness[rid] = late

        passenger_time_min += base
        deadline = _choice_deadline(req)
        choice_excess = max(0.0, do_t - deadline)
        choice_miss_count += int(choice_excess > 0.0)
        choice_excess_min += choice_excess
        if choice_excess <= 0.0:
            near_deadline_risk_min += max(
                0.0, _NEAR_DEADLINE_BUFFER_MIN - (deadline - do_t)
            )

    if not _QUALITY_OBJECTIVE:
        search_cost = obj_total
        search_deadhead = vmt_deadhead
        search_total_vmt = vmt_total
    else:
        search_deadhead = search_vmt_deadhead if lightweight else vmt_deadhead
        search_total_vmt = search_vmt_total if lightweight else vmt_total
        search_cost = (
            passenger_time_min
            + _CHOICE_MISS_PENALTY * choice_miss_count
            + _CHOICE_EXCESS_WEIGHT * choice_excess_min
            + _NEAR_DEADLINE_WEIGHT * near_deadline_risk_min
            + _DEADHEAD_WEIGHT * search_deadhead
            + _TOTAL_VMT_WEIGHT * search_total_vmt
        )

    return RouteEval(
        feasible=True,
        obj_total=obj_total,
        obj_base=obj_base,
        obj_penalty=obj_penalty,
        lateness_sum=lateness_sum,
        finish_time_min=t,
        finish_lat=lat,
        finish_lon=lon,
        vmt_total_km=vmt_total,
        vmt_deadhead_km=vmt_deadhead,
        vmt_service_km=vmt_service,
        time_wait_min=time_wait,
        time_deadhead_min=time_deadhead,
        time_service_min=time_service,
        pickup_time=pickup_time,
        dropoff_time=dropoff_time,
        onboard_dist=onboard_dist,
        req_obj=req_obj,
        req_lateness=req_lateness,
        events=events,
        search_cost=search_cost,
        choice_miss_count=choice_miss_count,
        choice_excess_min=choice_excess_min,
        near_deadline_risk_min=near_deadline_risk_min,
        passenger_time_min=passenger_time_min,
        search_deadhead_km=search_deadhead,
        search_total_vmt_km=search_total_vmt,
    )


def build_initial_solution(req_ids: List[int], vehs: List[Veh], req_map: Dict[int, Req]) -> Solution:
    routes = {v.veh_id: [] for v in vehs}
    tail_time = {v.veh_id: v.start_time_min for v in vehs}
    tail_lat = {v.veh_id: v.start_lat for v in vehs}
    tail_lon = {v.veh_id: v.start_lon for v in vehs}
    req_ids_sorted = sorted(req_ids, key=lambda rid: req_map[rid].call_time_min)

    for rid in req_ids_sorted:
        req = req_map[rid]
        best_vid = None
        best_finish = float("inf")

        for v in vehs:
            vid = v.veh_id
            _, tt_to_pu = travel(tail_lat[vid], tail_lon[vid], req.pickup_lat, req.pickup_lon)
            arr = tail_time[vid] + tt_to_pu
            pu_t = max(arr, req.call_time_min)
            _, tt_pu_do = travel(req.pickup_lat, req.pickup_lon, req.drop_lat, req.drop_lon)
            do_t = pu_t + tt_pu_do
            # horizon 초과 차량은 후보에서 제외
            if do_t > HORIZON_MIN:
                continue
            if do_t < best_finish:
                best_finish = do_t
                best_vid = vid

        if best_vid is None:
            # 모든 차량이 horizon을 초과 -> 이 요청은 미배정으로 남김
            continue

        routes[best_vid].append(StopNode("PU", rid, req.pickup_lat, req.pickup_lon))
        routes[best_vid].append(StopNode("DO", rid, req.drop_lat, req.drop_lon))
        _, tt_to_pu = travel(tail_lat[best_vid], tail_lon[best_vid], req.pickup_lat, req.pickup_lon)
        arr = tail_time[best_vid] + tt_to_pu
        pu_t = max(arr, req.call_time_min)
        _, tt_pu_do = travel(req.pickup_lat, req.pickup_lon, req.drop_lat, req.drop_lon)
        do_t = pu_t + tt_pu_do
        tail_time[best_vid] = do_t
        tail_lat[best_vid] = req.drop_lat
        tail_lon[best_vid] = req.drop_lon

    route_eval: Dict[int, RouteEval] = {}
    total_cost = 0.0
    for v in vehs:
        ev = simulate_route(v, routes[v.veh_id], req_map)
        if not ev.feasible:
            # infeasible 차량의 route 끝에서 요청을 하나씩 제거하여 feasible하게 만듦
            while routes[v.veh_id] and not ev.feasible:
                # 마지막 요청(DO, PU 쌍)을 제거
                removed_do = routes[v.veh_id].pop()
                if routes[v.veh_id]:
                    removed_pu = routes[v.veh_id].pop()
                ev = simulate_route(v, routes[v.veh_id], req_map)
        route_eval[v.veh_id] = ev
        total_cost += ev.search_cost

    assigned = set()
    for nodes in routes.values():
        for n in nodes:
            assigned.add(n.req_id)
    unassigned = set(req_map.keys()) - assigned
    total_cost += _unassigned_search_cost(len(unassigned))

    return Solution(
        routes=routes,
        unassigned=unassigned,
        route_eval=route_eval,
        total_cost=total_cost,
    )

def _travel_cached(cache: Dict[Tuple[float, float, float, float], Tuple[float, float]], lat1: float, lon1: float, lat2: float, lon2: float) -> Tuple[float, float]:
    # The bounded process-wide LRU keeps exact directed coordinates and spans
    # initializer, T0, repair, and rescue.  Do not also grow the historical
    # per-solve rounded dictionary when that cache is active.
    if process_memory_cache_enabled():
        return travel(lat1, lon1, lat2, lon2)
    key = (round(lat1, 7), round(lon1, 7), round(lat2, 7), round(lon2, 7))
    out = cache.get(key)
    if out is None:
        out = travel(lat1, lon1, lat2, lon2)
        cache[key] = out
    return out

def _empty_solution(vehs: List[Veh], req_map: Dict[int, Req]) -> Solution:
    routes = {v.veh_id: [] for v in vehs}
    route_eval: Dict[int, RouteEval] = {}
    total_cost = 0.0
    for v in vehs:
        ev = simulate_route(v, routes[v.veh_id], req_map)
        if not ev.feasible:
            raise RuntimeError("빈 초기해 평가 실패")
        route_eval[v.veh_id] = ev
        total_cost += ev.search_cost
    
    unassigned = set(req_map.keys())
    total_cost += _unassigned_search_cost(len(unassigned))
    return Solution(
        routes=routes,
        unassigned=unassigned,
        route_eval=route_eval,
        total_cost=total_cost,
    )


def _solution_from_route_plan(
    route_plan: Dict[int, List[Tuple[str, int]]],
    vehs: List[Veh],
    req_map: Dict[int, Req],
) -> Solution:
    """Validate and materialize a portfolio/global-rescue warm start."""
    veh_by_id = {v.veh_id: v for v in vehs}
    routes: Dict[int, List[StopNode]] = {v.veh_id: [] for v in vehs}
    assigned: Set[int] = set()

    for raw_vid, raw_nodes in route_plan.items():
        vid = int(raw_vid)
        if vid not in veh_by_id:
            raise ValueError(f"initial route references unknown vehicle {vid}")
        nodes: List[StopNode] = []
        for raw_kind, raw_rid in raw_nodes:
            kind = str(raw_kind)
            rid = int(raw_rid)
            if kind not in {"PU", "DO"} or rid not in req_map:
                raise ValueError(f"invalid initial route node: {(kind, rid)!r}")
            req = req_map[rid]
            lat, lon = (
                (req.pickup_lat, req.pickup_lon)
                if kind == "PU"
                else (req.drop_lat, req.drop_lon)
            )
            nodes.append(StopNode(kind, rid, lat, lon))
        if not _is_route_sequence_valid(nodes):
            raise ValueError(f"invalid pickup/drop-off sequence for vehicle {vid}")
        route_rids = {node.req_id for node in nodes}
        overlap = assigned.intersection(route_rids)
        if overlap:
            raise ValueError(f"requests assigned to multiple vehicles: {sorted(overlap)}")
        assigned.update(route_rids)
        routes[vid] = nodes

    route_eval: Dict[int, RouteEval] = {}
    for vehicle in vehs:
        ev = simulate_route(vehicle, routes[vehicle.veh_id], req_map, eval_only=True)
        if not ev.feasible:
            raise ValueError(f"infeasible initial route for vehicle {vehicle.veh_id}")
        route_eval[vehicle.veh_id] = ev
    unassigned = set(req_map).difference(assigned)
    total_cost = sum(ev.search_cost for ev in route_eval.values())
    total_cost += _unassigned_search_cost(len(unassigned))
    return Solution(routes, unassigned, route_eval, total_cost)

def _select_candidate_vehicles_for_request(sol: Solution, req: Req, vehs: List[Veh], top_k: int, travel_cache: Dict[Tuple[float, float, float, float], Tuple[float, float]]) -> List[Veh]:
    if top_k <= 0 or top_k >= len(vehs):
        return vehs
    ranked: List[Tuple[float, Veh]] = []
    for v in vehs:
        ev = sol.route_eval[v.veh_id]
        _, tt_to_pu = _travel_cached(travel_cache, ev.finish_lat, ev.finish_lon, req.pickup_lat, req.pickup_lon)
        lateness_bias = max(0.0, ev.finish_time_min - req.call_time_min)
        ranked.append((tt_to_pu + 0.1 * lateness_bias, v))
    ranked.sort(key=lambda x: x[0])
    return [v for _, v in ranked[:top_k]]


def _solution_with_inserted_route(
    sol: Solution,
    vid: int,
    rid: int,
    nodes: List[StopNode],
    route_eval: RouteEval,
    total_cost: float,
) -> Solution:
    routes = dict(sol.routes)
    route_evals = dict(sol.route_eval)
    routes[vid] = nodes
    route_evals[vid] = route_eval
    return Solution(
        routes=routes,
        unassigned=sol.unassigned - {rid},
        route_eval=route_evals,
        total_cost=total_cost,
    )

def _best_insertion_with_regret(sol: Solution, rid: int, vehs: List[Veh], req_map: Dict[int, Req], rng: random.Random, regret_k: int, position_samples: int, top_k_vehicles: int, travel_cache: Dict[Tuple[float, float, float, float], Tuple[float, float]]) -> Tuple[Solution, float, float]:
    req = req_map[rid]
    cand_vehs = _select_candidate_vehicles_for_request(sol, req, vehs, top_k_vehicles, travel_cache)
    best_cost = float("inf")
    best_vid = None
    best_nodes = None
    best_eval = None
    best_candidate: Optional[Solution] = None
    vehicle_best_candidates: List[Solution] = []

    for v in cand_vehs:
        vid = v.veh_id
        nodes = sol.routes[vid]
        old_eval = sol.route_eval[vid]
        vehicle_best_candidate: Optional[Solution] = None
        cand_positions = _candidate_positions(len(nodes), rng, max_samples=position_samples)
        seen_pos: Set[Tuple[int, int]] = set()

        for (i, j) in cand_positions:
            if (i, j) in seen_pos:
                continue
            seen_pos.add((i, j))
            new_nodes = nodes.copy()
            new_nodes.insert(i, StopNode("PU", rid, req.pickup_lat, req.pickup_lon))
            new_nodes.insert(j, StopNode("DO", rid, req.drop_lat, req.drop_lon))
            new_eval = simulate_route(v, new_nodes, req_map, eval_only=True, travel_cache=travel_cache)
            if not new_eval.feasible:
                continue
            new_total_cost = (
                sol.total_cost
                - old_eval.search_cost
                - _unassigned_search_cost(1)
                + new_eval.search_cost
            )
            candidate = _solution_with_inserted_route(
                sol, vid, rid, new_nodes, new_eval, new_total_cost
            )
            if vehicle_best_candidate is None or _is_better_solution(
                candidate, vehicle_best_candidate
            ):
                vehicle_best_candidate = candidate
            if best_candidate is None or _is_better_solution(candidate, best_candidate):
                best_candidate = candidate
                best_cost = new_total_cost
                best_vid = vid
                best_nodes = new_nodes
                best_eval = new_eval

        if vehicle_best_candidate is not None:
            vehicle_best_candidates.append(vehicle_best_candidate)

    if best_candidate is None or best_vid is None or best_nodes is None or best_eval is None:
        fallback_sol = try_insert_best(sol, rid, vehs, req_map, rng)
        return fallback_sol, fallback_sol.total_cost, fallback_sol.total_cost

    return _regret_cost_pair(vehicle_best_candidates, regret_k)


def _build_initial_solution_regret_multistart(
    req_ids: List[int],
    vehs: List[Veh],
    req_map: Dict[int, Req],
    rng: random.Random,
    starts: int,
    regret_k: int,
    position_samples: int,
    top_k_vehicles: int,
    candidate_pool_size: int,
    max_time_sec: float,
) -> Solution:
    if not req_ids:
        return _empty_solution(vehs, req_map)

    # baseline을 항상 만들어 두고 premium 생성 실패/시간초과 시 fallback한다.
    baseline_error: Optional[RuntimeError] = None
    try:
        best_overall: Optional[Solution] = build_initial_solution(req_ids, vehs, req_map)
    except RuntimeError as exc:
        best_overall = None
        baseline_error = exc

    if starts <= 0:
        if best_overall is None:
            raise RuntimeError("premium 초기화 실패: baseline 초기해 불가, multistart도 비활성화") from baseline_error
        return best_overall

    call_sorted = sorted(req_ids, key=lambda rid: req_map[rid].call_time_min)
    deadline = None if max_time_sec <= 0 else (time.time() + max_time_sec)

    for start_idx in range(starts):
        if deadline is not None and time.time() >= deadline:
            break

        srng = random.Random(rng.random() + start_idx * 10007)
        travel_cache: Dict[Tuple[float, float, float, float], Tuple[float, float]] = {}
        sol = _empty_solution(vehs, req_map)
        remaining: Set[int] = set(call_sorted)

        while remaining:
            rem_sorted = [rid for rid in call_sorted if rid in remaining]
            if len(rem_sorted) <= candidate_pool_size:
                eval_rids = rem_sorted
            else:
                n_front = max(1, candidate_pool_size // 2)
                front = rem_sorted[:n_front]
                tail = rem_sorted[n_front:]
                n_rand = candidate_pool_size - len(front)
                if n_rand > 0 and tail:
                    rand_pick = srng.sample(tail, min(n_rand, len(tail)))
                    eval_rids = front + rand_pick
                else:
                    eval_rids = front

            best_pick_rid: Optional[int] = None
            best_pick_sol: Optional[Solution] = None
            best_pick_regret = -float("inf")
            best_pick_cost = float("inf")

            for rid in eval_rids:
                cand_sol, cand_best_cost, cand_kth_cost = _best_insertion_with_regret(
                    sol=sol,
                    rid=rid,
                    vehs=vehs,
                    req_map=req_map,
                    rng=srng,
                    regret_k=regret_k,
                    position_samples=position_samples,
                    top_k_vehicles=top_k_vehicles,
                    travel_cache=travel_cache,
                )
                regret = cand_kth_cost - cand_best_cost

                if regret > best_pick_regret or (abs(regret - best_pick_regret) <= 1e-9 and cand_best_cost < best_pick_cost):
                    best_pick_regret = regret
                    best_pick_cost = cand_best_cost
                    best_pick_rid = rid
                    best_pick_sol = cand_sol

            if best_pick_rid is None or best_pick_sol is None:
                rid = rem_sorted[0]
                sol = try_insert_best(sol, rid, vehs, req_map, srng)
                remaining.remove(rid)
                continue

            sol = best_pick_sol
            remaining.remove(best_pick_rid)

        if best_overall is None or _is_better_solution(sol, best_overall):
            best_overall = sol
    if best_overall is None:
        raise RuntimeError("premium 초기화 실패: feasible 초기해를 찾지 못함") from baseline_error
    return best_overall


# -----------------------------
# Destroy & Repair
# -----------------------------

def extract_all_req_ids(sol: Solution) -> List[int]:
    seen = set()
    for nodes in sol.routes.values():
        for n in nodes:
            seen.add(n.req_id)
    return list(seen)


def remove_requests(
    sol: Solution,
    removed: Set[int],
    vehs: List[Veh],
    req_map: Dict[int, Req],
    travel_cache: Optional[
        Dict[Tuple[float, float, float, float], Tuple[float, float]]
    ] = None,
) -> Solution:
    """Remove requests and reevaluate only routes that actually contained them."""
    if not removed:
        return sol

    if _SELECTIVE_ROUTE_REEVALUATION:
        affected_vids = {
            vid
            for vid, nodes in sol.routes.items()
            if any(node.req_id in removed for node in nodes)
        }

        routes = dict(sol.routes)
        route_eval = dict(sol.route_eval)
        veh_by_id = {v.veh_id: v for v in vehs}
        for vid in affected_vids:
            routes[vid] = [
                node for node in sol.routes[vid] if node.req_id not in removed
            ]
            vehicle = veh_by_id[vid]
            ev = simulate_route(
                vehicle,
                routes[vid],
                req_map,
                eval_only=True,
                travel_cache=travel_cache,
            )
            if not ev.feasible:
                # Removing stops cannot normally break feasibility; keep a hard guard.
                ev = _infeasible_route_eval(
                    finish_time_min=float("inf"),
                    finish_lat=vehicle.start_lat,
                    finish_lon=vehicle.start_lon,
                    pickup_time={},
                    dropoff_time={},
                    onboard_dist={},
                    vmt_total_km=0.0,
                    vmt_deadhead_km=0.0,
                    vmt_service_km=0.0,
                    time_wait_min=0.0,
                    time_deadhead_min=0.0,
                    time_service_min=0.0,
                )
            route_eval[vid] = ev
    else:
        # Reproducible A/B fallback: the pre-optimization all-route path.
        routes = {
            vid: [node for node in nodes if node.req_id not in removed]
            for vid, nodes in sol.routes.items()
        }
        route_eval = {}
        for vehicle in vehs:
            route_eval[vehicle.veh_id] = simulate_route(
                vehicle,
                routes[vehicle.veh_id],
                req_map,
                eval_only=True,
                travel_cache=travel_cache,
            )

    # Keep the historical vehicle-order summation to avoid floating-point order drift.
    unassigned = sol.unassigned.union(removed)
    total_cost = sum(route_eval[v.veh_id].search_cost for v in vehs)
    total_cost += _unassigned_search_cost(len(unassigned))
    return Solution(
        routes=routes,
        unassigned=unassigned,
        route_eval=route_eval,
        total_cost=total_cost,
    )


def _candidate_positions(route_len: int, rng: random.Random, max_samples: int = 40) -> List[Tuple[int, int]]:
    # route_len: current number of nodes
    # positions i in [0..route_len] for PU, j in [i+1..route_len+1] for DO
    if route_len <= 24:
        return [(i, j) for i in range(route_len + 1) for j in range(i + 1, route_len + 2)]

    positions: List[Tuple[int, int]] = []
    for _ in range(max_samples):
        i = rng.randint(0, route_len)
        j = rng.randint(i + 1, route_len + 1)
        positions.append((i, j))
    return positions


def _all_candidate_positions(route_len: int) -> List[Tuple[int, int]]:
    return [(i, j) for i in range(route_len + 1) for j in range(i + 1, route_len + 2)]


def try_insert_best_and_second(
    sol: Solution,
    rid: int,
    vehs: List[Veh],
    req_map: Dict[int, Req],
    rng: random.Random,
    position_samples: int = 40,
    top_k_vehicles: int = 0,
    exact_top_vehicle_count: int = 0,
    exact_route_len_limit: int = 28,
    regret_k: int = 2,
    travel_cache: Optional[Dict[Tuple[float, float, float, float], Tuple[float, float]]] = None,
) -> Tuple[Solution, float, float]:
    """Return the selected solution and best/k-th vehicle insertion costs.

    Every vehicle contributes only its cheapest position pair.  Therefore a
    high regret represents genuine vehicle scarcity instead of two nearly
    identical positions on the same vehicle.
    """
    req = req_map[rid]
    tc = travel_cache if travel_cache is not None else {}
    if top_k_vehicles > 0:
        cand_vehs = _select_candidate_vehicles_for_request(sol, req, vehs, top_k_vehicles, tc)
    else:
        cand_vehs = vehs

    best_cost = float("inf")
    vehicle_best_candidates: List[Solution] = []
    best_vid: Optional[int] = None
    best_nodes: Optional[List[StopNode]] = None
    best_eval: Optional[RouteEval] = None
    best_candidate: Optional[Solution] = None

    for veh_idx, v in enumerate(cand_vehs):
        vid = v.veh_id
        nodes = sol.routes[vid]
        old_eval = sol.route_eval[vid]
        vehicle_best_candidate: Optional[Solution] = None

        if veh_idx < exact_top_vehicle_count and len(nodes) <= exact_route_len_limit:
            cand_positions = _all_candidate_positions(len(nodes))
        else:
            samples = max(8, int(position_samples))
            if veh_idx < exact_top_vehicle_count:
                samples = max(samples, int(samples * 1.8))
            cand_positions = _candidate_positions(len(nodes), rng, max_samples=samples)
        seen_pos: Set[Tuple[int, int]] = set()
        for (i, j) in cand_positions:
            if (i, j) in seen_pos:
                continue
            seen_pos.add((i, j))
            new_nodes = nodes.copy()
            new_nodes.insert(i, StopNode("PU", rid, req.pickup_lat, req.pickup_lon))
            new_nodes.insert(j, StopNode("DO", rid, req.drop_lat, req.drop_lon))

            new_eval = simulate_route(v, new_nodes, req_map, eval_only=True, travel_cache=tc)
            if not new_eval.feasible:
                continue

            new_total_cost = (
                sol.total_cost
                - old_eval.search_cost
                - _unassigned_search_cost(1)
                + new_eval.search_cost
            )
            candidate = _solution_with_inserted_route(
                sol, vid, rid, new_nodes, new_eval, new_total_cost
            )
            if vehicle_best_candidate is None or _is_better_solution(
                candidate, vehicle_best_candidate
            ):
                vehicle_best_candidate = candidate
            if best_candidate is None or _is_better_solution(candidate, best_candidate):
                best_candidate = candidate
                best_cost = new_total_cost
                best_vid = vid
                best_nodes = new_nodes
                best_eval = new_eval

        if vehicle_best_candidate is not None:
            vehicle_best_candidates.append(vehicle_best_candidate)

    if best_candidate is None or best_vid is None or best_nodes is None or best_eval is None:
        return sol, sol.total_cost, sol.total_cost

    insertion_sol, best_cost, kth_cost = _regret_cost_pair(
        vehicle_best_candidates, regret_k
    )
    selected_sol = insertion_sol if _is_better_solution(insertion_sol, sol) else sol
    return selected_sol, best_cost, kth_cost


def try_insert_best(
    sol: Solution,
    rid: int,
    vehs: List[Veh],
    req_map: Dict[int, Req],
    rng: random.Random,
    position_samples: int = 40,
    top_k_vehicles: int = 0,
    exact_top_vehicle_count: int = 0,
    exact_route_len_limit: int = 28,
    travel_cache: Optional[
        Dict[Tuple[float, float, float, float], Tuple[float, float]]
    ] = None,
) -> Solution:
    best_sol, _, _ = try_insert_best_and_second(
        sol,
        rid,
        vehs,
        req_map,
        rng,
        position_samples=position_samples,
        top_k_vehicles=top_k_vehicles,
        exact_top_vehicle_count=exact_top_vehicle_count,
        exact_route_len_limit=exact_route_len_limit,
        regret_k=2,
        travel_cache=travel_cache,
    )
    return best_sol


def repair_greedy(sol: Solution, removed: Set[int], vehs: List[Veh], req_map: Dict[int, Req], rng: random.Random) -> Solution:
    for rid in sorted(removed, key=lambda r: (_choice_deadline(req_map[r]), r)):
        sol = try_insert_best(sol, rid, vehs, req_map, rng)
    return sol


def repair_greedy_precise(
    sol: Solution,
    removed: Set[int],
    vehs: List[Veh],
    req_map: Dict[int, Req],
    rng: random.Random,
    position_samples: int,
    top_k_vehicles: int,
    exact_top_vehicle_count: int,
    exact_route_len_limit: int,
    travel_cache: Dict[Tuple[float, float, float, float], Tuple[float, float]],
) -> Solution:
    for rid in sorted(removed, key=lambda r: (_choice_deadline(req_map[r]), r)):
        sol = try_insert_best(
            sol,
            rid,
            vehs,
            req_map,
            rng,
            position_samples=position_samples,
            top_k_vehicles=top_k_vehicles,
            exact_top_vehicle_count=exact_top_vehicle_count,
            exact_route_len_limit=exact_route_len_limit,
            travel_cache=travel_cache,
        )
    return sol


def repair_regret2(sol: Solution, removed: Set[int], vehs: List[Veh], req_map: Dict[int, Req], rng: random.Random) -> Solution:
    remaining = set(removed)

    while remaining:
        best_rid = None
        best_regret = -float("inf")
        best_sol_for_rid: Optional[Solution] = None

        for rid in sorted(remaining):
            if _TRUE_REGRET2:
                sol1, cost1, cost2 = try_insert_best_and_second(
                    sol, rid, vehs, req_map, rng, regret_k=2
                )
            else:
                # Reproducible A/B fallback for the former random approximation.
                sol1 = try_insert_best(sol, rid, vehs, req_map, rng)
                cost1 = sol1.total_cost
                rng2 = random.Random(rng.random())
                sol2 = try_insert_best(sol, rid, vehs, req_map, rng2)
                cost2 = sol2.total_cost

            # regret2 = second-best - best (클수록 먼저 넣어야)
            regret = cost2 - cost1
            if regret > best_regret:
                best_regret = regret
                best_rid = rid
                best_sol_for_rid = sol1

        if best_rid is None or best_sol_for_rid is None:
            raise RuntimeError("Regret2 삽입 실패")

        sol = best_sol_for_rid
        remaining.remove(best_rid)

    return sol


def repair_regret2_precise(
    sol: Solution,
    removed: Set[int],
    vehs: List[Veh],
    req_map: Dict[int, Req],
    rng: random.Random,
    position_samples: int,
    top_k_vehicles: int,
    exact_top_vehicle_count: int,
    exact_route_len_limit: int,
    travel_cache: Dict[Tuple[float, float, float, float], Tuple[float, float]],
    regret_k: int = 2,
) -> Solution:
    remaining = set(removed)

    while remaining:
        best_rid = None
        best_regret = -float("inf")
        best_sol_for_rid: Optional[Solution] = None

        for rid in sorted(remaining):
            if _TRUE_REGRET2:
                sol1, cost1, cost2 = try_insert_best_and_second(
                    sol,
                    rid,
                    vehs,
                    req_map,
                    rng,
                    position_samples=position_samples,
                    top_k_vehicles=top_k_vehicles,
                    exact_top_vehicle_count=exact_top_vehicle_count,
                    exact_route_len_limit=exact_route_len_limit,
                    regret_k=regret_k,
                    travel_cache=travel_cache,
                )
            else:
                sol1 = try_insert_best(
                    sol,
                    rid,
                    vehs,
                    req_map,
                    rng,
                    position_samples=position_samples,
                    top_k_vehicles=top_k_vehicles,
                    exact_top_vehicle_count=exact_top_vehicle_count,
                    exact_route_len_limit=exact_route_len_limit,
                    travel_cache=travel_cache,
                )
                cost1 = sol1.total_cost
                rng2 = random.Random(rng.random())
                sol2 = try_insert_best(
                    sol,
                    rid,
                    vehs,
                    req_map,
                    rng2,
                    position_samples=position_samples,
                    top_k_vehicles=top_k_vehicles,
                    exact_top_vehicle_count=exact_top_vehicle_count,
                    exact_route_len_limit=exact_route_len_limit,
                    travel_cache=travel_cache,
                )
                cost2 = sol2.total_cost

            regret = cost2 - cost1
            if regret > best_regret:
                best_regret = regret
                best_rid = rid
                best_sol_for_rid = sol1

        if best_rid is None or best_sol_for_rid is None:
            raise RuntimeError("Precise Regret2 삽입 실패")

        sol = best_sol_for_rid
        remaining.remove(best_rid)

    return sol


# -----------------------------
# Destroy operators
# -----------------------------

def destroy_random(sol: Solution, q: int, rng: random.Random) -> Set[int]:
    all_ids = extract_all_req_ids(sol)
    if q >= len(all_ids):
        return set(all_ids)
    return set(rng.sample(all_ids, q))


def destroy_worst(sol: Solution, q: int) -> Set[int]:
    # 요청별 obj_total이 큰 순으로 제거
    # route_eval에 req_obj가 들어있음
    scores: Dict[int, float] = {}
    for ev in sol.route_eval.values():
        for rid, v in ev.req_obj.items():
            scores[rid] = v
    if not scores:
        return set()
    sorted_ids = sorted(scores.keys(), key=lambda rid: scores[rid], reverse=True)
    return set(sorted_ids[: min(q, len(sorted_ids))])


def destroy_shaw(sol: Solution, q: int, rng: random.Random, req_map: Dict[int, Req]) -> Set[int]:
    all_ids = extract_all_req_ids(sol)
    if not all_ids:
        return set()
    seed_rid = rng.choice(all_ids)
    removed = {seed_rid}

    def relatedness(a: int, b: int) -> float:
        ra, rb = req_map[a], req_map[b]
        pu_km, _ = travel(ra.pickup_lat, ra.pickup_lon, rb.pickup_lat, rb.pickup_lon)
        do_km, _ = travel(ra.drop_lat, ra.drop_lon, rb.drop_lat, rb.drop_lon)
        dt = abs(ra.call_time_min - rb.call_time_min)
        ddl = abs(_choice_deadline(ra) - _choice_deadline(rb))
        # Pickup-only Shaw removal grouped opposite-direction trips together.
        # Include both endpoints and deadline proximity to form route-compatible
        # request strings.
        return pu_km + do_km + 0.08 * dt + 0.04 * ddl

    while len(removed) < min(q, len(all_ids)):
        # removed 집합과 가장 '관련된' 후보를 추가로 제거
        best = None
        best_val = float("inf")
        for cand in all_ids:
            if cand in removed:
                continue
            # seed와의 relatedness만 사용(간단)
            val = relatedness(seed_rid, cand)
            if val < best_val:
                best_val = val
                best = cand
        if best is None:
            break
        removed.add(best)

    return removed


def destroy_choice_critical(
    sol: Solution,
    q: int,
    req_map: Dict[int, Req],
    rng: random.Random,
) -> Set[int]:
    """Remove requests closest to the exact Stage2 choice boundary first."""
    scored: List[Tuple[int, float, float, int]] = []
    for ev in sol.route_eval.values():
        for rid, do_t in ev.dropoff_time.items():
            slack = _choice_deadline(req_map[rid]) - do_t
            # Slight misses are often rescuable; then protect small positive
            # slack.  Random jitter diversifies otherwise identical portfolios.
            miss_rank = 0 if slack < 0.0 else 1
            scored.append((miss_rank, abs(slack), rng.random(), rid))
    scored.sort()
    return {rid for _, _, _, rid in scored[: min(max(1, q), len(scored))]}


def _select_unassigned_for_repair(
    sol: Solution,
    req_map: Dict[int, Req],
    limit: int,
    rng: random.Random,
    exclude: Optional[Set[int]] = None,
) -> Set[int]:
    if limit <= 0 or not sol.unassigned:
        return set()
    scored: List[Tuple[int, float, float, int]] = []
    excluded = exclude or set()
    for rid in sol.unassigned:
        if rid in excluded:
            continue
        req = req_map[rid]
        _, direct_min = travel(req.pickup_lat, req.pickup_lon, req.drop_lat, req.drop_lon)
        direct_slack = _choice_deadline(req) - (req.call_time_min + direct_min)
        # Scarce/tight feasible requests first; retain a deterministic random
        # tie-break to diversify portfolio replicas.
        scored.append((0 if direct_slack >= 0.0 else 1, abs(direct_slack), rng.random(), rid))
    scored.sort()
    return {rid for _, _, _, rid in scored[: min(limit, len(scored))]}


def _route_request_order(nodes: List[StopNode]) -> List[int]:
    seen: Set[int] = set()
    order: List[int] = []
    for n in nodes:
        if n.req_id not in seen:
            seen.add(n.req_id)
            order.append(n.req_id)
    return order


def destroy_route_segment(sol: Solution, q: int, rng: random.Random) -> Set[int]:
    veh_ids = [vid for vid, nodes in sol.routes.items() if nodes]
    if not veh_ids:
        return set()
    vid = rng.choice(veh_ids)
    order = _route_request_order(sol.routes[vid])
    if not order:
        return set()
    k = min(max(1, q), len(order))
    if k >= len(order):
        return set(order)
    start = rng.randint(0, len(order) - k)
    return set(order[start : start + k])


def destroy_route_tail(sol: Solution, q: int) -> Set[int]:
    candidates = []
    for vid, nodes in sol.routes.items():
        order = _route_request_order(nodes)
        if order:
            candidates.append((vid, order))
    if not candidates:
        return set()
    # 종료시간이 늦은 route tail을 우선적으로 제거한다.
    vid = max(candidates, key=lambda x: sol.route_eval[x[0]].finish_time_min)[0]
    order = next(order for v, order in candidates if v == vid)
    k = min(max(1, q), len(order))
    return set(order[-k:])


# -----------------------------
# Route-level neighborhood moves
# -----------------------------

def _is_route_sequence_valid(nodes: List[StopNode]) -> bool:
    """Validate pickup/dropoff pairing for one route sequence."""
    pu_seen: Set[int] = set()
    do_seen: Set[int] = set()
    for n in nodes:
        rid = n.req_id
        if n.kind == "PU":
            if rid in pu_seen or rid in do_seen:
                return False
            pu_seen.add(rid)
        elif n.kind == "DO":
            if rid in do_seen or rid not in pu_seen:
                return False
            do_seen.add(rid)
        else:
            return False
    return pu_seen == do_seen


def _zero_load_cut_positions(nodes: List[StopNode]) -> List[int]:
    """Cuts that never split a pickup/drop-off pair across route tails."""
    cuts = [0]
    load = 0
    for idx, node in enumerate(nodes):
        load += 1 if node.kind == "PU" else -1
        if load == 0:
            cuts.append(idx + 1)
    return cuts


def _build_two_route_candidate(
    sol: Solution,
    vid_a: int,
    new_nodes_a: List[StopNode],
    vid_b: int,
    new_nodes_b: List[StopNode],
    veh_by_id: Dict[int, Veh],
    req_map: Dict[int, Req],
) -> Optional[Solution]:
    if vid_a == vid_b:
        return None
    if not _is_route_sequence_valid(new_nodes_a) or not _is_route_sequence_valid(new_nodes_b):
        return None

    eva = simulate_route(veh_by_id[vid_a], new_nodes_a, req_map, eval_only=True)
    if not eva.feasible:
        return None
    evb = simulate_route(veh_by_id[vid_b], new_nodes_b, req_map, eval_only=True)
    if not evb.feasible:
        return None

    routes2 = dict(sol.routes)
    route_eval2 = dict(sol.route_eval)
    routes2[vid_a] = new_nodes_a
    routes2[vid_b] = new_nodes_b
    route_eval2[vid_a] = eva
    route_eval2[vid_b] = evb

    new_total = (
        sol.total_cost
        - sol.route_eval[vid_a].search_cost
        - sol.route_eval[vid_b].search_cost
        + eva.search_cost
        + evb.search_cost
    )
    return Solution(routes=routes2, unassigned=sol.unassigned, route_eval=route_eval2, total_cost=new_total)


def local_two_opt_star(
    sol: Solution,
    vehs: List[Veh],
    req_map: Dict[int, Req],
    rng: random.Random,
    pair_trials: int = 18,
    cuts_per_pair: int = 8,
) -> Solution:
    """2-opt* between two routes: exchange route tails at sampled cut points."""
    veh_ids = [vid for vid, nodes in sol.routes.items() if len(nodes) >= 2]
    if len(veh_ids) < 2:
        return sol

    veh_by_id = {v.veh_id: v for v in vehs}
    trials = max(6, min(pair_trials, len(veh_ids) * 4))
    best: Optional[Solution] = None
    best_cost = float("inf")

    for _ in range(trials):
        vid_a, vid_b = rng.sample(veh_ids, 2)
        nodes_a = sol.routes[vid_a]
        nodes_b = sol.routes[vid_b]
        for _ in range(max(2, cuts_per_pair)):
            cut_a = rng.choice(_zero_load_cut_positions(nodes_a))
            cut_b = rng.choice(_zero_load_cut_positions(nodes_b))
            if cut_a == 0 and cut_b == 0:
                continue
            if cut_a == len(nodes_a) and cut_b == len(nodes_b):
                continue

            cand = _build_two_route_candidate(
                sol=sol,
                vid_a=vid_a,
                new_nodes_a=nodes_a[:cut_a] + nodes_b[cut_b:],
                vid_b=vid_b,
                new_nodes_b=nodes_b[:cut_b] + nodes_a[cut_a:],
                veh_by_id=veh_by_id,
                req_map=req_map,
            )
            if cand is not None and (best is None or _is_better_solution(cand, best)):
                best = cand
                best_cost = cand.total_cost

    return best if best is not None else sol


def _try_insert_on_fixed_vehicle(
    sol: Solution,
    rid: int,
    veh: Veh,
    req_map: Dict[int, Req],
    rng: random.Random,
    position_samples: int = 80,
) -> Optional[Solution]:
    req = req_map[rid]
    nodes = sol.routes[veh.veh_id]
    old_eval = sol.route_eval[veh.veh_id]
    if len(nodes) <= 28:
        cand_positions = _all_candidate_positions(len(nodes))
    else:
        cand_positions = _candidate_positions(len(nodes), rng, max_samples=max(20, position_samples))

    best_sol: Optional[Solution] = None
    best_cost = float("inf")
    seen_pos: Set[Tuple[int, int]] = set()
    for (i, j) in cand_positions:
        if (i, j) in seen_pos:
            continue
        seen_pos.add((i, j))
        new_nodes = nodes.copy()
        new_nodes.insert(i, StopNode("PU", rid, req.pickup_lat, req.pickup_lon))
        new_nodes.insert(j, StopNode("DO", rid, req.drop_lat, req.drop_lon))
        new_eval = simulate_route(veh, new_nodes, req_map, eval_only=True)
        if not new_eval.feasible:
            continue
        new_total = (
            sol.total_cost
            - old_eval.search_cost
            - _unassigned_search_cost(1)
            + new_eval.search_cost
        )
        if best_sol is None or _is_better_solution(
            Solution(
                routes={**sol.routes, veh.veh_id: new_nodes},
                unassigned=(sol.unassigned - {rid}),
                route_eval={**sol.route_eval, veh.veh_id: new_eval},
                total_cost=new_total,
            ),
            best_sol,
        ):
            routes2 = dict(sol.routes)
            route_eval2 = dict(sol.route_eval)
            routes2[veh.veh_id] = new_nodes
            route_eval2[veh.veh_id] = new_eval
            best_sol = Solution(routes=routes2, unassigned=(sol.unassigned - {rid}), route_eval=route_eval2, total_cost=new_total)
            best_cost = new_total

    return best_sol


def local_cross_exchange(
    sol: Solution,
    vehs: List[Veh],
    req_map: Dict[int, Req],
    rng: random.Random,
    pair_trials: int = 20,
) -> Solution:
    """Cross-exchange between two routes via request-pair swap and constrained reinsertion."""
    veh_ids = [vid for vid, nodes in sol.routes.items() if nodes]
    if len(veh_ids) < 2:
        return sol
    veh_by_id = {v.veh_id: v for v in vehs}

    trials = max(8, min(pair_trials, len(veh_ids) * 4))
    best: Optional[Solution] = None
    best_cost = float("inf")

    for _ in range(trials):
        vid_a, vid_b = rng.sample(veh_ids, 2)
        reqs_a = _route_request_order(sol.routes[vid_a])
        reqs_b = _route_request_order(sol.routes[vid_b])
        if not reqs_a or not reqs_b:
            continue
        rid_a = rng.choice(reqs_a)
        rid_b = rng.choice(reqs_b)
        if rid_a == rid_b:
            continue

        sol_removed = remove_requests(sol, {rid_a, rid_b}, vehs, req_map)
        after_a = _try_insert_on_fixed_vehicle(
            sol_removed, rid_a, veh_by_id[vid_b], req_map, rng, position_samples=90
        )
        if after_a is None:
            continue
        cand = _try_insert_on_fixed_vehicle(
            after_a, rid_b, veh_by_id[vid_a], req_map, rng, position_samples=90
        )
        if cand is not None and (best is None or _is_better_solution(cand, best)):
            best = cand
            best_cost = cand.total_cost

    return best if best is not None else sol


# -----------------------------
# SA helpers
# -----------------------------


def _weighted_pick(rng: random.Random, ops: List[str], weights: Dict[str, float]) -> str:
    total = 0.0
    for op in ops:
        total += max(1e-6, float(weights.get(op, 1.0)))
    if total <= 0:
        return rng.choice(ops)
    r = rng.random() * total
    acc = 0.0
    for op in ops:
        acc += max(1e-6, float(weights.get(op, 1.0)))
        if r <= acc:
            return op
    return ops[-1]


def _update_weight(cur: float, reward: float, lr: float = 0.08) -> float:
    nxt = (1.0 - lr) * cur + lr * reward
    return max(0.05, min(20.0, nxt))


def _new_operator_stats(ops: List[str]) -> Dict[str, Dict[str, float | int]]:
    """Create deterministic per-operator counters for one solver call."""
    return {
        op: {
            "calls": 0,
            "no_ops": 0,
            "accepted": 0,
            "improved_current": 0,
            "best_improved": 0,
            "runtime_sec": 0.0,
            "final_weight": 1.0,
        }
        for op in ops
    }


def _record_operator_outcome(
    stats: Dict[str, Dict[str, float | int]],
    op: str,
    *,
    is_noop: bool,
    accepted: bool,
    improved_current: bool,
    best_improved: bool,
) -> None:
    """Attribute one candidate outcome to its selected operator."""
    row = stats[op]
    row["no_ops"] = int(row["no_ops"]) + int(is_noop)
    row["accepted"] = int(row["accepted"]) + int(accepted)
    row["improved_current"] = int(row["improved_current"]) + int(
        improved_current
    )
    row["best_improved"] = int(row["best_improved"]) + int(best_improved)


def _operator_stats_payload(
    destroy_stats: Dict[str, Dict[str, float | int]],
    repair_stats: Dict[str, Dict[str, float | int]],
    destroy_weights: Dict[str, float],
    repair_weights: Dict[str, float],
) -> Dict[str, Dict[str, Dict[str, float | int]]]:
    """Attach final adaptive weights without mutating live counters."""
    payload: Dict[str, Dict[str, Dict[str, float | int]]] = {
        "destroy": {},
        "repair": {},
    }
    for kind, stats, weights in (
        ("destroy", destroy_stats, destroy_weights),
        ("repair", repair_stats, repair_weights),
    ):
        for op, row in stats.items():
            out = dict(row)
            out["final_weight"] = float(weights[op])
            payload[kind][op] = out
    return payload


def _adaptive_destroy_q(n: int, stagnation_iters: int, rng: random.Random) -> int:
    if n <= 1:
        return 1
    min_frac = 0.08
    max_frac = 0.35
    frac = min_frac + min(max_frac - min_frac, stagnation_iters / 2500.0)
    frac += (rng.random() - 0.5) * 0.04
    frac = max(min_frac, min(max_frac, frac))
    q = max(3, int(round(frac * n)))
    q = min(q, max(1, n // 2))
    return q


def _operator_reward(
    improved_best: bool,
    accepted: bool,
    delta: float,
    ref_cost: float,
) -> float:
    """Improvement-centric reward: accepted-but-not-improving gets low credit."""
    scale = max(1.0, abs(ref_cost))
    improv_ratio = max(0.0, -delta) / scale
    if improved_best:
        return min(20.0, 6.0 + 400.0 * improv_ratio)
    if delta < -1e-9:
        return min(10.0, 3.0 + 220.0 * improv_ratio)
    if accepted:
        return 0.35
    return 0.10


def _destroy_weight_multiplier(
    op: str,
    n: int,
    stagnation_iters: int,
    iter_idx: int,
    route_destroy_used: int,
) -> float:
    """Restrict ROUTE_* usage and gate heavy route-level moves."""
    route_ratio = route_destroy_used / max(1, iter_idx)

    if op == "ROUTE_SEGMENT":
        if n < 12 or stagnation_iters < 80:
            return 0.04
        if route_ratio > 0.22:
            return 0.20
        return 0.70

    if op == "ROUTE_TAIL":
        if n < 16 or stagnation_iters < 140:
            return 0.03
        if route_ratio > 0.18:
            return 0.15
        return 0.45

    if op == "TWO_OPT_STAR":
        if n < 10:
            return 0.03
        if stagnation_iters < 35:
            return 0.12
        return 0.85

    if op == "CROSS_EXCHANGE":
        if n < 10:
            return 0.03
        if stagnation_iters < 55:
            return 0.10
        return 0.75

    return 1.0


def _effective_destroy_weights(
    base_weights: Dict[str, float],
    n: int,
    stagnation_iters: int,
    iter_idx: int,
    route_destroy_used: int,
) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for op, w in base_weights.items():
        mult = _destroy_weight_multiplier(op, n, stagnation_iters, iter_idx, route_destroy_used)
        out[op] = max(1e-6, float(w) * mult)
    return out


def _effective_stagnation_patience(
    configured_patience: int,
    iter_cap: int,
    best_sol: Solution,
) -> int:
    """Return conservative quality-mode patience for unresolved hard cases.

    A positive configured patience remains useful once the elite solution is
    fully choice-eligible and clear of the near-deadline buffer.  While any
    primary/near-deadline risk remains, early termination would trade away the
    remainder of the caller's fixed iteration budget, so extend patience past
    that cap.  A configured value of zero continues to disable early stopping.
    """
    patience = max(0, int(configured_patience))
    if patience == 0 or not _QUALITY_OBJECTIVE:
        return patience
    score = solution_score(best_sol)
    unresolved = (
        score.not_choice_eligible > 0
        or score.unassigned > 0
        or score.choice_excess_min > 1e-12
        or score.near_deadline_risk_min > 1e-12
    )
    if not unresolved:
        return patience
    return max(patience * 2, int(iter_cap) + 1)


def estimate_T0(sol: Solution, vehs: List[Veh], req_map: Dict[int, Req], rng: random.Random, samples: int = 8) -> float:
    base = sol.total_cost
    if base <= 0:
        return 1.0

    all_ids = extract_all_req_ids(sol)
    if not all_ids:
        return 1.0

    deltas = []
    for _ in range(samples):
        q = min(5, max(1, len(all_ids) // 10))
        rem = destroy_random(sol, q, rng)
        sol_r = remove_requests(sol, rem, vehs, req_map)
        sol_c = repair_greedy(sol_r, rem, vehs, req_map, rng)
        deltas.append(abs(sol_c.total_cost - base))

    avg = sum(deltas) / max(1, len(deltas))
    return max(1e-6, avg)


def _materialize_full_solution(
    sol: Solution, vehs: List[Veh], req_map: Dict[int, Req]
) -> Solution:
    """Build full route details for final KPI/event output on every exit path."""
    route_eval: Dict[int, RouteEval] = {}
    for vehicle in vehs:
        ev = simulate_route(
            vehicle, sol.routes[vehicle.veh_id], req_map, eval_only=False
        )
        if not ev.feasible:
            raise RuntimeError(
                f"final route evaluation failed for vehicle {vehicle.veh_id}"
            )
        route_eval[vehicle.veh_id] = ev
    return Solution(
        routes=sol.routes,
        unassigned=sol.unassigned,
        route_eval=route_eval,
        total_cost=sol.total_cost,
    )


def _route_plan_payload(sol: Solution) -> Dict[int, List[Tuple[str, int]]]:
    return {
        int(vid): [(node.kind, int(node.req_id)) for node in nodes]
        for vid, nodes in sol.routes.items()
    }


def _score_payload(sol: Solution) -> Dict[str, float | int]:
    score = solution_score(sol)
    return {
        "not_choice_eligible": score.not_choice_eligible,
        "choice_eligible": max(0, len(extract_all_req_ids(sol)) - sum(
            ev.choice_miss_count for ev in sol.route_eval.values()
        )),
        "unassigned": score.unassigned,
        "choice_excess_min": score.choice_excess_min,
        "near_deadline_risk_min": score.near_deadline_risk_min,
        "passenger_time_min": score.passenger_time_min,
        "deadhead_km": score.deadhead_km,
        "total_vmt_km": score.total_vmt_km,
        "finish_time_sum_min": score.finish_time_sum_min,
    }


def _build_problem_objects(
    requests_od: List[Dict], vehicles_od: List[Dict]
) -> Tuple[Dict[int, Req], List[Veh]]:
    """Build the immutable request/vehicle objects shared by search and output.

    Keeping this conversion in one place is important for winner-only
    materialization: a selected route plan must be evaluated against exactly
    the same request deadlines and vehicle start state as the portfolio worker.
    """
    req_map: Dict[int, Req] = {}
    for raw in requests_od:
        rid = int(raw["req_id"])
        req_map[rid] = Req(
            req_id=rid,
            call_time_min=float(raw["call_time_min"]),
            pickup_lat=float(raw["pickup_lat"]),
            pickup_lon=float(raw["pickup_lon"]),
            drop_lat=float(raw["drop_lat"]),
            drop_lon=float(raw["drop_lon"]),
            latest_min=float(raw["latest_min"]),
            choice_deadline_min=(
                float(raw["choice_deadline_min"])
                if raw.get("choice_deadline_min") not in (None, "")
                else float(raw["latest_min"])
            ),
        )

    vehs = [
        Veh(
            veh_id=int(raw["veh_id"]),
            cap=int(raw["cap"]),
            start_time_min=float(raw["start_time_min"]),
            start_lat=float(raw["start_lat"]),
            start_lon=float(raw["start_lon"]),
        )
        for raw in vehicles_od
    ]
    return req_map, vehs


def materialize_route_plan(
    route_plan: Dict[int, List[Tuple[str, int]]],
    requests_od: List[Dict],
    vehicles_od: List[Dict],
    *,
    events_path: Optional[str | Path] = None,
    speed_kmph: float = 45.0,
    rejection_penalty: float = 300.0,
    alns_quality_profile: bool = False,
    quality_choice_miss_penalty: float = 10000.0,
    quality_choice_excess_weight: float = 25.0,
    quality_near_deadline_buffer_min: float = 1.0,
) -> Dict:
    """Fully evaluate a selected route plan without running ALNS/SA.

    Portfolio workers may return only a lightweight route plan.  This helper
    validates that plan, rebuilds full request/vehicle/event details, and uses
    the same objective configuration as :func:`solve_od_alns_sa`.  It is safe
    to call only for the selected local/global result.
    """
    if float(quality_choice_miss_penalty) <= 0.0:
        raise ValueError("quality_choice_miss_penalty must be > 0")

    set_speed_kmph(speed_kmph)
    global REJECTION_PENALTY
    global _QUALITY_OBJECTIVE
    global _CHOICE_MISS_PENALTY
    global _CHOICE_EXCESS_WEIGHT
    global _NEAR_DEADLINE_BUFFER_MIN
    REJECTION_PENALTY = float(rejection_penalty)
    _QUALITY_OBJECTIVE = bool(alns_quality_profile)
    _CHOICE_MISS_PENALTY = float(quality_choice_miss_penalty)
    _CHOICE_EXCESS_WEIGHT = float(quality_choice_excess_weight)
    _NEAR_DEADLINE_BUFFER_MIN = max(
        0.0, float(quality_near_deadline_buffer_min)
    )

    req_map, vehs = _build_problem_objects(requests_od, vehicles_od)
    search_sol = _solution_from_route_plan(route_plan, vehs, req_map)
    search_cost = search_sol.total_cost
    full_sol = _materialize_full_solution(search_sol, vehs, req_map)
    req_updates, veh_updates = _finalize_outputs(full_sol, vehs, req_map)

    output_events_path = ""
    if events_path is not None:
        event_file = Path(events_path)
        event_file.parent.mkdir(parents=True, exist_ok=True)
        with _open_with_retry(
            event_file, "w", newline="", encoding="utf-8"
        ) as handle:
            csv.writer(handle).writerow(VEHICLE_EVENTS_HEADER)
        _write_events_from_solution(event_file, full_sol, vehs)
        output_events_path = str(event_file)

    return {
        "summary": {
            "candidates": len(req_map),
            "served": len(req_map) - len(full_sol.unassigned),
            "feasible": all(ev.feasible for ev in full_sol.route_eval.values()),
            "best_cost": _legacy_objective(full_sol),
            "search_best_cost": search_cost,
            "quality_profile": bool(alns_quality_profile),
            "quality_score": _score_payload(full_sol),
        },
        "events_path": output_events_path,
        "req_updates": req_updates,
        "veh_updates": veh_updates,
        "route_plan": _route_plan_payload(full_sol),
    }


# -----------------------------
# Main worker: solve one (hour, OD)
# -----------------------------

def solve_od_alns_sa(
    hour: int,
    od: Tuple[RegionId, RegionId],
    requests_od: List[Dict],
    vehicles_od: List[Dict],
    time_budget_sec: float,
    seed: int,
    tmp_dir: str,
    init_strategy: str = "fast",
    init_multistart: int = 8,
    init_regret_k: int = 3,
    init_vehicle_top_k: int = 12,
    init_pos_samples: int = 160,
    init_candidate_pool: int = 24,
    init_time_limit_sec: float = 0.0,
    max_iters: int = 12000,
    speed_kmph: float = 45.0,
    rejection_penalty: float = 300.0,
    alns_selective_route_reevaluation: bool = True,
    alns_lightweight_eval_only: bool = True,
    alns_true_regret2: bool = True,
    sa_cooling_rate: float = 0.998,
    sa_initial_acceptance_probability: float = 0.36787944117144233,
    sa_stagnation_patience_iters: int = 4000,
    sa_stagnation_min_iters: int = 1000,
    sa_accept_rate_window_iters: int = 200,
    sa_high_accept_rate_threshold: float = 0.90,
    alns_quality_profile: bool = False,
    quality_choice_miss_penalty: float = 10000.0,
    quality_choice_excess_weight: float = 25.0,
    quality_near_deadline_buffer_min: float = 1.0,
    quality_regret_k: int = 3,
    quality_unassigned_repair_limit: int = 16,
    quality_reheat_interval_iters: int = 500,
    quality_max_reheats: int = 4,
    quality_choice_loss_acceptance_cap: float = 0.02,
    artifact_tag: str = "",
    initial_routes: Optional[Dict[int, List[Tuple[str, int]]]] = None,
    materialize_output: bool = True,
    alns_noop_operator_adaptation: bool = False,
    t0_samples: int = 0,
) -> Dict:
    """OD 서브문제 해결.

    반환:
      {
        'summary': {...},
        'trace_path': str,
        'events_path': str,
        'req_updates': list[dict],
        'veh_updates': list[dict],
      }
    """
    set_speed_kmph(speed_kmph)
    reset_od_cache_stats()
    if not 0.0 < float(sa_cooling_rate) <= 1.0:
        raise ValueError("sa_cooling_rate must be in (0, 1]")
    if not 0.0 < float(sa_initial_acceptance_probability) < 1.0:
        raise ValueError("sa_initial_acceptance_probability must be in (0, 1)")
    if int(sa_stagnation_patience_iters) < 0:
        raise ValueError("sa_stagnation_patience_iters must be >= 0")
    if int(sa_stagnation_min_iters) < 0:
        raise ValueError("sa_stagnation_min_iters must be >= 0")
    if int(sa_accept_rate_window_iters) <= 0:
        raise ValueError("sa_accept_rate_window_iters must be >= 1")
    if not 0.0 <= float(sa_high_accept_rate_threshold) <= 1.0:
        raise ValueError("sa_high_accept_rate_threshold must be in [0, 1]")
    if float(quality_choice_miss_penalty) <= 0.0:
        raise ValueError("quality_choice_miss_penalty must be > 0")
    if int(quality_regret_k) < 2:
        raise ValueError("quality_regret_k must be >= 2")
    if int(quality_unassigned_repair_limit) < 0:
        raise ValueError("quality_unassigned_repair_limit must be >= 0")
    if int(quality_reheat_interval_iters) < 0 or int(quality_max_reheats) < 0:
        raise ValueError("quality reheating settings must be >= 0")
    if not 0.0 <= float(quality_choice_loss_acceptance_cap) <= 1.0:
        raise ValueError("quality_choice_loss_acceptance_cap must be in [0, 1]")
    if int(t0_samples) < 0:
        raise ValueError("t0_samples must be >= 0")

    global REJECTION_PENALTY
    global _SELECTIVE_ROUTE_REEVALUATION
    global _LIGHTWEIGHT_EVAL_ONLY
    global _TRUE_REGRET2
    global _QUALITY_OBJECTIVE
    global _CHOICE_MISS_PENALTY
    global _CHOICE_EXCESS_WEIGHT
    global _NEAR_DEADLINE_BUFFER_MIN
    REJECTION_PENALTY = rejection_penalty
    _SELECTIVE_ROUTE_REEVALUATION = bool(alns_selective_route_reevaluation)
    _LIGHTWEIGHT_EVAL_ONLY = bool(alns_lightweight_eval_only)
    _TRUE_REGRET2 = bool(alns_true_regret2)
    _QUALITY_OBJECTIVE = bool(alns_quality_profile)
    _CHOICE_MISS_PENALTY = float(quality_choice_miss_penalty)
    _CHOICE_EXCESS_WEIGHT = float(quality_choice_excess_weight)
    _NEAR_DEADLINE_BUFFER_MIN = max(0.0, float(quality_near_deadline_buffer_min))

    o_region, d_region = od
    group_mode = solver_group_mode(od)
    group_label = solver_group_label(od)
    tmp_dir_path = Path(tmp_dir)
    tmp_dir_path.mkdir(parents=True, exist_ok=True)

    safe_tag = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(artifact_tag))
    suffix = f"_{safe_tag}" if safe_tag else ""
    trace_path = tmp_dir_path / f"trace_h{hour}_{o_region}_{d_region}{suffix}.csv"
    events_path = tmp_dir_path / f"events_h{hour}_{o_region}_{d_region}{suffix}.csv"

    # 항상 파일 헤더는 작성한다 (수요 0이어도 추후 merge 안정)
    with _open_with_retry(trace_path, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow(ALNS_TRACE_HEADER)
    with _open_with_retry(events_path, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow(VEHICLE_EVENTS_HEADER)

    # 수요 0이면 빈 결과
    if not requests_od:
        # iter=1 한 줄 기록 (1-based)
        with _open_with_retry(trace_path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([hour, o_region, d_region, 1, 0.0, 0.0, 0.0, 0.0, 0.0, 0, 0, "NONE", "NONE", 0])
        return {
            "summary": {
                "hour": hour,
                "o_region": o_region.value,
                "d_region": d_region.value,
                "group_mode": group_mode,
                "group_label": group_label,
                "candidates": 0,
                "served": 0,
                "forward_candidates": 0,
                "reverse_candidates": 0,
                "iters": 0,
                "time_budget_sec": time_budget_sec,
                "runtime_sec": 0.0,
                "initial_cost": 0.0,
                "final_cost": 0.0,
                "best_cost": 0.0,
                "search_initial_cost": 0.0,
                "search_final_cost": 0.0,
                "search_best_cost": 0.0,
                "accept_rate": 0.0,
                "uphill_accept_rate": 0.0,
                "zero_delta_rate": 0.0,
                "accept_rate_window_final": 0.0,
                "accept_rate_window_peak": 0.0,
                "high_accept_window_count": 0,
                "last_improved_iter": 0,
                "iterations_since_improvement": 0,
                "stopped_early": False,
                "stop_reason": "empty_demand",
                "effective_stagnation_patience_iters": int(
                    sa_stagnation_patience_iters
                ),
                "quality_profile": bool(alns_quality_profile),
                "quality_score": {
                    "not_choice_eligible": 0,
                    "choice_eligible": 0,
                    "unassigned": 0,
                },
                "t0_runtime_sec": 0.0,
                "t0_samples_effective": 0,
                "operator_stats": {"destroy": {}, "repair": {}},
                "od_cache_stats": get_od_cache_stats(),
            },
            "trace_path": str(trace_path),
            "events_path": str(events_path),
            "req_updates": [],
            "veh_updates": [],
            "route_plan": {},
        }

    # Build the exact same immutable inputs used by winner-only materialization.
    req_map, vehs = _build_problem_objects(requests_od, vehicles_od)

    req_ids = list(req_map.keys())
    if o_region == d_region:
        forward_candidates = len(req_ids)
        reverse_candidates = 0
    else:
        forward_candidates = sum(
            1 for r in requests_od if RegionId(r["o_region"]) == o_region and RegionId(r["d_region"]) == d_region
        )
        reverse_candidates = sum(
            1 for r in requests_od if RegionId(r["o_region"]) == d_region and RegionId(r["d_region"]) == o_region
        )

    rng = random.Random(seed)

    # 1) 초기해
    t0 = time.time()
    strategy = str(init_strategy).strip().lower()
    if initial_routes is not None:
        sol = _solution_from_route_plan(initial_routes, vehs, req_map)
    elif strategy == "premium":
        sol = _build_initial_solution_regret_multistart(
            req_ids=req_ids,
            vehs=vehs,
            req_map=req_map,
            rng=rng,
            starts=max(1, int(init_multistart)),
            regret_k=max(2, int(init_regret_k)),
            position_samples=max(40, int(init_pos_samples)),
            top_k_vehicles=max(1, int(init_vehicle_top_k)),
            candidate_pool_size=max(4, int(init_candidate_pool)),
            max_time_sec=float(init_time_limit_sec),
        )
    else:
        sol = build_initial_solution(req_ids, vehs, req_map)
    initial_cost = sol.total_cost
    initial_legacy_cost = _legacy_objective(sol)
    initial_quality_score = _score_payload(sol)
    best_sol = sol
    best_cost = initial_cost
    current_cost = initial_cost

    # time_budget이 0에 가까우면 ALNS skip
    if time_budget_sec <= 0.001:
        runtime = time.time() - t0
        # trace iter=1 (1-based)
        with _open_with_retry(trace_path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([hour, o_region, d_region, 1, 0.0, current_cost, current_cost, best_cost, 0.0, 1, 1, "INIT", "INIT", 0])

        # Premium initializer may contain lightweight RouteEval objects.  A
        # portfolio search-only worker deliberately leaves them lightweight;
        # the selected winner can be expanded later by materialize_route_plan.
        final_legacy_cost = _legacy_objective(sol)
        best_legacy_cost = _legacy_objective(best_sol)
        if materialize_output:
            best_sol = _materialize_full_solution(best_sol, vehs, req_map)
            best_legacy_cost = _legacy_objective(best_sol)
            req_updates, veh_updates = _finalize_outputs(best_sol, vehs, req_map)
            _write_events_from_solution(events_path, best_sol, vehs)
        else:
            req_updates, veh_updates = [], []
        effective_stagnation_patience_iters = _effective_stagnation_patience(
            int(sa_stagnation_patience_iters), max(1, int(max_iters)), best_sol
        )

        return {
            "summary": {
                "hour": hour,
                "o_region": o_region.value,
                "d_region": d_region.value,
                "group_mode": group_mode,
                "group_label": group_label,
                "candidates": len(req_ids),
                "served": len(req_ids) - len(best_sol.unassigned),
                "forward_candidates": forward_candidates,
                "reverse_candidates": reverse_candidates,
                "iters": 0,
                "time_budget_sec": time_budget_sec,
                "runtime_sec": runtime,
                "initial_cost": initial_legacy_cost,
                "final_cost": final_legacy_cost,
                "best_cost": best_legacy_cost,
                "search_initial_cost": initial_cost,
                "search_final_cost": current_cost,
                "search_best_cost": best_cost,
                "accept_rate": 0.0,
                "uphill_accept_rate": 0.0,
                "zero_delta_rate": 0.0,
                "accept_rate_window_final": 0.0,
                "accept_rate_window_peak": 0.0,
                "high_accept_window_count": 0,
                "last_improved_iter": 0,
                "iterations_since_improvement": 0,
                "stopped_early": False,
                "stop_reason": "zero_time_budget",
                "effective_stagnation_patience_iters": (
                    effective_stagnation_patience_iters
                ),
                "quality_profile": bool(alns_quality_profile),
                "quality_score": _score_payload(best_sol),
                "t0_runtime_sec": 0.0,
                "t0_samples_effective": 0,
                "operator_stats": {"destroy": {}, "repair": {}},
                "od_cache_stats": get_od_cache_stats(),
            },
            "trace_path": str(trace_path),
            "events_path": str(events_path),
            "req_updates": req_updates,
            "veh_updates": veh_updates,
            "route_plan": _route_plan_payload(best_sol),
        }

    # 2) SA 파라미터
    auto_t0_samples = min(8, max(2, len(req_ids) // 10))
    t0_samples_effective = (
        int(t0_samples) if int(t0_samples) > 0 else auto_t0_samples
    )
    t0_started = time.perf_counter()
    raw_t0 = estimate_T0(
        sol, vehs, req_map, rng, samples=t0_samples_effective
    )
    t0_runtime_sec = time.perf_counter() - t0_started
    # At the mean sampled delta, exp(-delta / T0) equals the configured target.
    T = raw_t0 / -math.log(float(sa_initial_acceptance_probability))
    initial_temperature = T
    cooling_rate = float(sa_cooling_rate)

    # destroy/repair operators
    destroy_ops = [
        "RANDOM",
        "SHAW",
        "WORST",
        "ROUTE_SEGMENT",
        "ROUTE_TAIL",
        "TWO_OPT_STAR",
        "CROSS_EXCHANGE",
    ]
    if _QUALITY_OBJECTIVE:
        destroy_ops.append("CHOICE_CRITICAL")
    repair_ops = ["GREEDY", "REGRET2"]
    if _QUALITY_OBJECTIVE and int(quality_regret_k) >= 3:
        repair_ops.append("REGRET3")
    destroy_weights = {op: 1.0 for op in destroy_ops}
    repair_weights = {op: 1.0 for op in repair_ops}
    destroy_stats = _new_operator_stats(destroy_ops)
    repair_stats = _new_operator_stats(repair_ops)
    stagnation_iters = 0
    route_destroy_used = 0
    travel_cache_alns: Dict[Tuple[float, float, float, float], Tuple[float, float]] = {}

    start = time.time()
    iter_idx = 0
    accept_count = 0
    uphill_proposal_count = 0
    uphill_accept_count = 0
    zero_delta_count = 0
    last_improved_iter = 0
    stopped_early = False
    stop_reason = "iteration_or_time_limit"
    accept_window: Deque[int] = deque()
    accept_window_sum = 0
    accept_window_peak = 0.0
    high_accept_window_count = 0
    no_op_count = 0
    choice_loss_proposal_count = 0
    choice_loss_accept_count = 0
    reheat_count = 0
    accept_window_size = max(1, int(sa_accept_rate_window_iters))
    # 수요 규모에 따라 iter_cap을 적응적으로 조절 (소규모 OD에서 불필요한 반복 절감)
    if _QUALITY_OBJECTIVE:
        iter_cap = max(1, int(max_iters))
    else:
        iter_cap = min(max(1, int(max_iters)), max(500, 80 * len(req_ids)))
    effective_stagnation_patience_iters = _effective_stagnation_patience(
        int(sa_stagnation_patience_iters), iter_cap, best_sol
    )

    # trace 시작: iter=1은 초기해로 기록 (1-based)
    # PERF-2: 파일을 한 번 열고 루프 전체에서 재사용하여 I/O 오버헤드 제거
    _trace_f = _open_with_retry(trace_path, "a", newline="", encoding="utf-8")
    _trace_w = csv.writer(_trace_f)
    _trace_w.writerow([hour, o_region, d_region, 1, T, current_cost, current_cost, best_cost, 0.0, 1, 1, "INIT", "INIT", 0])

    try:
        while (time.time() - start) < time_budget_sec and iter_idx < iter_cap:
            iter_idx += 1

            n = len(req_ids)
            q = _adaptive_destroy_q(n, stagnation_iters, rng)
            if _QUALITY_OBJECTIVE and n >= 120:
                q = min(q, max(6, int(math.sqrt(n))))

            effective_destroy_weights = _effective_destroy_weights(
                base_weights=destroy_weights,
                n=n,
                stagnation_iters=stagnation_iters,
                iter_idx=iter_idx,
                route_destroy_used=route_destroy_used,
            )
            d_op = _weighted_pick(rng, destroy_ops, effective_destroy_weights)

            removed: Set[int] = set()
            destroy_started = time.perf_counter()
            if d_op == "TWO_OPT_STAR":
                r_op = "DIRECT"
                cand = local_two_opt_star(sol, vehs, req_map, rng)
                destroy_runtime_sec = time.perf_counter() - destroy_started
            elif d_op == "CROSS_EXCHANGE":
                r_op = "DIRECT"
                cand = local_cross_exchange(sol, vehs, req_map, rng)
                destroy_runtime_sec = time.perf_counter() - destroy_started
            else:
                r_op = _weighted_pick(rng, repair_ops, repair_weights)
                if d_op == "RANDOM":
                    removed = destroy_random(sol, q, rng)
                elif d_op == "SHAW":
                    removed = destroy_shaw(sol, q, rng, req_map)
                elif d_op == "ROUTE_SEGMENT":
                    removed = destroy_route_segment(sol, q, rng)
                    route_destroy_used += 1
                elif d_op == "ROUTE_TAIL":
                    removed = destroy_route_tail(sol, q)
                    route_destroy_used += 1
                elif d_op == "CHOICE_CRITICAL":
                    removed = destroy_choice_critical(sol, q, req_map, rng)
                else:
                    removed = destroy_worst(sol, q)
                if not removed:
                    removed = destroy_random(sol, q, rng)

                sol_removed = remove_requests(sol, removed, vehs, req_map, travel_cache=travel_cache_alns)
                destroy_runtime_sec = time.perf_counter() - destroy_started
                repair_started = time.perf_counter()
                repair_ids = set(removed)
                if _QUALITY_OBJECTIVE:
                    repair_ids.update(
                        _select_unassigned_for_repair(
                            sol_removed,
                            req_map,
                            min(
                                int(quality_unassigned_repair_limit),
                                max(2, q // 2),
                            ),
                            rng,
                            exclude=removed,
                        )
                    )

                # 정밀 복구: 복구 시 차량 후보와 삽입 위치를 확대하여 정밀 탐색
                precise_position_samples = max(80, min(220, 70 + int(0.6 * q)))
                precise_top_k_vehicles = max(6, min(14, len(vehs)))
                precise_exact_top = max(2, min(4, len(vehs)))
                precise_exact_route_len = 34

                if r_op == "GREEDY":
                    cand = repair_greedy_precise(
                        sol_removed,
                        repair_ids,
                        vehs,
                        req_map,
                        rng,
                        position_samples=precise_position_samples,
                        top_k_vehicles=precise_top_k_vehicles,
                        exact_top_vehicle_count=precise_exact_top,
                        exact_route_len_limit=precise_exact_route_len,
                        travel_cache=travel_cache_alns,
                    )
                else:
                    cand = repair_regret2_precise(
                        sol_removed,
                        repair_ids,
                        vehs,
                        req_map,
                        rng,
                        position_samples=precise_position_samples,
                        top_k_vehicles=precise_top_k_vehicles,
                        exact_top_vehicle_count=precise_exact_top,
                        exact_route_len_limit=precise_exact_route_len,
                        travel_cache=travel_cache_alns,
                        regret_k=(
                            int(quality_regret_k) if r_op == "REGRET3" else 2
                        ),
                    )

                repair_runtime_sec = time.perf_counter() - repair_started

            destroy_row = destroy_stats[d_op]
            destroy_row["calls"] = int(destroy_row["calls"]) + 1
            destroy_row["runtime_sec"] = (
                float(destroy_row["runtime_sec"]) + destroy_runtime_sec
            )
            if r_op in repair_stats:
                repair_row = repair_stats[r_op]
                repair_row["calls"] = int(repair_row["calls"]) + 1
                repair_row["runtime_sec"] = (
                    float(repair_row["runtime_sec"]) + repair_runtime_sec
                )

            cand_cost = cand.total_cost
            prev_cost = current_cost
            delta = cand_cost - prev_cost
            is_noop = _solution_signature(cand) == _solution_signature(sol)
            skip_noop = bool(is_noop)
            if skip_noop:
                # A structural identity has the same route evaluation and
                # objective by construction.  Avoid rebuilding quality tuples
                # and best comparisons for this iteration.
                current_score = None
                candidate_score = None
                improved_current = False
                primary_choice_loss = False
            else:
                current_score = solution_score(sol)
                candidate_score = solution_score(cand)
                improved_current = (
                    candidate_score < current_score
                    if _QUALITY_OBJECTIVE
                    else cand_cost < prev_cost
                )
                primary_choice_loss = (
                    candidate_score.not_choice_eligible
                    > current_score.not_choice_eligible
                )
            if is_noop:
                no_op_count += 1
            if delta > 0.0:
                uphill_proposal_count += 1
            elif delta == 0.0:
                zero_delta_count += 1

            accepted = 0
            improved_best = 0

            if skip_noop:
                accepted = 0
            elif _QUALITY_OBJECTIVE and candidate_score < current_score:
                accepted = 1
            elif _QUALITY_OBJECTIVE and primary_choice_loss:
                choice_loss_proposal_count += 1
                if cand_cost <= prev_cost:
                    p = 1.0
                else:
                    p = math.exp(-(cand_cost - prev_cost) / max(1e-9, T))
                p = min(p, float(quality_choice_loss_acceptance_cap))
                if rng.random() < p:
                    accepted = 1
            elif cand_cost <= prev_cost:
                accepted = 1
            else:
                p = math.exp(-(cand_cost - prev_cost) / max(1e-9, T))
                if rng.random() < p:
                    accepted = 1

            if accepted:
                sol = cand
                current_cost = cand_cost
                accept_count += 1
                if delta > 0.0:
                    uphill_accept_count += 1
                if primary_choice_loss:
                    choice_loss_accept_count += 1

            if not skip_noop and _is_better_solution(cand, best_sol):
                best_sol = cand
                best_cost = cand_cost
                improved_best = 1
                stagnation_iters = 0
                last_improved_iter = iter_idx
            else:
                stagnation_iters += 1

            if not is_noop:
                if len(accept_window) == accept_window_size:
                    accept_window_sum -= accept_window.popleft()
                accept_window.append(accepted)
                accept_window_sum += accepted
                window_rate = accept_window_sum / len(accept_window)
                if len(accept_window) == accept_window_size:
                    accept_window_peak = max(accept_window_peak, window_rate)
                    if window_rate >= float(sa_high_accept_rate_threshold):
                        high_accept_window_count += 1

            if not is_noop:
                reward = _operator_reward(
                    improved_best=bool(improved_best),
                    accepted=bool(accepted),
                    delta=delta,
                    ref_cost=prev_cost,
                )
                destroy_weights[d_op] = _update_weight(destroy_weights[d_op], reward)
                if r_op in repair_weights:
                    repair_weights[r_op] = _update_weight(repair_weights[r_op], reward)
            elif alns_noop_operator_adaptation:
                # Keep the historical no-op/trace semantics, but let an
                # explicitly enabled profile gradually reduce unproductive
                # operator weights.  _update_weight supplies the bounded 0.05
                # base floor; TWO_OPT_STAR still receives its existing
                # diversity multiplier in _effective_destroy_weights.
                destroy_weights[d_op] = _update_weight(
                    destroy_weights[d_op], 0.0
                )
                if r_op in repair_weights:
                    repair_weights[r_op] = _update_weight(
                        repair_weights[r_op], 0.0
                    )

            _record_operator_outcome(
                destroy_stats,
                d_op,
                is_noop=is_noop,
                accepted=bool(accepted),
                improved_current=bool(improved_current),
                best_improved=bool(improved_best),
            )
            if r_op in repair_stats:
                _record_operator_outcome(
                    repair_stats,
                    r_op,
                    is_noop=is_noop,
                    accepted=bool(accepted),
                    improved_current=bool(improved_current),
                    best_improved=bool(improved_best),
                )

            # trace 기록 (배치 쓰기: 파일은 이미 열려 있음)
            _trace_w.writerow(
                [
                    hour,
                    o_region,
                    d_region,
                    iter_idx + 1,
                    T,
                    current_cost,
                    cand_cost,
                    best_cost,
                    delta,
                    accepted,
                    improved_best,
                    d_op,
                    r_op,
                    len(removed),
                ]
            )
            # 200 iteration마다 flush (메모리/crash 안전)
            if iter_idx % 200 == 0:
                _trace_f.flush()

            if not is_noop:
                T *= cooling_rate

            reheat_interval = int(quality_reheat_interval_iters)
            if (
                _QUALITY_OBJECTIVE
                and reheat_interval > 0
                and reheat_count < int(quality_max_reheats)
                and stagnation_iters >= reheat_interval * (reheat_count + 1)
            ):
                # Restart from the elite best and re-open the SA acceptance
                # window.  The archive itself remains lexicographically safe.
                sol = best_sol
                current_cost = best_sol.total_cost
                T = max(T, initial_temperature * max(0.18, 0.55 ** (reheat_count + 1)))
                reheat_count += 1

            # The effective policy depends only on the elite best solution.
            # Recompute it only when that elite changes; scanning every route
            # on every iteration is pure overhead on long hard cases.
            if improved_best:
                effective_stagnation_patience_iters = (
                    _effective_stagnation_patience(
                        int(sa_stagnation_patience_iters), iter_cap, best_sol
                    )
                )
            patience = effective_stagnation_patience_iters
            if (
                patience > 0
                and iter_idx < iter_cap
                and (time.time() - start) < time_budget_sec
                and iter_idx >= int(sa_stagnation_min_iters)
                and (iter_idx - last_improved_iter) >= patience
            ):
                stopped_early = True
                stop_reason = "stagnation_patience"
                break
    finally:
        _trace_f.close()

    if not stopped_early:
        stop_reason = (
            "iteration_cap" if iter_idx >= iter_cap else "time_budget"
        )
    runtime = time.time() - t0
    accept_rate = accept_count / max(1, iter_idx)
    uphill_accept_rate = uphill_accept_count / max(1, uphill_proposal_count)
    zero_delta_rate = zero_delta_count / max(1, iter_idx)
    no_op_rate = no_op_count / max(1, iter_idx)
    productive_iters = max(0, iter_idx - no_op_count)
    productive_accept_rate = accept_count / max(1, productive_iters)
    accept_rate_window_final = accept_window_sum / max(1, len(accept_window))
    if iter_idx < accept_window_size:
        accept_window_peak = accept_rate_window_final

    # Candidate RouteEval objects intentionally omit output details.  Keep the
    # historical public objective in its original units/schema; the quality
    # search energy is exposed through additive ``search_*`` summary fields.
    final_legacy_cost = _legacy_objective(sol)
    best_search_cost = best_cost
    best_legacy_cost = _legacy_objective(best_sol)
    if materialize_output:
        best_sol = _materialize_full_solution(best_sol, vehs, req_map)
        best_legacy_cost = _legacy_objective(best_sol)

        # 최종 결과 산출 (best_sol)
        req_updates, veh_updates = _finalize_outputs(best_sol, vehs, req_map)
        _write_events_from_solution(events_path, best_sol, vehs)
    else:
        req_updates, veh_updates = [], []

    return {
        "summary": {
            "hour": hour,
            "o_region": o_region.value,
            "d_region": d_region.value,
            "group_mode": group_mode,
            "group_label": group_label,
            "candidates": len(req_ids),
            "served": len(req_ids) - len(best_sol.unassigned),
            "forward_candidates": forward_candidates,
            "reverse_candidates": reverse_candidates,
            "iters": iter_idx,
            "time_budget_sec": time_budget_sec,
            "runtime_sec": runtime,
            "initial_cost": initial_legacy_cost,
            "final_cost": final_legacy_cost,
            "best_cost": best_legacy_cost,
            "search_initial_cost": initial_cost,
            "search_final_cost": current_cost,
            "search_best_cost": best_search_cost,
            "accept_rate": accept_rate,
            "uphill_accept_rate": uphill_accept_rate,
            "zero_delta_rate": zero_delta_rate,
            "accept_rate_window_final": accept_rate_window_final,
            "accept_rate_window_peak": accept_window_peak,
            "high_accept_window_count": high_accept_window_count,
            "initial_temperature": initial_temperature,
            "last_improved_iter": last_improved_iter,
            "iterations_since_improvement": iter_idx - last_improved_iter,
            "stopped_early": stopped_early,
            "stop_reason": stop_reason,
            "effective_stagnation_patience_iters": (
                effective_stagnation_patience_iters
            ),
            "quality_profile": bool(alns_quality_profile),
            "initial_quality_score": initial_quality_score,
            "quality_score": _score_payload(best_sol),
            "no_op_count": no_op_count,
            "no_op_rate": no_op_rate,
            "productive_iters": productive_iters,
            "productive_accept_rate": productive_accept_rate,
            "choice_loss_proposal_count": choice_loss_proposal_count,
            "choice_loss_accept_count": choice_loss_accept_count,
            "reheat_count": reheat_count,
            "t0_runtime_sec": t0_runtime_sec,
            "t0_samples_effective": t0_samples_effective,
            "operator_stats": _operator_stats_payload(
                destroy_stats,
                repair_stats,
                destroy_weights,
                repair_weights,
            ),
            "od_cache_stats": get_od_cache_stats(),
        },
        "trace_path": str(trace_path),
        "events_path": str(events_path),
        "req_updates": req_updates,
        "veh_updates": veh_updates,
        "route_plan": _route_plan_payload(best_sol),
    }


def _finalize_outputs(sol: Solution, vehs: List[Veh], req_map: Dict[int, Req]) -> Tuple[List[Dict], List[Dict]]:
    """best_sol을 기반으로 request 업데이트(픽업/드랍/탑승거리/OFV)와 차량 업데이트(종료상태/VMT)를 만든다."""
    req_updates: List[Dict] = []
    veh_updates: List[Dict] = []

    # 각 차량 route_eval은 이미 cache되어 있음
    for v in vehs:
        ev = sol.route_eval[v.veh_id]
        veh_updates.append(
            {
                "veh_id": v.veh_id,
                "finish_time_min": ev.finish_time_min,
                "finish_lat": ev.finish_lat,
                "finish_lon": ev.finish_lon,
                "vmt_total_km": ev.vmt_total_km,
                "vmt_deadhead_km": ev.vmt_deadhead_km,
                "vmt_service_km": ev.vmt_service_km,
                "time_wait_min": ev.time_wait_min,
                "time_deadhead_min": ev.time_deadhead_min,
                "time_service_min": ev.time_service_min,
            }
        )

        # 요청 업데이트
        for rid, do_t in ev.dropoff_time.items():
            req = req_map[rid]
            pu_t = ev.pickup_time.get(rid, float("nan"))
            onboard_dist = ev.onboard_dist.get(rid, 0.0)
            drt_time = do_t - req.call_time_min
            lateness = max(0.0, do_t - req.latest_min)
            obj_base = drt_time
            obj_penalty = 4.0 * lateness
            obj_total = obj_base + obj_penalty

            req_updates.append(
                {
                    "req_id": rid,
                    "vehicle_id": v.veh_id,
                    "pickup_time_min": pu_t,
                    "dropoff_time_min": do_t,
                    "drt_time_min": drt_time,
                    "onboard_dist_km": onboard_dist,
                    "lateness_min": lateness,
                    "obj_base": obj_base,
                    "obj_penalty": obj_penalty,
                    "obj_total": obj_total,
                }
            )

    # 미배정 요청 처리 (drt_time_min = -1.0, vehicle_id = 빈 문자열)
    for rid in sol.unassigned:
        req_updates.append(
            {
                "req_id": rid,
                "vehicle_id": "",
                "pickup_time_min": -1.0,
                "dropoff_time_min": -1.0,
                "drt_time_min": -1.0,
                "onboard_dist_km": 0.0,
                "lateness_min": 0.0,
                "obj_base": 0.0,
                "obj_penalty": REJECTION_PENALTY,
                "obj_total": REJECTION_PENALTY,
            }
        )

    # sanity: 모든 요청이 포함돼야 한다 (배정 + 미배정 = 전체)
    if len(req_updates) != len(req_map):
        raise RuntimeError(f"서빙 누락: updates={len(req_updates)} vs reqs={len(req_map)}")

    return req_updates, veh_updates


def _write_events_from_solution(events_path: Path, sol: Solution, vehs: List[Veh]) -> None:
    """worker 내부에서 events 파일에 append. (헤더는 이미 작성되어 있음)

    요구사항: vehicle_events.csv는 WAIT/PICKUP/DROPOFF만 저장하고, 이동은 이벤트행의
    move_dist/time 컬럼으로 합계 형태로 남긴다.
    """
    with _open_with_retry(events_path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        for v in vehs:
            ev = sol.route_eval[v.veh_id]
            for row in ev.events:
                w.writerow(row)
