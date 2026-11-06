#!/usr/bin/env python3
"""Boundary checks for the shared Stage2/ALNS DRT choice rule."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from drt_pipeline.choice_rules import (
    effective_drt_deadline_min,
    effective_drt_time_cutoff_min,
    evaluate_drt_time_choice,
    evaluate_trip_drt_choice,
)
from drt_pipeline.stage12 import _drt_rule_satisfied


def _trip(drt: object, pt: object, car: object = 20.0) -> dict:
    return {
        "alternativeModes": [
            {"mode": "DRT", "timeMinute": drt},
            {"mode": "PUBLIC_TRANSIT", "timeMinute": pt},
            {"mode": "CAR_DRIVER", "timeMinute": car},
        ]
    }


def main() -> None:
    # PT equality is eligible; the first representable value above it is not.
    result = evaluate_drt_time_choice(30.0, 30.0)
    assert result.eligible
    assert result.effective_time_cutoff_min == 30.0
    assert result.excess_min == 0.0

    result = evaluate_drt_time_choice(30.000001, 30.0)
    assert not result.eligible
    assert result.reason == "drt_exceeds_public_transit"
    assert abs((result.excess_min or 0.0) - 0.000001) < 1e-12

    # For an original car trip by a CAR_USER, the tighter car boundary wins.
    kwargs = {
        "original_mode": "CAR_DRIVER",
        "mobility_group": "CAR_USER",
        "original_car_time_min": 20.0,
        "car_time_ratio_cap": 1.5,
    }
    result = evaluate_drt_time_choice(30.0, 60.0, **kwargs)
    assert result.eligible
    assert result.car_time_limit_min == 30.0
    assert result.effective_time_cutoff_min == 30.0

    result = evaluate_drt_time_choice(30.000001, 60.0, **kwargs)
    assert not result.eligible
    assert result.reason == "drt_exceeds_car_time_cap"

    assert effective_drt_time_cutoff_min(
        60.0,
        original_mode="CAR_DRIVER",
        mobility_group="CAR_USER",
        original_car_time_min=20.0,
        car_time_ratio_cap=1.5,
    ) == 30.0
    assert effective_drt_deadline_min(480.0, 60.0, **kwargs) == 510.0

    # The car cap is not applied to a non-car original mode or non-CAR_USER.
    assert evaluate_drt_time_choice(
        45.0,
        60.0,
        original_mode="PUBLIC_TRANSIT",
        mobility_group="CAR_USER",
        original_car_time_min=None,
        car_time_ratio_cap=1.5,
    ).eligible
    assert evaluate_drt_time_choice(
        45.0,
        60.0,
        original_mode="CAR_DRIVER",
        mobility_group="NON_CAR_USER",
        original_car_time_min=None,
        car_time_ratio_cap=1.5,
    ).eligible

    # Missing original-car time and Stage0's -1 DRT sentinel are ineligible.
    assert not evaluate_drt_time_choice(
        25.0,
        60.0,
        original_mode="CAR_DRIVER",
        mobility_group="CAR_USER",
        original_car_time_min=None,
        car_time_ratio_cap=1.5,
    ).eligible
    assert not evaluate_drt_time_choice(-1.0, 60.0).eligible

    # The OMOSIM Trip adapter and Stage2 now use exactly the same helper.
    trip = _trip(30.0, 60.0, 20.0)
    trip_result = evaluate_trip_drt_choice(
        trip,
        original_mode="CAR_DRIVER",
        mobility_group="CAR_USER",
        car_time_ratio_cap=1.5,
    )
    assert trip_result.eligible
    assert _drt_rule_satisfied(
        trip,
        original_mode="CAR_DRIVER",
        mobility_group="CAR_USER",
        car_time_ratio_cap=1.5,
    )

    over_trip = _trip(30.000001, 60.0, 20.0)
    assert not evaluate_trip_drt_choice(
        over_trip,
        original_mode="CAR_DRIVER",
        mobility_group="CAR_USER",
        car_time_ratio_cap=1.5,
    ).eligible
    assert not _drt_rule_satisfied(
        over_trip,
        original_mode="CAR_DRIVER",
        mobility_group="CAR_USER",
        car_time_ratio_cap=1.5,
    )

    print("choice rule boundary tests: OK")


if __name__ == "__main__":
    main()
