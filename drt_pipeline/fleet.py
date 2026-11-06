"""차량(fleet) 초기화 및 OD 타겟 배분 관련 유틸."""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Dict, List, Tuple

from .constants import (
    OD_LIST,
    REGION_ORDER_INDEX,
    SOLVER_GROUP_LIST,
    RegionId,
    SIX_REGIONS,
    canonical_solver_group,
)
from .regions import region_center, map_to_region
from .travel import travel


@dataclass
class VehicleState:
    veh_id: int
    cap: int

    # carry-over 상태
    start_time_min: float
    start_lat: float
    start_lon: float

    finish_time_min: float
    finish_lat: float
    finish_lon: float

    # 누적 KPI
    vmt_total_km: float = 0.0
    vmt_deadhead_km: float = 0.0
    vmt_service_km: float = 0.0
    time_wait_min: float = 0.0
    time_deadhead_min: float = 0.0
    time_service_min: float = 0.0
    served_count: int = 0


def proportional_integer_allocation(demand: Dict, total_units: int) -> Dict:
    """largest remainder + demand>0이면 최소 1.

    demand 키는 region 또는 OD 등 어떤 해시 가능한 타입이어도 됨.
    """
    keys = list(demand.keys())
    total_demand = sum(demand.values())
    raw = {}
    base = {}
    rem = {}
    for k in keys:
        raw[k] = 0.0 if total_demand == 0 else total_units * demand[k] / total_demand
        base[k] = int(raw[k])  # floor
        rem[k] = raw[k] - base[k]

    # demand>0 최소 1
    for k in keys:
        if demand[k] > 0 and base[k] == 0:
            base[k] = 1

    assigned = sum(base.values())

    # overshoot: base>1인 곳에서 demand 작은 순으로 감산
    while assigned > total_units:
        candidates = [k for k in keys if base[k] > 1]
        if not candidates:
            break
        k = min(candidates, key=lambda x: demand[x])
        base[k] -= 1
        assigned -= 1

    # undershoot: rem 큰 순으로 증산
    while assigned < total_units:
        k = max(keys, key=lambda x: rem[x])
        base[k] += 1
        rem[k] = 0.0
        assigned += 1

    return base


def init_fleet_from_requests(
    requests,
    fleet_size: int = 300,
    cap: int = 15,
) -> List[VehicleState]:
    """요청의 origin region 수요 비율로 6개 region 중심점에 분산 배치."""
    demand_by_region = {r: 0 for r in SIX_REGIONS}
    for req in requests:
        if req.o_region == RegionId.OUTSIDE or req.d_region == RegionId.OUTSIDE:
            continue
        demand_by_region[req.o_region] += 1

    alloc = proportional_integer_allocation(demand_by_region, fleet_size)

    vehicles: List[VehicleState] = []
    veh_id = 0
    for region in SIX_REGIONS:
        n = alloc[region]
        c_lat, c_lon = region_center(region)
        for _ in range(n):
            vehicles.append(
                VehicleState(
                    veh_id=veh_id,
                    cap=cap,
                    start_time_min=0.0,
                    start_lat=c_lat,
                    start_lon=c_lon,
                    finish_time_min=0.0,
                    finish_lat=c_lat,
                    finish_lon=c_lon,
                )
            )
            veh_id += 1

    # 혹시 rounding으로 인해 fleet_size가 안 맞는 경우를 방어
    if len(vehicles) != fleet_size:
        # 부족하면 DJ_SW에 추가, 초과하면 끝에서 자름
        while len(vehicles) < fleet_size:
            c_lat, c_lon = region_center(RegionId.DJ_SW)
            vehicles.append(
                VehicleState(
                    veh_id=len(vehicles),
                    cap=cap,
                    start_time_min=0.0,
                    start_lat=c_lat,
                    start_lon=c_lon,
                    finish_time_min=0.0,
                    finish_lat=c_lat,
                    finish_lon=c_lon,
                )
            )
        vehicles = vehicles[:fleet_size]

    return vehicles


def compute_od_targets(requests_by_od: Dict[Tuple[RegionId, RegionId], List], fleet_size: int = 300) -> Dict:
    demand = {od: len(requests_by_od.get(od, [])) for od in OD_LIST}
    return proportional_integer_allocation(demand, fleet_size)


