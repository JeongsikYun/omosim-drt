# OMOSIM input contract

Use original OMOSIM result JSON, with `agents[]` and activity/trip plans. The preflight validates the fields used by the DRT pipeline. The example at `examples/synthetic_germany/input.json` is small and explicit.

| Field | Requirement |
|---|---|
| `agents[].id` | Unique integer or integer string. |
| `agents[].drtLike` | Finite score in [0,1]. Missing values must not be replaced by invented German preferences. |
| `agents[].mobilityGroup` | Nonempty group label; exact `CAR_USER` triggers the original-car choice rule. |
| `mobilityDemand[].day` | Integer day label; one diary per agent/day, one simulated day across each DRT input. |
| `plan[].type` | `Activity` or `Trip`. Every Trip is immediately between two Activities. |
| Activity `activityType` | Activity label, including `SHARED_OFFICE` / `HOME_OFFICE` for impact counting. |
| Activity `lat`, `lon` | Latitude/longitude in degrees (EPSG:4326). |
| Trip `legID` | Integer, unique in the agent's day. |
| Trip `startTime` | HH:MM in 00:00–23:59. Split multi-day and midnight-overflow outputs deliberately before use. |
| Trip `mode`, `timeMinute`, `distanceKilometer` | Selected mode and finite non-negative time (min) / distance (km). |
| Trip `alternativeModes[]` | One object per mode with `mode`, `timeMinute`, `distanceKilometer`; retain selected-mode alternative too. |

DRT candidates need alternatives named exactly `CAR_DRIVER`, `FOOT` and `PUBLIC_TRANSIT`. The existing Stage0 filter omits current BICYCLE/FOOT/WALKING/BIKING/CAR_PASSENGER trips, requires car-alternative distance <15 km, walking-alternative distance >2 km, and agent `drtLike` at or above the configured threshold. The distance limits are currently code constants in `drt_pipeline/stage0.py`, not launcher settings. Changing them defines a modified experimental model and needs a recorded code change.

All agents with any selected CAR_PASSENGER trip are removed before candidate extraction. An alternative CAR_PASSENGER option alone does not trigger exclusion. Missing alternative modes are counted in Stage0 diagnostics; zero eligible in-area requests fails preflight.

The DRT alternative is generated/reset by the pipeline. Do not feed a prior DRT result into the standard one-pass launcher as if it were original OMOSIM demand. Unavailable alternatives may have time −1; available alternatives must have valid distance/time. Candidate public-transit time must be valid because it defines a routing deadline and choice reference.

The road route database supplies DRT vehicle distances and times. Public-transit alternatives come from the German OMOSIM output; the DRT solver does not build a German transit network or re-estimate OMOSIM mode-choice parameters.

## Common validation messages

- **No eligible requests:** inspect `drtLike`, alternative-mode availability, distance limits, area assignment and service hours.
- **All locations outside:** wrong country configuration, reversed latitude/longitude or projected coordinates.
- **Multiple simulated days:** split into independent daily inputs; do not pool several days' requests into one fleet schedule.
- **Unknown mode or invalid time:** inspect the OMOSIM export/version and the meaning of unavailable alternatives.
- **Selected mode missing from alternatives:** preserve the selected-mode record so Stage2 can retain/fall back to it.

For pre-DRT aggregate analysis only, fewer fields are needed: the standalone analyzer does not require coordinates, preferences or alternatives. Its population/time definitions still apply to the entire recorded period.
