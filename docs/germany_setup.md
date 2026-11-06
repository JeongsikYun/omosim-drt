# Setting up a German DRT experiment

You can use German OMOSIM outputs as demand and choose service assumptions for your own study. Matching Korea's fleet or operating conditions is only necessary when that is the research question.

## 1. Define the service area

```bash
mkdir -p configs/local data/od
cp configs/service_area.germany.template.json configs/local/service_area.json
cp configs/runtime_v2.json configs/local/experiment.json
```

These commands create local configuration copies. Edit every `null` in the service-area template; the incomplete template intentionally fails validation. Do not use `examples/synthetic_germany/` as a real study region.

Service-area format:

| Key | Meaning |
|---|---|
| `schema_version` | Keep `1`. |
| `name` | Your study area's descriptive name. |
| `coordinate_system` | Keep `EPSG:4326`. Reproject input coordinates beforehand if necessary. |
| `mean_latitude` | Representative latitude for local longitude scaling and diagnostic approximate distance. |
| `centers` | Exactly six distinct `[latitude, longitude]` points indexed by the six stable internal IDs. |
| `groups[].bounds` | `[lat_min, lon_min, lat_max, lon_max]`, inclusive bounds. |
| `groups[].regions` | IDs among which a point inside this rectangle is assigned. Each ID occurs in exactly one group. |
| `assignment_metric` | `local_km_l1` uses latitude/longitude scaling; `legacy_degree_l1` reproduces the Korean raw-degree metric. |
| `illustrative` | Set `false` for a real, reviewed study definition. |

Groups are evaluated in list order: the first containing rectangle wins. Within it, assign to the representative point with the smallest L1 coordinate distance; ties follow the order in `regions`. A point outside every rectangle is `OUTSIDE`. Requests with either endpoint outside are excluded from routing. This is rectangular coverage, not an administrative boundary polygon.

One rectangle containing all six IDs is sufficient for a compact study region. Several ordered rectangles are supported. Representative points must lie inside their own group and map back to their intended ID. Choose points reachable by the configured road profile: these are also initial vehicle positions and repositioning targets.

The internal labels still contain `DJ`/`SJ` for solver compatibility. You do not need to rename them. Changing the number of zones or using arbitrary GeoJSON polygons is beyond this adapter and needs coordinated changes to constants, fleet allocation and tests.

The launcher freezes the service-area JSON and passes its path through `DRT_SERVICE_AREA_CONFIG` to every subprocess. The small adapter changes coordinate assignment, representative points and the diagnostic approximation latitude; ALNS, fleet allocation, candidate extraction and passenger-choice logic remain the copied core implementation. The Korean configuration is tested against the original mapping. German real-data/road-network calibration is not claimed.

## 2. Select operating assumptions

Edit `configs/local/experiment.json`. Typical study variables are `fleet_size`, `cap`, service hours, `drt_like_threshold`, `car_time_ratio_cap`, `rebalancing_strategy`, seeds and compute budget. The launcher keeps the full Runtime-v2 feature bundle fixed and records all effective settings.

Defaults are Korean reference experiment settings, not recommendations calibrated for Germany. `drt_like_threshold = 0.6` is a preference-score threshold, not a guaranteed population share. `speed_kmph` controls the approximate-distance fallback; it does not override travel times stored from GraphHopper. The standard launcher uses strict caching and does not accept fallback routes.

## 3. Prepare German road routing and OD cache

Start [GraphHopper](installation.md) with an OSM extract covering request points, all six representative points and connecting roads. The builder uses `/route?profile=car` and stores directed distance in km and time in minutes. A→B and B→A are separate entries. Coordinates are rounded to four decimal places for lookup keys.

The first experiment can build its cache automatically using `--gh-url`. Subsequent experiments may fill additional pairs into that cache only if graph, profile, coordinates and assumptions are compatible. The cache includes all Stage0 request coordinates, including requests later excluded by area or service hours, plus representative points. The preflight reports the potential M² size.

Use a different database for different cities, OSM versions or routing profiles. The SQLite key contains coordinates, not the graph/profile identity; existing entries are otherwise reused. Keep a record of the OSM checksum, GraphHopper version/JAR checksum, config and cache build date with the experiment. Do not let two independent builders write to the same database simultaneously.

For an already complete, compatible database:

```bash
python scripts/run_experiment.py \
  --input data/germany_input.json \
  --service-area configs/local/service_area.json \
  --settings configs/local/experiment.json \
  --od-cache data/od/germany_car.db \
  --skip-od-build --output-dir outputs/germany
```

The solver checks routes strictly. A missing, invalid or unreachable cached pair is an error to resolve through coordinates, graph coverage or cache rebuilding. A `.json` cache argument is accepted for legacy compatibility and internally maps to `.db`; using `.db` explicitly is clearer.

## 4. Validate, then scale

Run the preflight, inspect exclusion/candidate/inside-area counts, and check representative route queries. Begin with a small real demand subset and an explicitly labelled test compute budget. Review Stage2 errors, selected trips, fallback transitions, vehicle mileage and waiting times before a full experiment. Worker count and first-time cache cost depend on your hardware and distinct locations; the Korean 120-worker setting is not a requirement.

Country comparison and local service exploration are separate experiment designs. For a local German study, choose locally meaningful inputs; for a cross-country comparison, document which assumptions are held constant and which are local.
