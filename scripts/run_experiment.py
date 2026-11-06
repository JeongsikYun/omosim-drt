#!/usr/bin/env python3
"""Launch the frozen Runtime-v2 pipeline with explicit local geography/settings."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

RUNTIME_V2 = {
    "alns_quality_profile": True, "fleet_target_strategy": "workload_deadline",
    "portfolio_replicas": 8, "global_rescue": True, "global_rescue_replicas": 4,
    "global_rescue_time_fraction": 0.18, "sa_stagnation_patience_iters": 8000,
    "quality_stagnation_stop": True, "solver_travel_cache": True,
    "solver_travel_cache_max_entries": 524288, "winner_only_materialization": True,
    "adaptive_global_rescue": True, "adaptive_global_rescue_compact_polish": True,
    "adaptive_global_rescue_compact_pressure_threshold": 3.0,
}
EXPERIMENT = {
    "workers", "time_limit_sec", "fleet_size", "cap", "alns_iter_cap",
    "init_strategy", "init_multistart", "init_regret_k", "init_vehicle_top_k",
    "init_pos_samples", "init_candidate_pool", "init_time_limit_sec", "speed_kmph",
    "rejection_penalty", "car_time_ratio_cap", "drt_like_threshold", "drt_like_sub_ratio",
    "fallback_criteria", "service_hour_start", "service_hour_end", "rebalancing_strategy",
    "seed", "seed_base",
}


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024*1024), b""):
            h.update(block)
    return h.hexdigest()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", type=Path, required=True)
    p.add_argument("--service-area", type=Path, required=True)
    p.add_argument("--settings", type=Path, default=ROOT/"configs/runtime_v2.json")
    p.add_argument("--od-cache", type=Path, required=True)
    p.add_argument("--gh-url", help="Required when building a cache; must point to the local study-area graph")
    p.add_argument("--skip-od-build", action="store_true")
    p.add_argument("--output-dir", type=Path, default=ROOT/"outputs")
    p.add_argument("--allow-illustrative", action="store_true", help="Allow the clearly labelled synthetic geography for software testing")
    p.add_argument("--dry-run", action="store_true", help="Validate and print settings without writing artifacts or starting a simulation")
    a = p.parse_args()
    settings = json.loads(a.settings.read_text())
    if set(settings) != EXPERIMENT | set(RUNTIME_V2):
        raise ValueError("Settings must contain exactly the documented keys; start with configs/runtime_v2.json")
    if any(settings[k] != v for k, v in RUNTIME_V2.items()):
        raise ValueError("Runtime-v2 algorithm flags must match the reference configuration")
    if settings["drt_like_sub_ratio"] != 1.0:
        raise ValueError("This launcher validates all boundary agents; drt_like_sub_ratio must be 1.0")
    for k in ("workers", "fleet_size", "cap", "alns_iter_cap"):
        if isinstance(settings[k], bool) or not isinstance(settings[k], int) or settings[k] <= 0:
            raise ValueError(f"{k} must be a positive integer")
    if not (0 <= settings["service_hour_start"] <= settings["service_hour_end"] <= 23):
        raise ValueError("service hours must satisfy 0 <= start <= end <= 23")
    area_path = a.service_area.resolve()
    os.environ["DRT_SERVICE_AREA_CONFIG"] = str(area_path)
    from drt_pipeline.service_area import configured_area
    area = configured_area()
    if area.data.get("illustrative", False) and not a.allow_illustrative:
        raise ValueError("Illustrative coordinates require --allow-illustrative and are not a real study-area definition")
    from check_input import validate_input
    check = validate_input(a.input, settings["drt_like_threshold"], settings["service_hour_start"], settings["service_hour_end"])
    cache = a.od_cache.resolve()
    if cache.suffix == ".json":
        cache = cache.with_suffix(".db")
    if a.skip_od_build and not cache.is_file():
        raise ValueError("--skip-od-build requires an existing, complete local OD database")
    if not a.skip_od_build and not a.gh_url:
        raise ValueError("Provide --gh-url to build a cache, or --skip-od-build for an existing database")
    if a.dry_run:
        print(json.dumps({"preflight":check,"settings":settings,"service_area":area.data,"cache":str(cache)},indent=2))
        return 0
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:8]
    out = a.output_dir.resolve()/stamp
    out.mkdir(parents=True, exist_ok=False)
    frozen_area = out/"service_area.json"
    frozen_area.write_text(json.dumps(area.data,indent=2)+"\n")
    os.environ["DRT_SERVICE_AREA_CONFIG"] = str(frozen_area)
    cache.parent.mkdir(parents=True, exist_ok=True)
    command = [sys.executable,str(ROOT/"scripts/run_pipeline_iterations.py"),
               "--input_output_json",str(a.input.resolve()),"--iters","1",
               "--od_cache",str(cache),"--od_cache_strict",
               "--runs_dir",str(out/"runs"),"--pipeline_dir",str(out/f"pipeline_{stamp}")]
    for key, value in settings.items():
        command.append("--"+key)
        if value is not True:
            command.append(str(value))
    if a.gh_url:
        command.extend(["--gh_url",a.gh_url])
    if a.skip_od_build:
        command.append("--skip_od_build")
    source_hashes = {str(path.relative_to(ROOT)):digest(path)
                     for folder in ("drt_pipeline","scripts") for path in sorted((ROOT/folder).glob("*.py"))}
    identity = {"algorithm":"Runtime-v2","source_snapshot":"2026-11-06",
                "settings":settings,"input":str(a.input.resolve()),"input_sha256":digest(a.input),
                "service_area_sha256":digest(frozen_area),"source_sha256":source_hashes,
                "od_cache":str(cache),"od_cache_strict":True,"graphhopper_url":a.gh_url,
                "note":"Record OSM extract and GraphHopper build/config identity separately; a cache filename alone is not provenance.",
                "command":command,"preflight":check}
    (out/"effective_configuration.json").write_text(json.dumps(identity,indent=2)+"\n")
    print("Artifacts: "+str(out),flush=True)
    print("Command: "+shlex.join(command),flush=True)
    with (out/"pipeline.log").open("w") as log:
        result = subprocess.run(command,cwd=ROOT,env=dict(os.environ),stdout=log,stderr=subprocess.STDOUT)
    (out/"execution_status.json").write_text(json.dumps({"returncode":result.returncode,"completed":result.returncode==0},indent=2)+"\n")
    print(f"Pipeline exit code: {result.returncode}; log: {out/'pipeline.log'}")
    return result.returncode


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError, KeyError, TypeError) as exc:
        sys.exit(f"Experiment setup failed: {exc}")
