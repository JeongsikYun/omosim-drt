"""대전/세종 사각형 서비스 권역 + 6개 Region 분할.

요구사항 반영:
- 대전/세종 bbox 밖은 OUTSIDE
- bbox 중첩 시 대전 우선
- 세종: 위도 centroid 기준 북/남 2분할
- 대전: centroid(lat,lon) 기준 4분면
- 초기 차량 배치는 각 sub-rectangle(6개)의 중앙점
"""

from __future__ import annotations

from typing import Tuple

from .constants import RegionId
from .service_area import configured_area

_CONFIGURED_AREA = configured_area()


# Daejeon bbox
DJ_LAT_MIN = 36.1831
DJ_LAT_MAX = 36.5000
DJ_LON_MIN = 127.2466
DJ_LON_MAX = 127.5597

# Sejong bbox
SJ_LAT_MIN = 36.3680
SJ_LAT_MAX = 36.7370
SJ_LON_MIN = 127.0650
SJ_LON_MAX = 127.4081

# Split points (사용자 제공) -> 이제 Centroid (대표점) 기반 보로노이 분할로 변경됨
DJ_CENTERS = {
    RegionId.DJ_SW: (36.2750, 127.3200),
    RegionId.DJ_SE: (36.2750, 127.4600),
    RegionId.DJ_NW: (36.4250, 127.3200),
    RegionId.DJ_NE: (36.4250, 127.4600),
}

SJ_CENTERS = {
    RegionId.SJ_S: (36.4735, 127.2365),
    RegionId.SJ_N: (36.6580, 127.2365),
}


def in_rect(lat: float, lon: float, lat_min: float, lat_max: float, lon_min: float, lon_max: float) -> bool:
    return (lat_min <= lat <= lat_max) and (lon_min <= lon <= lon_max)


def map_to_region(lat: float, lon: float) -> RegionId:
    """위경도를 6개 지역 + OUTSIDE로 매핑.

    - 중첩 시 대전 우선: in_dj를 먼저 검사.
    - 직선 커팅이 아닌, 각 클러스터 중심점과의 최단거리(보로노이) 맵핑 수행
    """
    if _CONFIGURED_AREA is not None:
        return RegionId(_CONFIGURED_AREA.map(lat, lon))
    in_dj = in_rect(lat, lon, DJ_LAT_MIN, DJ_LAT_MAX, DJ_LON_MIN, DJ_LON_MAX)
    in_sj = in_rect(lat, lon, SJ_LAT_MIN, SJ_LAT_MAX, SJ_LON_MIN, SJ_LON_MAX)

    if in_dj:
        best_r = RegionId.OUTSIDE
        best_d = float('inf')
        for r_id, (c_lat, c_lon) in DJ_CENTERS.items():
            dist = abs(lat - c_lat) + abs(lon - c_lon)
            if dist < best_d:
                best_d = dist
                best_r = r_id
        return best_r

    if in_sj:
        best_r = RegionId.OUTSIDE
        best_d = float('inf')
        for r_id, (c_lat, c_lon) in SJ_CENTERS.items():
            dist = abs(lat - c_lat) + abs(lon - c_lon)
            if dist < best_d:
                best_d = dist
                best_r = r_id
        return best_r

    return RegionId.OUTSIDE


def region_center(region: RegionId) -> Tuple[float, float]:
    """각 구역의 맵핑 중심점. 초기 차량 배치 등에 동일하게 사용됨."""
    if _CONFIGURED_AREA is not None:
        return _CONFIGURED_AREA.centers[region.value]
    if region in DJ_CENTERS:
        return DJ_CENTERS[region]
    if region in SJ_CENTERS:
        return SJ_CENTERS[region]
    raise ValueError(f"Region center undefined for: {region}")
