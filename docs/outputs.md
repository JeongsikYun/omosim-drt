# Outputs, monitoring and recovery

Each launcher invocation creates a unique directory below `--output-dir` and prints its path. It contains:

| Artifact | Meaning |
|---|---|
| `effective_configuration.json` | Input hash, source hashes, fixed Runtime-v2 flags, operating assumptions, cache path and preflight counts. |
| `service_area.json` | Frozen copy actually passed to all worker/stage subprocesses. |
| `pipeline.log` | Full pipeline console output. |
| `execution_status.json` | Final process return code. |
| `pipeline_*/iteration_map.csv` | Exact mapping between Stage0, run directory and final Stage2 result. |
| `pipeline_*/iteration_kpi_all.csv` | Combined pipeline metrics for the one iteration. |
| `runs_*/run_*/metrics/run_config.json` | Solver's effective run configuration and provenance. |
| `runs_*/run_*/kpi/requests.csv`, `vehicles.csv` | Planned request and fleet operation records. |
| `runs_*/run_*/stage12/output_afterStage2_iter001.json` | Final activity plans after mode choice. |

Stage0 intermediate files are created under repository-local `cache/`, using a unique pipeline identifier. These are generated artifacts and ignored by Git.

To watch progress, open another terminal and use `tail -f` on the printed `pipeline.log`. This streams new lines as they arrive; Ctrl+C stops watching, not the simulation. The launcher runs in the foreground. For long remote runs you may use your institution's normal persistent-session/job system; do not terminate a shared routing server to stop a single experiment.

## Generate the four KPI tables

After successful completion, use the exact `pipeline_...` directory containing `iteration_map.csv`:

```bash
python scripts/report_run.py \
  --before data/germany_input.json \
  --baseline data/germany_baseline.json \
  --pipeline-dir outputs/germany/RUN_ID/PIPELINE_DIRECTORY \
  --scenario "Germany local service + DRT" \
  --output-dir outputs/germany/RUN_ID/report
```

Replace the uppercase placeholders with the directories created by your run. `--before` must be the exact original OMOSIM input to that DRT run. Omit `--baseline` to compare the main rows with that same pre-DRT input. The script writes:

1. `main_kpi.csv` — pre/post main scenario indicators.
2. `vmt_components.csv` — passenger-car changes and added fleet vehicle distance.
3. `drt_tt_components.csv` — all-trip time changes for agents who chose DRT.
4. `drt_operation_kpi.csv` — demand, selection, fleet operation and utilisation.

`report_metadata.json` records the input/run references and metric scope. A table without applicable observations contains a status row. This reporter supports the standard one-iteration release wrapper; it does not discover server-specific historical batches.

## When a run fails

Inspect `pipeline.log`, `pipeline_*/iteration_failures.csv` and any `metrics/run_failure.json` in the unfinished run folder. Distinguish input/region failures, unavailable road routes and exhausted compute budget. Correct the cause and start a new invocation; retain the failed directory for diagnosis. Do not present an incomplete or error-containing Stage2 output as a valid experiment.

If a cache is being written, let the writer finish before copying it. Use a SQLite backup operation for a consistent transfer; copying only a live `.db` while its WAL changes is not a reproducible snapshot. Each researcher should generate/manage their own road-network cache.
