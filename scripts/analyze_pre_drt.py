#!/usr/bin/env python3
"""Analyse pre-DRT OMOSIM outputs using Python 3.10+ (standard library only).

Example:
  python analyze_pre_drt.py --baseline baseline.json --scenario SO=so.json

Each invocation compares one baseline with scenarios from the same experiment
and seed. All recorded days/hours are included. No simulation is launched.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import csv
import hashlib
import json
import math
from pathlib import Path
import sys

VERSION = "1.0.0"


def array(value, location):
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{location}: expected an array")
    return value


def plan_items(agent):
    for demand in array(agent.get("mobilityDemand"), "mobilityDemand"):
        if not isinstance(demand, dict):
            raise ValueError("mobilityDemand entries must be objects")
        for item in array(demand.get("plan"), "plan"):
            if not isinstance(item, dict):
                raise ValueError("plan entries must be objects")
            if item.get("type") not in {"Activity", "Trip"}:
                raise ValueError(f"Unrecognised plan entry type: {item.get('type')!r}")
            yield item


def mode(item):
    return str(item.get("mode") or "").strip().upper()


def number(item, key, location):
    value = item.get(key)
    if isinstance(value, bool) or value is None or value == "":
        raise ValueError(f"{location}: missing/invalid {key}")
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{location}: invalid {key}={value!r}") from None
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{location}: {key} must be finite and non-negative")
    return result


def ratio(numerator, denominator, factor=1.0):
    return factor * numerator / denominator if denominator else None


def summarize(path):
    with path.open(encoding="utf-8-sig") as handle:
        data = json.load(handle)
    if not isinstance(data, dict) or not isinstance(data.get("agents"), list):
        raise ValueError(f"{path.name}: expected an object with an 'agents' array")
    counts = {name: 0 for name in (
        "excluded_car_passenger_agents", "analysis_agents", "trip_count",
        "so_agents", "wfh_agents", "so_wfh_both_agents", "private_car_users",
    )}
    pc_vmt = total_tt = 0.0
    by_mode = defaultdict(lambda: {"trip_count": 0, "passenger_distance_km": 0.0,
                                   "passenger_time_min": 0.0})
    ids = set()
    for agent in data["agents"]:
        if not isinstance(agent, dict):
            raise ValueError("agents entries must be objects")
        aid = agent.get("id")
        if isinstance(aid, bool) or not isinstance(aid, (int, str)) or str(aid) == "":
            raise ValueError("Each agent must have a non-empty integer/string id")
        aid = str(aid)
        if aid in ids:
            raise ValueError(f"Duplicate agent id: {aid}")
        ids.add(aid)
        items = list(plan_items(agent))
        if any(x["type"] == "Trip" and mode(x) == "CAR_PASSENGER" for x in items):
            counts["excluded_car_passenger_agents"] += 1
            continue
        counts["analysis_agents"] += 1
        so = wfh = car = False
        for index, item in enumerate(items):
            location = f"{path.name}, agent {aid}, entry {index}"
            if item["type"] == "Activity":
                if not isinstance(item.get("activityType"), str) or not item["activityType"]:
                    raise ValueError(f"{location}: missing activityType")
                so |= item["activityType"] == "SHARED_OFFICE"
                wfh |= item["activityType"] == "HOME_OFFICE"
                continue
            selected_mode = mode(item)
            if not selected_mode or selected_mode == "UNKNOWN":
                raise ValueError(f"{location}: missing/unknown selected mode")
            if selected_mode == "DRT":
                raise ValueError(f"{location}: selected DRT trip in a pre-DRT input")
            distance = number(item, "distanceKilometer", location)
            duration = number(item, "timeMinute", location)
            counts["trip_count"] += 1
            total_tt += duration
            if selected_mode == "CAR_DRIVER":
                car = True
                pc_vmt += distance
            bucket = by_mode[selected_mode]
            bucket["trip_count"] += 1
            bucket["passenger_distance_km"] += distance
            bucket["passenger_time_min"] += duration
        counts["so_agents"] += int(so)
        counts["wfh_agents"] += int(wfh)
        counts["so_wfh_both_agents"] += int(so and wfh)
        counts["private_car_users"] += int(car)
    counts["total_agents"] = len(data["agents"])
    counts["so_or_wfh_agents"] = counts["so_agents"] + counts["wfh_agents"] - counts["so_wfh_both_agents"]
    denom = counts["analysis_agents"]
    for prefix in ("so", "wfh", "so_wfh_both", "so_or_wfh"):
        counts[prefix + "_share_pct"] = ratio(counts[prefix + "_agents"], denom, 100)
    counts.update(
        passenger_car_vmt_km=pc_vmt, total_passenger_tt_min=total_tt,
        passenger_car_vmt_km_per_analysis_agent=ratio(pc_vmt, denom),
        passenger_tt_min_per_analysis_agent=ratio(total_tt, denom),
        passenger_tt_min_per_trip=ratio(total_tt, counts["trip_count"]),
    )
    params = data.get("runParameters")
    params = params if isinstance(params, dict) else {}
    provenance = {
        "input_file": path.name,
        "run_parameters": {key: params[key] for key in (
            "--seed", "--n_agents", "--sswc_mode", "--so_rate", "--wfh_rate"
        ) if key in params},
    }
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(block)
    provenance["input_sha256"] = hasher.hexdigest()
    return counts, dict(by_mode), provenance


def compare(current, baseline):
    out = dict(current)
    for metric in ("passenger_car_vmt_km", "total_passenger_tt_min", "private_car_users"):
        delta = current[metric] - baseline[metric]
        out[metric + "_change_vs_baseline"] = delta
        out[metric + "_change_vs_baseline_pct"] = ratio(delta, baseline[metric], 100)
    out["private_car_users_reduced"] = baseline["private_car_users"] - current["private_car_users"]
    return out


def write_csv(path, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--scenario", action="append", default=[], metavar="NAME=FILE",
                        help="Repeat for WFH, SO and WFH_SO; quote the whole value if it contains spaces")
    parser.add_argument("--output-dir", type=Path, default=Path("results"))
    args = parser.parse_args(argv)
    files = [("Baseline", args.baseline)]
    for item in args.scenario:
        name, separator, filename = item.partition("=")
        if not separator or not name.strip() or not filename:
            parser.error("--scenario must be NAME=FILE")
        files.append((name.strip(), Path(filename)))
    if len({name.casefold() for name, _ in files}) != len(files):
        parser.error("Scenario labels must be unique (Baseline is reserved)")
    summaries, mode_rows, sources, notices = [], [], [], []
    baseline = None
    baseline_seed = None
    for name, path in files:
        print(f"Analysing {name}: {path.name}", flush=True)
        summary, modes, source = summarize(path)
        seed = source["run_parameters"].get("--seed")
        if baseline is None:
            baseline, baseline_seed = summary, seed
        elif seed is not None and baseline_seed is not None and str(seed) != str(baseline_seed):
            raise ValueError(f"{name}: seed differs from Baseline; compare matching seeds")
        if seed is None:
            notices.append(f"{name}: seed not recorded; verify pairing manually")
        if summary["analysis_agents"] != baseline["analysis_agents"]:
            notices.append(f"{name}: analysis population differs from Baseline; no common-agent intersection was applied")
        if summary["analysis_agents"] == 0:
            notices.append(f"{name}: no retained agents; shares and per-agent metrics are undefined")
        summaries.append({"scenario": name, **compare(summary, baseline)})
        for selected_mode, values in sorted(modes.items()):
            mode_rows.append({"scenario": name, "mode": selected_mode, **values,
                              "trip_share_pct": ratio(values["trip_count"], summary["trip_count"], 100)})
        sources.append({"scenario": name, **source})
    targets = [args.output_dir / name for name in ("main_kpi.csv", "mode_breakdown.csv", "analysis_metadata.json")]
    inputs = {path.resolve() for _, path in files}
    if any(path.resolve() in inputs for path in targets):
        raise ValueError("An output path would overwrite an input")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    write_csv(targets[0], summaries)
    if mode_rows:
        write_csv(targets[1], mode_rows)
    else:
        targets[1].write_text("scenario,mode,trip_count,passenger_distance_km,passenger_time_min,trip_share_pct\n", encoding="utf-8")
    metadata = {
        "analyzer_version": VERSION,
        "scope": "All recorded days/hours; each scenario filtered independently; no population weights",
        "exclusion": "Exclude the entire agent if any selected Trip.mode is CAR_PASSENGER",
        "distance_unit": "km", "time_unit": "min",
        "undefined_ratios": "Blank CSV cells; JSON null",
        "numeric_validation": "Require finite non-negative selected-trip distance/time for retained agents",
        "sources": sources, "notices": notices,
    }
    targets[2].write_text(json.dumps(metadata, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    for notice in notices:
        print("Note: " + notice)
    print(f"Saved results to {args.output_dir}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(2)
