"""상수/열거/고정 파라미터 모음.

본 프로젝트는 '계산시간 우선'의 규칙 기반 DARP/DRT 시뮬레이션 프레임워크다.

- 지역(6개) + OUTSIDE
- OD(36개) 전체 목록(Option A: 차량은 한 시간대에 한 OD만 담당)
"""

from __future__ import annotations

from enum import Enum
from typing import Dict, List, Tuple


class RegionId(str, Enum):
    # Daejeon quadrants
    DJ_SW = "DJ_SW"
    DJ_SE = "DJ_SE"
    DJ_NW = "DJ_NW"
    DJ_NE = "DJ_NE"
    # Sejong north/south
    SJ_S = "SJ_S"
    SJ_N = "SJ_N"
    # Outside service rectangles
    OUTSIDE = "OUTSIDE"


SIX_REGIONS: List[RegionId] = [
    RegionId.DJ_SW,
    RegionId.DJ_SE,
    RegionId.DJ_NW,
    RegionId.DJ_NE,
    RegionId.SJ_S,
    RegionId.SJ_N,
]


def all_ods() -> List[Tuple[RegionId, RegionId]]:
    """6x6 = 36 OD 키 리스트를 고정 순서로 생성."""
    return [(o, d) for o in SIX_REGIONS for d in SIX_REGIONS]


OD_LIST: List[Tuple[RegionId, RegionId]] = all_ods()

# OD -> index (seed 등에서 사용)
OD_INDEX = {od: i for i, od in enumerate(OD_LIST)}

REGION_ORDER_INDEX: Dict[RegionId, int] = {region: idx for idx, region in enumerate(SIX_REGIONS)}


def canonical_solver_group(o_region: RegionId, d_region: RegionId) -> Tuple[RegionId, RegionId]:
    """Return the Stage 1 solver group key."""
    if o_region == d_region:
        return (o_region, d_region)
    if REGION_ORDER_INDEX[o_region] <= REGION_ORDER_INDEX[d_region]:
        return (o_region, d_region)
    return (d_region, o_region)


def all_solver_groups() -> List[Tuple[RegionId, RegionId]]:
    groups: List[Tuple[RegionId, RegionId]] = []
    for o_region in SIX_REGIONS:
        for d_region in SIX_REGIONS:
            group = canonical_solver_group(o_region, d_region)
            if group not in groups:
                groups.append(group)
    return groups


SOLVER_GROUP_LIST: List[Tuple[RegionId, RegionId]] = all_solver_groups()
SOLVER_GROUP_INDEX = {group: i for i, group in enumerate(SOLVER_GROUP_LIST)}


def solver_group_mode(group: Tuple[RegionId, RegionId]) -> str:
    return "intra" if group[0] == group[1] else "bidirectional_pair"


def solver_group_label(group: Tuple[RegionId, RegionId]) -> str:
    if group[0] == group[1]:
        return f"{group[0].value}->{group[1].value}"
    return f"{group[0].value}<->{group[1].value}"


# CSV headers
ALNS_TRACE_HEADER = [
    "hour",
    "o_region",
    "d_region",
    "iter",
    "T",
    "cost_current",
    "cost_candidate",
    "cost_best",
    "delta",
    "accepted",
    "improved_best",
    "destroy_op",
    "repair_op",
    "q_removed",
]

ALNS_DIAGNOSTICS_HEADER = [
    "hour",
    "o_region",
    "d_region",
    "group_mode",
    "group_label",
    "candidates",
    "iters",
    "time_budget_sec",
    "runtime_sec",
    "initial_temperature",
    "accept_rate",
    "uphill_accept_rate",
    "zero_delta_rate",
    "accept_rate_window_final",
    "accept_rate_window_peak",
    "high_accept_window_count",
    "last_improved_iter",
    "iterations_since_improvement",
    "stopped_early",
    "stop_reason",
]

VEHICLE_EVENTS_HEADER = [
    "veh_id",
    "event_type",  # WAIT / PICKUP / DROPOFF
    "req_id",
    "t_start_min",
    "t_end_min",
    "lat",
    "lon",
    # '이동은 합계로 저장' 요구 반영: 이전 이벤트 지점 -> 현재 이벤트 지점 이동 합계
    "move_dist_km_from_prev",
    "move_time_min_from_prev",
]

REQUESTS_HEADER = [
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
    # 결과
    "vehicle_id",
    "pickup_time_min",
    "dropoff_time_min",
    "drt_time_min",
    "onboard_dist_km",
    "lateness_min",
    "obj_base",
    "obj_penalty",
    "obj_total",
]

VEHICLES_HEADER = [
    "veh_id",
    "cap",
    "start_time_min",
    "start_lat",
    "start_lon",
    "finish_time_min",
    "finish_lat",
    "finish_lon",
    "vmt_total_km",
    "vmt_deadhead_km",
    "vmt_service_km",
    "time_wait_min",
    "time_deadhead_min",
    "time_service_min",
    "served_count",
]

HOUR_KPI_HEADER = [
    "hour",
    "candidates_in_hour",
    "outside_excluded",
    "served_in_hour",
    "obj_total",
    "obj_base",
    "obj_penalty",
    "vmt_total_km",
    "vmt_deadhead_km",
    "vmt_service_km",
    "runtime_sec",
]

OD_KPI_HEADER = [
    "hour",
    "o_region",
    "d_region",
    "group_mode",
    "group_label",
    "candidates",
    "served",
    "vehicles_target",
    "vehicles_assigned",
    "forward_candidates",
    "reverse_candidates",
    "iters",
    "time_budget_sec",
    "runtime_sec",
    "initial_cost",
    "final_cost",
    "best_cost",
    "accept_rate",
]
