# Model and analysis definitions

## Pipeline

1. Preprocess OMOSIM agents and build candidate requests from activity locations.
2. Prepare/read directed GraphHopper OD distance/time entries.
3. Schedule the daily fleet in hourly origin–destination groups using quality ALNS, portfolio replicas and global rescue.
4. Apply the shared DRT time-choice rule and tour/day fallback logic.
5. Export final plans, request/vehicle records and KPI tables.

The standard wrapper records all Runtime-v2 flags from `configs/runtime_v2.json`. It does not silently use the historical Initial exact ALNS version or experimental citywide Q/S mode. Its time budget is the standalone pipeline's wall-clock budget, not the server-specific shared-batch reference-parallel accounting used by some later experiments.

## Passenger choice and consistency

DRT total time includes waiting and onboard time and must be no greater than the public-transit alternative. For an originally car-mode trip by a `CAR_USER`, it must also be no greater than `car_time_ratio_cap × original car-alternative time`. Equality is accepted.

If a qualifying DRT alternative triggers the car-free day/tour logic, other trips may move to PUBLIC_TRANSIT, FOOT or BICYCLE under the configured fallback rule. Consequently, the whole DRT-user travel-time effect is broader than the set of DRT-labelled trips.

The defaults are modelling assumptions, not an estimated German behavioural model. They can be studied or calibrated locally. Serving a request in route planning and the passenger finally choosing DRT are distinct events.

## Core KPI definitions

| Measure | Definition |
|---|---|
| Analysis agents | Exclude every agent with any selected CAR_PASSENGER trip. Include retained non-travellers. |
| SO/WFH/DRT agents | At least one SHARED_OFFICE activity / HOME_OFFICE activity / selected DRT trip, respectively. |
| Any impact | Union of SO, WFH and DRT flags; overlaps counted explicitly. |
| Passenger-car VMT | Sum selected CAR_DRIVER trip distance. |
| DRT VMT | DRT fleet vehicle mileage, including occupied/service and empty/deadhead movement. |
| Total VMT | Passenger-car VMT + DRT fleet VMT; not distance summed across all passenger modes. |
| Total TT | Sum selected passenger trip time across modes, in minutes. |
| Private-car users reduced | Baseline car-user count minus scenario car-user count; car user means any CAR_DRIVER trip. |
| Net vehicle reduction | Private-car users reduced minus configured DRT fleet count; a proxy, not registered-vehicle measurement. |
| Passenger DRT adoption | Final DRT chosen trips / DRT available in final choice scope. |
| Request conversion | Final DRT chosen trips / DRT requests built. |
| Planned fleet service utilisation | Vehicle passenger-service minutes / (fleet size × inclusive service-window minutes). |
| Productive VMT share | DRT service VMT / total DRT vehicle VMT. |
| Average occupancy | Passenger onboard minutes / vehicle service minutes. |

Routes are not recomputed after final passenger choice. Fleet utilisation and associated route mileage describe the planned operation; report this limitation when interpreting chosen-passenger occupancy and service efficiency.

VMT component comparisons match trip keys from the original OMOSIM input to the final Stage2 JSON, including when older mode-change CSVs have empty before-mode fields. The DRT-user TT table covers **all trips of any agent who finally used DRT**, including non-DRT fallback trips. Operation-table car-user changes compare against the pre-DRT policy input; the main table can compare against a separate country baseline.

## Comparing scenarios

Apply preprocessing independently to each scenario and show the analysis population. Equal SO/WFH input rates do not guarantee equal achieved participation; report measured activity-based shares. Compare each policy run with its matching country/seed baseline, calculate per-seed effects first, and then summarise independent seeds. Keep time period, population sampling and units explicit.

The pre-DRT portable analyzer is strict about invalid numeric inputs and writes blank undefined ratios. The extracted legacy DRT KPI routines retain their original zero/undefined handling; inspect the CSV definitions and do not interpret a zero-denominator rate as observed uptake. Non-DRT utilisation fields are N/A.
