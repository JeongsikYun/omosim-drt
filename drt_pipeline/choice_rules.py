"""Shared DRT travel-time choice rules.

The optimizer and Stage2 must agree on what an ``eligible`` DRT itinerary is.
Stage2's rule is intentionally simple and uses inclusive boundaries:

* DRT travel time must be no greater than public-transit travel time.
* For an original car trip made by a ``CAR_USER``, DRT travel time must also
  be no greater than ``car_time_ratio_cap * original car travel time``.

This module keeps the scalar calculation independent from the OMOSIM JSON
shape, while :func:`evaluate_trip_drt_choice` provides the small adapter used
by Stage1/Stage2.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence


CAR_MODES = frozenset({"CAR_DRIVER", "CAR_PASSENGER"})
DEFAULT_CAR_TIME_RATIO_CAP = 2.0
DRT_UNAVAILABLE_TIME_MIN = -1.0


def _safe_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_mode(value: Any) -> Optional[str]:
    """Return Stage2's normalized mode representation."""

    if value is None:
        return None
    mode = str(value).strip()
    return mode if mode else None


def get_alternative(
    trip: Mapping[str, Any],
    mode: str,
) -> Optional[Mapping[str, Any]]:
    """Find the first alternative-mode entry matching ``mode``."""

    alternatives = trip.get("alternativeModes", [])
    if not isinstance(alternatives, Sequence) or isinstance(alternatives, (str, bytes)):
        return None
    for alternative in alternatives:
        if isinstance(alternative, Mapping) and alternative.get("mode") == mode:
            return alternative
    return None


def car_time_cap_applies(
    *,
    original_mode: Any,
    mobility_group: Any,
) -> bool:
    """Return whether Stage2 applies the additional original-car time cap."""

    return mobility_group == "CAR_USER" and normalize_mode(original_mode) in CAR_MODES


def effective_drt_time_cutoff_min(
    pt_time_min: Any,
    *,
    original_mode: Any = None,
    mobility_group: Any = None,
    original_car_time_min: Any = None,
    car_time_ratio_cap: float = DEFAULT_CAR_TIME_RATIO_CAP,
) -> Optional[float]:
    """Return the inclusive DRT travel-time cutoff, or ``None`` if undefined.

    ``original_car_time_min`` is required only when the car cap applies.  The
    function deliberately uses direct floating-point comparisons/addition so
    the equality boundary is identical to Stage2 (there is no hidden epsilon).
    """

    pt_time = _safe_float(pt_time_min)
    if pt_time is None:
        return None

    cutoff = pt_time
    if car_time_cap_applies(original_mode=original_mode, mobility_group=mobility_group):
        car_time = _safe_float(original_car_time_min)
        if car_time is None:
            return None
        cutoff = min(cutoff, float(car_time_ratio_cap) * car_time)
    return cutoff


def effective_drt_deadline_min(
    call_time_min: Any,
    pt_time_min: Any,
    *,
    original_mode: Any = None,
    mobility_group: Any = None,
    original_car_time_min: Any = None,
    car_time_ratio_cap: float = DEFAULT_CAR_TIME_RATIO_CAP,
) -> Optional[float]:
    """Return ``call time + effective DRT time cutoff`` for route scoring."""

    call_time = _safe_float(call_time_min)
    if call_time is None:
        return None
    cutoff = effective_drt_time_cutoff_min(
        pt_time_min,
        original_mode=original_mode,
        mobility_group=mobility_group,
        original_car_time_min=original_car_time_min,
        car_time_ratio_cap=car_time_ratio_cap,
    )
    if cutoff is None:
        return None
    return call_time + cutoff


@dataclass(frozen=True)
class DRTChoiceEvaluation:
    """Detailed result of applying the Stage2 DRT travel-time rule."""

    eligible: bool
    reason: str
    drt_time_min: Optional[float]
    pt_time_min: Optional[float]
    car_time_limit_min: Optional[float]
    effective_time_cutoff_min: Optional[float]
    excess_min: Optional[float]


