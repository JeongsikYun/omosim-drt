# OMOSIM → DRT: Runtime-v2 research simulator

Run a demand-responsive transport experiment using an existing OMOSIM activity-plan output. The pipeline extracts eligible passenger requests, plans shared vehicle routes, applies passenger mode choice, and reports passenger and vehicle outcomes.

This release was prepared for researchers who have generated OMOSIM outputs for Germany and want to investigate a **locally designed DRT service**. It includes a Korean reference geography and an explicit service-area configuration interface. German demand data, road-network caches and calibrated service assumptions are supplied by the researcher.

## Start here

1. [Install the Python environment](docs/installation.md).
2. Run the small synthetic check below. It requires no GraphHopper server.
3. [Define your German service area and routing database](docs/germany_setup.md).
4. [Check the OMOSIM input contract](docs/input_schema.md) and run the preflight check.
5. Run the experiment, then [generate the four KPI tables](docs/outputs.md).

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python scripts/smoke_test.py
```

Run these commands in the extracted/cloned repository root. `venv` creates a separate Python environment; activation selects it; `pip` installs the dependency. The smoke check uses two workers, three synthetic agents (one excluded), a temporary synthetic OD database and a short optimisation budget. A PASS demonstrates software integration, not real-world routing or model calibration.

## Main workflow

Prepare `configs/local/service_area.json`, `configs/local/experiment.json`, and `data/germany_input.json` using the guides. Start a GraphHopper instance containing the matching German road network, then run:

```bash
python scripts/check_input.py \
  --input data/germany_input.json \
  --service-area configs/local/service_area.json

python scripts/run_experiment.py \
  --input data/germany_input.json \
  --service-area configs/local/service_area.json \
  --settings configs/local/experiment.json \
  --gh-url http://localhost:8989 \
  --od-cache data/od/germany_car.db \
  --output-dir outputs/germany
```

The first command only validates input and reports candidate/coverage counts. Its defaults are threshold 0.6 and hours 6–22; pass `--threshold`, `--service-start` and `--service-end` to match a different experiment. The second command always validates using the experiment JSON, builds or fills the OD database, runs one daily Runtime-v2 experiment and writes configuration snapshots and logs. Both use your chosen local geography. `--dry-run` on the second command validates setup and prints settings without starting the pipeline.

## Why zones and an OD database?

Assigning many requests to vehicles while optimising pickup/drop-off sequences creates a large search problem. Runtime-v2 processes hourly request groups by origin–destination region pair, supports trips between zones, and includes a global rescue stage. Grouping makes local search and parallel execution manageable; zoning can also influence the result.

Road routing inside every candidate-route evaluation would be expensive. GraphHopper is used ahead of optimisation to calculate the required directed distances and travel times between request locations and vehicle positioning points. SQLite stores those values for repeated lookup. For M distinct rounded coordinates, the current builder considers M² ordered pairs, including diagonals. These are the experiment's locations, not every node in Germany's road network.

## What can be configured?

| Setting | Location |
|---|---|
| Service boundaries, six representative points, assignment metric | Service-area JSON |
| Fleet size, capacity, operating hours, eligibility threshold, time cap, seeds | Experiment JSON |
| GraphHopper endpoint and OD database | Launcher arguments |
| Runtime-v2 algorithm features | Fixed and recorded by the launcher |

This first portable release retains **exactly six internal region IDs** (`DJ_SW`, `DJ_SE`, `DJ_NW`, `DJ_NE`, `SJ_S`, `SJ_N`). They act as stable slots outside Korea; their local coordinates come from your JSON. General polygon boundaries and a variable number of zones require additional development. See [the region configuration contract](docs/germany_setup.md).

## Pre-DRT analysis

An independent analyzer compares Baseline/WFH/SO/WFH+SO JSON files. It requires no routing setup:

```bash
python scripts/analyze_pre_drt.py \
  --baseline examples/pre_drt/baseline.json \
  --scenario WFH=examples/pre_drt/wfh.json \
  --scenario SO=examples/pre_drt/so.json \
  --scenario WFH_SO=examples/pre_drt/wfh_so.json \
  --output-dir outputs/pre_drt_example
```

The files in `examples/` are synthetic. [Methodology](docs/methodology.md) explains agent exclusion, affected-population shares, vehicle mileage and passenger travel time.

## Scope and reproducibility

- Linux/Python 3.12 is the checked environment; WSL is the suggested Windows route. Native Windows/macOS execution has not been validated.
- Run **one simulated day per DRT invocation**. The solver schedules hours 0–23 and does not maintain separate vehicle schedules for multiple days.
- `service_hour_end` is inclusive: start 6/end 22 covers departures from 06:00 through 22:59 (17 hourly bins).
- The launcher always uses strict OD lookup. Missing routes cause an error instead of silently substituting approximate distances.
- Vehicle routes are planned before final passenger mode choice; non-DRT choices do not trigger a second vehicle-routing pass. Fleet utilisation is therefore labelled **Planned**.
- The packaged core is a working-tree snapshot dated 2026-11-06, including changes newer than the historical `runtime-v2-default` tag. [Provenance](provenance/source_snapshot.json) records source hashes and [validation](provenance/validation.json) records the local software checks. Same seeds with time-limited search do not guarantee identical results on different hardware.
- This package does not contain OMOSIM or GraphHopper JARs, OSM/GTFS data, Korean population files, production result folders, server connection details or an OD database.

## Repository map

```text
drt_pipeline/        Core solver, fleet logic, choice rules and geography adapter
scripts/             Explicit launcher, validation, reporting and tests
configs/             Reference settings and editable templates
examples/            Small synthetic inputs only
analysis/            Existing KPI calculations with portable orchestration
docs/                Installation, German setup, methodology and outputs
tests/               Release-specific geography/configuration checks
provenance/          Source snapshot and release-change record
```

## Sharing and contributions

Read [LICENSE_STATUS.md](LICENSE_STATUS.md) before wider redistribution; no distribution licence has been selected in this preparation package. [CONTRIBUTING.md](CONTRIBUTING.md) describes changes and checks. The maintainer's [publishing guide](docs/publishing.md) describes creating a new repository without importing the production workspace or its Git history.
