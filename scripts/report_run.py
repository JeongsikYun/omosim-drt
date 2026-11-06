#!/usr/bin/env python3
"""Produce the four core KPI tables for one completed pipeline invocation."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analysis import kpi_core as core


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--before", type=Path, required=True, help="Original pre-DRT OMOSIM JSON")
    p.add_argument("--baseline", type=Path, help="Country's matching baseline; defaults to --before")
    p.add_argument("--pipeline-dir", type=Path, required=True)
    p.add_argument("--scenario", default="Local scenario + DRT")
    p.add_argument("--output-dir", type=Path, required=True)
    a = p.parse_args()
    mapping = core._read_csv(a.pipeline_dir/"iteration_map.csv")
    if len(mapping) != 1:
        raise ValueError("Expected one completed iteration from run_experiment.py")
    run_dir = Path(mapping[0]["run_dir"])
    if not run_dir.is_absolute():
        run_dir = Path(__file__).resolve().parents[1]/run_dir
    config = json.loads((run_dir/"metrics/run_config.json").read_text())
    kpi = core._read_csv(a.pipeline_dir/"iteration_kpi_all.csv")[-1]
    kpi.update(core._read_csv(run_dir/"stage12/iteration_kpi.csv")[-1])
    if int(float(kpi["stage2_error_count"])) != 0:
        raise ValueError("Stage2 errors are present; inspect the run before reporting")
    run = core.RunInfo(a.scenario, run_dir, run_dir/"stage12/output_afterStage2_iter001.json",kpi,
                       core._read_csv(run_dir/"kpi/requests.csv"),core._read_csv(run_dir/"kpi/vehicles.csv"),
                       core._read_csv(run_dir/"metrics/hour_kpi.csv"),config)
    before = core.summarize_json(a.before)
    baseline = core.summarize_json(a.baseline or a.before)
    after = core.summarize_json(run.stage2_json)
    post = core.main_kpi_row(a.scenario,after,baseline,run)
    post["Total Agents"] = before.total_agents
    post["Excluded CAR_PASSENGER Agents"] = before.excluded_car_passenger_agents
    tables = {
        "main_kpi": [core.main_kpi_row("Pre-DRT input",before,baseline),post],
        "vmt_components":core.vmt_component_rows(a.scenario,before,after,run),
        "drt_tt_components":core.drt_tt_component_rows(a.scenario,before,after,run),
        "drt_operation_kpi":[core.operation_row(a.scenario,before,after,run)],
    }
    a.output_dir.mkdir(parents=True,exist_ok=True)
    for name,rows in tables.items():
        if rows:
            core._write_csv(a.output_dir/(name+".csv"),rows)
        else:
            (a.output_dir/(name+".csv")).write_text("Scenario,Status\n"+a.scenario+",No applicable observations\n")
    (a.output_dir/"report_metadata.json").write_text(json.dumps({
        "before":str(a.before.resolve()),"baseline":str((a.baseline or a.before).resolve()),
        "run_dir":str(run_dir),"analysis_rule":"Exclude agents with any CAR_PASSENGER trip",
        "vmt":"Passenger car + DRT vehicle mileage; not all passenger-mode distance",
        "tt_cohort":"All trips of agents who used DRT at least once",
        "utilisation":"Planned; no rerouting after final mode choice",
    },indent=2)+"\n")
    print(f"Wrote four KPI tables to {a.output_dir}")


if __name__ == "__main__":
    main()