def evaluate_drt_time_choice(
    drt_time_min: Any,
    pt_time_min: Any,
    *,
    original_mode: Any = None,
    mobility_group: Any = None,
    original_car_time_min: Any = None,
    car_time_ratio_cap: float = DEFAULT_CAR_TIME_RATIO_CAP,
) -> DRTChoiceEvaluation:
    """Evaluate a DRT duration against the exact Stage2 choice boundaries."""

    drt_time = _safe_float(drt_time_min)
    pt_time = _safe_float(pt_time_min)
    applies_car_cap = car_time_cap_applies(
        original_mode=original_mode,
        mobility_group=mobility_group,
    )

    if drt_time is None or pt_time is None:
        return DRTChoiceEvaluation(
            eligible=False,
            reason="missing_or_invalid_drt_pt_time",
            drt_time_min=drt_time,
            pt_time_min=pt_time,
            car_time_limit_min=None,
            effective_time_cutoff_min=None,
            excess_min=None,
        )
    if drt_time == DRT_UNAVAILABLE_TIME_MIN:
        return DRTChoiceEvaluation(
            eligible=False,
            reason="drt_unavailable",
            drt_time_min=drt_time,
            pt_time_min=pt_time,
            car_time_limit_min=None,
            effective_time_cutoff_min=None,
            excess_min=None,
        )

    car_time_limit: Optional[float] = None
    if applies_car_cap:
        car_time = _safe_float(original_car_time_min)
        if car_time is None:
            return DRTChoiceEvaluation(
                eligible=False,
                reason="missing_or_invalid_original_car_time",
                drt_time_min=drt_time,
                pt_time_min=pt_time,
                car_time_limit_min=None,
                effective_time_cutoff_min=None,
                excess_min=None,
            )
        car_time_limit = float(car_time_ratio_cap) * car_time

    cutoff = pt_time if car_time_limit is None else min(pt_time, car_time_limit)
    excess = max(0.0, drt_time - cutoff)
    if drt_time > pt_time:
        reason = "drt_exceeds_public_transit"
    elif car_time_limit is not None and drt_time > car_time_limit:
        reason = "drt_exceeds_car_time_cap"
    else:
        reason = "eligible"

    return DRTChoiceEvaluation(
        eligible=reason == "eligible",
        reason=reason,
        drt_time_min=drt_time,
        pt_time_min=pt_time,
        car_time_limit_min=car_time_limit,
        effective_time_cutoff_min=cutoff,
        excess_min=excess,
    )


def evaluate_trip_drt_choice(
    trip: Mapping[str, Any],
    *,
    original_mode: Any = None,
    mobility_group: Any = None,
    car_time_ratio_cap: float = DEFAULT_CAR_TIME_RATIO_CAP,
) -> DRTChoiceEvaluation:
    """Evaluate the DRT alternative in an OMOSIM Trip mapping."""

    alt_drt = get_alternative(trip, "DRT")
    alt_pt = get_alternative(trip, "PUBLIC_TRANSIT")
    if alt_drt is None or alt_pt is None:
        return DRTChoiceEvaluation(
            eligible=False,
            reason="missing_drt_or_public_transit_alternative",
            drt_time_min=None,
            pt_time_min=None,
            car_time_limit_min=None,
            effective_time_cutoff_min=None,
            excess_min=None,
        )

    original_car_time: Any = None
    if car_time_cap_applies(original_mode=original_mode, mobility_group=mobility_group):
        alt_car = get_alternative(trip, normalize_mode(original_mode) or "")
        if alt_car is not None:
            original_car_time = alt_car.get("timeMinute")

    return evaluate_drt_time_choice(
        alt_drt.get("timeMinute"),
        alt_pt.get("timeMinute"),
        original_mode=original_mode,
        mobility_group=mobility_group,
        original_car_time_min=original_car_time,
        car_time_ratio_cap=car_time_ratio_cap,
    )
