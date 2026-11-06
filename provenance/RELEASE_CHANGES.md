# Snapshot and release-only changes

The source snapshot was read on 2026-11-06. Its Git HEAD and per-file hashes are recorded in `source_snapshot.json`. The source working tree contained local changes, so the HEAD alone does not identify the executed code.

Unchanged imported core includes `alns_sa.py`, `choice_rules.py`, `constants.py`, `fleet.py`, `io_utils.py`, `simulate.py`, `stage0.py`, `stage12.py`, and the underlying pipeline/stage/cache scripts. The optional generic batch-pool helper is retained for import compatibility; server-specific experiment controllers, queues, data and reports are omitted.

The imported `regions.py` has an optional branch delegating region assignment/centers to the new JSON adapter. With no configured area, its original Korean mapping remains in place. Imported `travel.py` obtains the approximation's reference latitude from that configuration. Both changes exist only in this independently prepared copy.

New release code: `service_area.py`, input preflight, explicit Runtime-v2 launcher, a synthetic full-pipeline check, portable report orchestration and release-specific tests. `analysis/kpi_core.py` extracts existing calculation functions without rewriting their bodies; server-specific batch discovery and absolute input paths are excluded. The standalone pre-DRT analyzer was previously validated against the Korean analyzer on a 10,000-agent output.

The historical low-level `run_stage0.py` is omitted because it invokes the old candidate filter and does not apply the main pipeline's complete preprocessing. Use `run_experiment.py` for this release.

Validation details are recorded in `validation.json`. Full German road-routing and empirical demand validation remain the responsibility of the local experiment; the bundled German-coordinate fixture uses artificial OD values.