def compute_solver_group_targets(
    requests_by_group: Dict[Tuple[RegionId, RegionId], List],
    fleet_size: int = 300,
    strategy: str = "demand",
) -> Dict[Tuple[RegionId, RegionId], int]:
    if strategy == "demand":
        demand = {group: len(requests_by_group.get(group, [])) for group in SOLVER_GROUP_LIST}
        return proportional_integer_allocation(demand, fleet_size)
    if strategy != "workload_deadline":
        raise ValueError("solver group target strategy must be demand or workload_deadline")
    workloads = compute_solver_group_workloads(requests_by_group)
    return _allocate_active_weights(workloads, fleet_size)


def compute_solver_group_workloads(
    requests_by_group: Dict[Tuple[RegionId, RegionId], List],
    burst_window_min: int = 15,
) -> Dict[Tuple[RegionId, RegionId], float]:
    """Estimate service effort, deadline pressure, and within-hour bursts."""
    scores: Dict[Tuple[RegionId, RegionId], float] = {}
    window_size = max(1, int(burst_window_min))
    for group in SOLVER_GROUP_LIST:
        requests = requests_by_group.get(group, [])
        if not requests:
            scores[group] = 0.0
            continue
        direct_times: List[float] = []
        weighted_service = 0.0
        bins: Dict[int, int] = {}
        for req in requests:
            _, direct_min = travel(
                float(req["pickup_lat"]),
                float(req["pickup_lon"]),
                float(req["drop_lat"]),
                float(req["drop_lon"]),
            )
            direct_min = max(0.01, float(direct_min))
            call = float(req["call_time_min"])
            deadline = float(req.get("choice_deadline_min", req["latest_min"]))
            available = max(0.01, deadline - call)
            pressure = min(3.0, direct_min / available)
            weighted_service += direct_min * (1.0 + pressure)
            direct_times.append(direct_min)
            bucket = int(call // window_size)
            bins[bucket] = bins.get(bucket, 0) + 1
        peak = max(bins.values(), default=0)
        median_direct = statistics.median(direct_times)
        scores[group] = weighted_service + 0.5 * peak * median_direct
    return scores


def _allocate_active_weights(
    weights: Dict[Tuple[RegionId, RegionId], float],
    total_units: int,
) -> Dict[Tuple[RegionId, RegionId], int]:
    """Largest-remainder allocation with inactive=0 and active minimum one."""
    out = {group: 0 for group in SOLVER_GROUP_LIST}
    if total_units <= 0:
        return out
    active = [group for group in SOLVER_GROUP_LIST if weights.get(group, 0.0) > 0.0]
    if not active:
        return out
    if len(active) > total_units:
        for group in sorted(active, key=lambda g: (-weights[g], REGION_ORDER_INDEX[g[0]], REGION_ORDER_INDEX[g[1]]))[:total_units]:
            out[group] = 1
        return out

    for group in active:
        out[group] = 1
    remaining = total_units - len(active)
    total_weight = sum(weights[group] for group in active)
    raw = {
        group: (remaining * weights[group] / total_weight if total_weight > 0 else 0.0)
        for group in active
    }
    for group in active:
        out[group] += int(raw[group])
    left = total_units - sum(out.values())
    order = sorted(
        active,
        key=lambda group: (
            -(raw[group] - int(raw[group])),
            -weights[group],
            REGION_ORDER_INDEX[group[0]],
            REGION_ORDER_INDEX[group[1]],
        ),
    )
    for group in order[:left]:
        out[group] += 1
    return out


def _group_total_demand(
    group: Tuple[RegionId, RegionId],
    requests_by_group: Dict[Tuple[RegionId, RegionId], List],
) -> int:
    return len(requests_by_group.get(group, []))


def _group_origin_demand(
    group: Tuple[RegionId, RegionId],
    requests_by_group: Dict[Tuple[RegionId, RegionId], List],
    origin_region: RegionId,
) -> int:
    return sum(1 for req in requests_by_group.get(group, []) if req["o_region"] == origin_region)


def _group_has_endpoint(group: Tuple[RegionId, RegionId], region: RegionId) -> bool:
    return group[0] == region or group[1] == region


def _group_distance_to_vehicle(
    group: Tuple[RegionId, RegionId],
    vehicle: VehicleState,
) -> float:
    endpoints = [group[0]] if group[0] == group[1] else [group[0], group[1]]
    best_tt = float("inf")
    for endpoint in endpoints:
        c_lat, c_lon = region_center(endpoint)
        _, tt = travel(vehicle.start_lat, vehicle.start_lon, c_lat, c_lon)
        best_tt = min(best_tt, tt)
    return best_tt


def assign_vehicles_to_ods(
    vehicles: List[VehicleState],
    od_targets: Dict[Tuple[RegionId, RegionId], int],
    requests_by_od: Dict[Tuple[RegionId, RegionId], List],
) -> Dict[Tuple[RegionId, RegionId], List[int]]:
    """차량을 OD별로 배정. 이동 최소화를 위해 현재 위치의 region==origin인 OD를 우선.

    Option A: 이 배정 결과가 해당 hour의 OD 서브문제에 고정 적용.
    """
    demand = {od: len(requests_by_od.get(od, [])) for od in OD_LIST}

    remaining = dict(od_targets)
    assignments = {od: [] for od in OD_LIST}

    vehicles_sorted = sorted(vehicles, key=lambda v: (v.start_time_min, v.veh_id))

    for v in vehicles_sorted:
        cur_region = map_to_region(v.start_lat, v.start_lon)

        # Tier1: origin==cur_region
        tier1 = [od for od in OD_LIST if od[0] == cur_region and remaining.get(od, 0) > 0]
        if tier1:
            chosen = max(tier1, key=lambda od: demand[od])
            assignments[chosen].append(v.veh_id)
            remaining[chosen] -= 1
            continue

        # Tier2: quota 남은 OD 중 origin 중심점까지 이동시간이 최소인 OD 선택
        best_od = None
        best_score = float("inf")
        for od in OD_LIST:
            if remaining.get(od, 0) <= 0:
                continue
            o_region = od[0]
            c_lat, c_lon = region_center(o_region)
            _, tt = travel(v.start_lat, v.start_lon, c_lat, c_lon)
            score = tt - 0.001 * demand[od]  # 이동 최소가 우선, demand는 미세 가중
            if score < best_score:
                best_score = score
                best_od = od

        if best_od is None:
            # quota가 남은 곳이 없음: 남는 차량은 demand 최대 OD에 그냥 넣어둠
            best_od = max(OD_LIST, key=lambda od: demand[od])

        assignments[best_od].append(v.veh_id)
        if remaining.get(best_od, 0) > 0:
            remaining[best_od] -= 1

    return assignments


def assign_vehicles_to_solver_groups(
    vehicles: List[VehicleState],
    group_targets: Dict[Tuple[RegionId, RegionId], int],
    requests_by_group: Dict[Tuple[RegionId, RegionId], List],
) -> Dict[Tuple[RegionId, RegionId], List[int]]:
    """Assign vehicles to same-region or bidirectional inter-region solver groups.

    Priority:
    1. Groups with immediate-demand requests that start in the vehicle's current region.
    2. Groups whose endpoints include the vehicle's current region.
    3. Any remaining group with the shortest travel time to either endpoint center.
    """
    remaining = dict(group_targets)
    assignments = {group: [] for group in SOLVER_GROUP_LIST}
    vehicles_sorted = sorted(vehicles, key=lambda v: (v.start_time_min, v.veh_id))

    for vehicle in vehicles_sorted:
        cur_region = map_to_region(vehicle.start_lat, vehicle.start_lon)

        tier1 = [
            group
            for group in SOLVER_GROUP_LIST
            if remaining.get(group, 0) > 0 and _group_origin_demand(group, requests_by_group, cur_region) > 0
        ]
        if tier1:
            chosen = max(
                tier1,
                key=lambda group: (
                    _group_origin_demand(group, requests_by_group, cur_region),
                    _group_total_demand(group, requests_by_group),
                    -REGION_ORDER_INDEX[group[0]],
                    -REGION_ORDER_INDEX[group[1]],
                ),
            )
            assignments[chosen].append(vehicle.veh_id)
            remaining[chosen] -= 1
            continue

        tier2 = [
            group
            for group in SOLVER_GROUP_LIST
            if remaining.get(group, 0) > 0 and _group_has_endpoint(group, cur_region)
        ]
        if tier2:
            chosen = max(
                tier2,
                key=lambda group: (
                    _group_total_demand(group, requests_by_group),
                    -_group_distance_to_vehicle(group, vehicle),
                    -REGION_ORDER_INDEX[group[0]],
                    -REGION_ORDER_INDEX[group[1]],
                ),
            )
            assignments[chosen].append(vehicle.veh_id)
            remaining[chosen] -= 1
            continue

        best_group = None
        best_score = float("inf")
        for group in SOLVER_GROUP_LIST:
            if remaining.get(group, 0) <= 0:
                continue
            score = _group_distance_to_vehicle(group, vehicle) - 0.001 * _group_total_demand(group, requests_by_group)
            if score < best_score:
                best_score = score
                best_group = group

        if best_group is None:
            best_group = max(SOLVER_GROUP_LIST, key=lambda group: _group_total_demand(group, requests_by_group))

        assignments[best_group].append(vehicle.veh_id)
        if remaining.get(best_group, 0) > 0:
            remaining[best_group] -= 1

    return assignments


def assign_vehicles_to_solver_groups_sticky(
    vehicles: List[VehicleState],
    group_targets: Dict[Tuple[RegionId, RegionId], int],
    requests_by_group: Dict[Tuple[RegionId, RegionId], List],
    prev_assignments: Dict[int, Tuple[RegionId, RegionId]],
) -> Dict[Tuple[RegionId, RegionId], List[int]]:
    """Sticky assignment: keep vehicles in their previous solver group when possible.

    This minimizes cross-region deadhead by only reassigning vehicles whose previous
    group no longer has quota (demand dropped) or that were not previously assigned.

    Phase 1 — Retain: For each vehicle, if its previous group still has remaining quota
              AND that group has demand > 0, keep it there.
    Phase 2 — Reassign: Vehicles that couldn't be retained are assigned using the
              existing locality-based Tier1/2/3 logic from assign_vehicles_to_solver_groups.

    Literature: "Sticky assignment" — a soft constraint that keeps vehicles in their
    current zone, an industry-standard fleet management technique for minimizing deadhead.
    """
    remaining = dict(group_targets)
    assignments: Dict[Tuple[RegionId, RegionId], List[int]] = {
        group: [] for group in SOLVER_GROUP_LIST
    }
    unassigned_vehicles: List[VehicleState] = []

    # Phase 1: Retain vehicles in their previous group
    vehicles_sorted = sorted(vehicles, key=lambda v: (v.start_time_min, v.veh_id))
    for vehicle in vehicles_sorted:
        prev_group = prev_assignments.get(vehicle.veh_id)
        if (
            prev_group is not None
            and remaining.get(prev_group, 0) > 0
            and _group_total_demand(prev_group, requests_by_group) > 0
        ):
            assignments[prev_group].append(vehicle.veh_id)
            remaining[prev_group] -= 1
        else:
            unassigned_vehicles.append(vehicle)

    # Phase 2: Assign remaining vehicles using locality-based logic (same as default)
    for vehicle in unassigned_vehicles:
        cur_region = map_to_region(vehicle.start_lat, vehicle.start_lon)

        # Tier1: groups with demand originating in vehicle's current region
        tier1 = [
            group
            for group in SOLVER_GROUP_LIST
            if remaining.get(group, 0) > 0
            and _group_origin_demand(group, requests_by_group, cur_region) > 0
        ]
        if tier1:
            chosen = max(
                tier1,
                key=lambda group: (
                    _group_origin_demand(group, requests_by_group, cur_region),
                    _group_total_demand(group, requests_by_group),
                    -REGION_ORDER_INDEX[group[0]],
                    -REGION_ORDER_INDEX[group[1]],
                ),
            )
            assignments[chosen].append(vehicle.veh_id)
            remaining[chosen] -= 1
            continue

        # Tier2: groups whose endpoints include the vehicle's current region
        tier2 = [
            group
            for group in SOLVER_GROUP_LIST
            if remaining.get(group, 0) > 0 and _group_has_endpoint(group, cur_region)
        ]
        if tier2:
            chosen = max(
                tier2,
                key=lambda group: (
                    _group_total_demand(group, requests_by_group),
                    -_group_distance_to_vehicle(group, vehicle),
                    -REGION_ORDER_INDEX[group[0]],
                    -REGION_ORDER_INDEX[group[1]],
                ),
            )
            assignments[chosen].append(vehicle.veh_id)
            remaining[chosen] -= 1
            continue

        # Tier3: shortest travel time to any remaining group
        best_group = None
        best_score = float("inf")
        for group in SOLVER_GROUP_LIST:
            if remaining.get(group, 0) <= 0:
                continue
            score = _group_distance_to_vehicle(group, vehicle) - 0.001 * _group_total_demand(
                group, requests_by_group
            )
            if score < best_score:
                best_score = score
                best_group = group

        if best_group is None:
            best_group = max(
                SOLVER_GROUP_LIST,
                key=lambda group: _group_total_demand(group, requests_by_group),
            )

        assignments[best_group].append(vehicle.veh_id)
        if remaining.get(best_group, 0) > 0:
            remaining[best_group] -= 1

    return assignments


# ──────────────────────────────────────────────────────────────────────
#  Min-Cost Bipartite Matching (Hungarian / Kuhn-Munkres)
# ──────────────────────────────────────────────────────────────────────

def _hungarian_solve(cost_matrix: List[List[float]]) -> List[Tuple[int, int]]:
    """Pure-Python Hungarian algorithm for square cost matrices.

    Returns list of (row, col) pairs representing the optimal assignment.
    Time complexity: O(n^3), sufficient for n <= 200.
    """
    n = len(cost_matrix)
    if n == 0:
        return []

    INF = float("inf")
    # Pad to (n+1)x(n+1), 1-indexed
    u = [0.0] * (n + 1)
    v = [0.0] * (n + 1)
    p = [0] * (n + 1)
    way = [0] * (n + 1)

    cost = [[0.0] * (n + 1) for _ in range(n + 1)]
    for i in range(n):
        for j in range(n):
            cost[i + 1][j + 1] = cost_matrix[i][j]

    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv = [INF] * (n + 1)
        used = [False] * (n + 1)
        while True:
            used[j0] = True
            i0 = p[j0]
            delta = INF
            j1 = -1
            for j in range(1, n + 1):
                if not used[j]:
                    cur = cost[i0][j] - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j] = cur
                        way[j] = j0
                    if minv[j] < delta:
                        delta = minv[j]
                        j1 = j
            for j in range(n + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while j0:
            p[j0] = p[way[j0]]
            j0 = way[j0]

    result = []
    for j in range(1, n + 1):
        if p[j] != 0:
            result.append((p[j] - 1, j - 1))
    return result


def _compute_vehicle_group_cost(
    vehicle: "VehicleState",
    group: Tuple[RegionId, RegionId],
    requests_by_group: Dict[Tuple[RegionId, RegionId], List],
) -> float:
    """차량 → 그룹 배정 비용: 그룹 내 가장 가까운 실제 pickup까지의 이동 시간.

    그룹에 요청이 없으면 매우 큰 비용을 반환.
    """
    reqs = requests_by_group.get(group, [])
    if not reqs:
        return 9999.0

    best_tt = float("inf")
    for req in reqs:
        _, tt = travel(vehicle.start_lat, vehicle.start_lon,
                       float(req["pickup_lat"]), float(req["pickup_lon"]))
        if tt < best_tt:
            best_tt = tt
    return best_tt


def _deadline_anchor_requests(requests: List, target: int) -> List:
    """Choose deterministic, spatially diverse urgent pickup anchors."""
    if target <= 0 or not requests:
        return []
    ordered = sorted(
        requests,
        key=lambda req: (
            float(req.get("choice_deadline_min", req["latest_min"])),
            float(req["call_time_min"]),
            int(req["req_id"]),
        ),
    )
    selected = [ordered[0]]
    remaining = ordered[1:]
    while remaining and len(selected) < target:
        def diversity_key(req):
            nearest = float("inf")
            for anchor in selected:
                dist, _ = travel(
                    float(req["pickup_lat"]),
                    float(req["pickup_lon"]),
                    float(anchor["pickup_lat"]),
                    float(anchor["pickup_lon"]),
                )
                nearest = min(nearest, dist)
            return (
                nearest,
                -float(req.get("choice_deadline_min", req["latest_min"])),
                -int(req["req_id"]),
            )
        chosen = max(remaining, key=diversity_key)
        selected.append(chosen)
        remaining.remove(chosen)
    while len(selected) < target:
        selected.append(ordered[len(selected) % len(ordered)])
    return selected


def _compute_vehicle_anchor_cost(vehicle: "VehicleState", req) -> float:
    _, approach_min = travel(
        vehicle.start_lat,
        vehicle.start_lon,
        float(req["pickup_lat"]),
        float(req["pickup_lon"]),
    )
    _, direct_min = travel(
        float(req["pickup_lat"]),
        float(req["pickup_lon"]),
        float(req["drop_lat"]),
        float(req["drop_lon"]),
    )
    pickup_min = max(
        float(vehicle.start_time_min) + approach_min,
        float(req["call_time_min"]),
    )
    deadline = float(req.get("choice_deadline_min", req["latest_min"]))
    excess = max(0.0, pickup_min + direct_min - deadline)
    return approach_min + 10_000.0 * excess


def assign_vehicles_to_solver_groups_hungarian(
    vehicles: List["VehicleState"],
    group_targets: Dict[Tuple[RegionId, RegionId], int],
    requests_by_group: Dict[Tuple[RegionId, RegionId], List],
    deadline_aware: bool = False,
) -> Dict[Tuple[RegionId, RegionId], List[int]]:
    """Min-Cost Bipartite Matching으로 차량을 solver group에 배정.

    목적함수: 전체 fleet의 "현재 위치 → 배정된 그룹의 가장 가까운 pickup" 이동 시간 합 최소화.

    각 그룹은 target 수만큼 차량 슬롯을 가짐. Hungarian algorithm이
    모든 차량을 동시에 고려하여 전체 최적 배정을 결정.

    Literature: Pavone et al. (2012), Wallar et al. (2018)
    """
    assignments: Dict[Tuple[RegionId, RegionId], List[int]] = {
        group: [] for group in SOLVER_GROUP_LIST
    }

    n_vehicles = len(vehicles)
    if n_vehicles == 0:
        return assignments

    # 슬롯 확장: target=3이면 같은 그룹 슬롯 3개 생성
    slots: List[Tuple[Tuple[RegionId, RegionId], object]] = []
    for group in SOLVER_GROUP_LIST:
        target = group_targets.get(group, 0)
        anchors = (
            _deadline_anchor_requests(requests_by_group.get(group, []), target)
            if deadline_aware
            else [None] * target
        )
        for anchor in anchors:
            slots.append((group, anchor))

    n_slots = len(slots)

    # 차량 수와 슬롯 수가 다를 수 있으므로 정방 행렬로 패딩
    n = max(n_vehicles, n_slots)

    # 비용 행렬 구축
    cost_matrix: List[List[float]] = []
    # 차량별로 각 그룹에 대한 비용을 미리 계산 (중복 계산 방지)
    group_cost_cache: Dict[Tuple[int, Tuple[RegionId, RegionId]], float] = {}

    for i in range(n):
        row = []
        for j in range(n):
            if i < n_vehicles and j < n_slots:
                v = vehicles[i]
                g, anchor = slots[j]
                if deadline_aware and anchor is not None:
                    row.append(_compute_vehicle_anchor_cost(v, anchor))
                else:
                    cache_key = (v.veh_id, g)
                    if cache_key not in group_cost_cache:
                        group_cost_cache[cache_key] = _compute_vehicle_group_cost(
                            v, g, requests_by_group
                        )
                    row.append(group_cost_cache[cache_key])
            else:
                # 패딩: 가상 차량/슬롯은 비용 0 (매칭에 영향 없음)
                row.append(0.0)
        cost_matrix.append(row)

    # Hungarian algorithm 실행
    matching = _hungarian_solve(cost_matrix)

    # 결과 매핑
    for row_idx, col_idx in matching:
        if row_idx < n_vehicles and col_idx < n_slots:
            vid = vehicles[row_idx].veh_id
            group = slots[col_idx][0]
            assignments[group].append(vid)

    return assignments
