#!/usr/bin/env python3
"""Tiny end-to-end software check, using a synthetic OD DB and no HTTP server."""
from __future__ import annotations
import csv
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def main():
    os.environ["DRT_SERVICE_AREA_CONFIG"] = str(ROOT/"examples/synthetic_germany/service_area.json")
    from drt_pipeline.constants import SIX_REGIONS
    from drt_pipeline.regions import region_center
    from drt_pipeline.travel import make_route_key, manhattan_km
    with tempfile.TemporaryDirectory(prefix="omosim_drt_smoke_") as name:
        temp=Path(name);db=temp/"synthetic.db"
        points=[region_center(region) for region in SIX_REGIONS]
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE od_cache (route_key TEXT PRIMARY KEY, dist_km REAL, time_min REAL)")
            rows=[]
            for a in points:
                for b in points:
                    distance=manhattan_km(*a,*b)
                    rows.append((make_route_key(*a,*b),distance,distance*2))
            conn.executemany("INSERT INTO od_cache VALUES (?,?,?)",rows)
        command=[sys.executable,str(ROOT/"scripts/run_experiment.py"),
                 "--input",str(ROOT/"examples/synthetic_germany/input.json"),
                 "--service-area",str(ROOT/"examples/synthetic_germany/service_area.json"),
                 "--settings",str(ROOT/"examples/synthetic_germany/settings.json"),
                 "--od-cache",str(db),"--skip-od-build","--allow-illustrative","--output-dir",str(temp/"outputs")]
        completed=subprocess.run(command,cwd=ROOT,env=dict(os.environ),timeout=150)
        if completed.returncode:
            for log in temp.glob("outputs/*/pipeline.log"):
                print(log.read_text()[-8000:])
            raise RuntimeError("Synthetic pipeline failed")
        pipeline=next(temp.glob("outputs/*/pipeline_*/iteration_kpi_all.csv"))
        with pipeline.open() as handle:row=list(csv.DictReader(handle))[-1]
        assert int(row["stage0_agents_agent_cnt"])==2
        assert int(row["stage0_requests_trip_cnt"])==2
        assert int(row["stage2_error_count"])==0
        assert int(row["drt_chosen_count"])==2,row
        subprocess.run([sys.executable,str(ROOT/"scripts/report_run.py"),
                        "--before",str(ROOT/"examples/synthetic_germany/input.json"),
                        "--pipeline-dir",str(pipeline.parent),"--output-dir",str(temp/"report")],cwd=ROOT,check=True)
        assert len(list((temp/"report").glob("*.csv")))==4
        print("PASS: German-coordinate synthetic geography, strict cache, full pipeline, 2 DRT choices, four report tables")
        print("Synthetic OD values are test fixtures, not GraphHopper results or German research evidence.")


if __name__=="__main__":
    main()
