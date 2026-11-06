#!/usr/bin/env python3
"""Build a canonical, directed GraphHopper OD cache in SQLite."""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import csv
import logging
import math
from pathlib import Path
import sqlite3
import sys
import time
from typing import Iterable, Iterator, Sequence, Set, Tuple

import aiohttp

# Adjust path to import drt_pipeline when invoked as a script.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from drt_pipeline.constants import SIX_REGIONS
from drt_pipeline.regions import region_center
from drt_pipeline.travel import (
    CACHE_COORD_DECIMALS,
    format_coordinate,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

Coordinate = Tuple[str, str]
DirectedPair = Tuple[str, str, str, str, str]


async def get_route(
    session: aiohttp.ClientSession,
    gh_url: str,
    lat1: str,
    lon1: str,
    lat2: str,
    lon2: str,
    sem: asyncio.Semaphore,
) -> Tuple[str, float, float, str | None]:
    """Fetch exactly one directed GraphHopper route."""
    key = f"{lat1},{lon1}_{lat2},{lon2}"
    if lat1 == lat2 and lon1 == lon2:
        return key, 0.0, 0.0, None

    url = f"{gh_url.rstrip('/')}/route"
    params = {
        "point": [f"{lat1},{lon1}", f"{lat2},{lon2}"],
        "profile": "car",
        "instructions": "false",
        "calc_points": "false",
    }

    async with sem:
        try:
            async with session.get(url, params=params, timeout=10) as resp:
                if resp.status != 200:
                    return key, -1.0, -1.0, f"http_{resp.status}"
                data = await resp.json()
                paths = data.get("paths", [])
                if not paths:
                    return key, -1.0, -1.0, "no_path"
                dist_km = float(paths[0]["distance"]) / 1000.0
                time_min = float(paths[0]["time"]) / 60000.0
                if not math.isfinite(dist_km) or not math.isfinite(time_min):
                    return key, -1.0, -1.0, "non_finite"
                if dist_km < 0.0 or time_min < 0.0:
                    return key, -1.0, -1.0, "negative_value"
                return key, dist_km, time_min, None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return key, -1.0, -1.0, type(exc).__name__


def _canonical_coordinate_pair(lat: object, lon: object) -> Coordinate:
    return format_coordinate(float(lat)), format_coordinate(float(lon))


def _load_coordinates(csv_file: str) -> list[Coordinate]:
    # Four decimal places cluster points about 11 metres apart and must match
    # drt_pipeline.travel.make_route_key exactly.
    coords: Set[Coordinate] = set()
    with open(csv_file, "r", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            if "pickup_lat" in row and "pickup_lon" in row:
                coords.add(_canonical_coordinate_pair(row["pickup_lat"], row["pickup_lon"]))
            if "drop_lat" in row and "drop_lon" in row:
                coords.add(_canonical_coordinate_pair(row["drop_lat"], row["drop_lon"]))

    # Region centres previously entered the set as floats, creating compact
    # keys such as 36.275.  Canonicalising here removes those duplicate points.
    for region in SIX_REGIONS:
        c_lat, c_lon = region_center(region)
        coords.add(_canonical_coordinate_pair(c_lat, c_lon))

    return sorted(coords)


def _iter_directional_pairs(coords: Sequence[Coordinate]) -> Iterator[DirectedPair]:
    """Yield every ordered pair; A->B and B->A are separate entries."""
    for lat1, lon1 in coords:
        for lat2, lon2 in coords:
            yield (
                f"{lat1},{lon1}_{lat2},{lon2}",
                lat1,
                lon1,
                lat2,
                lon2,
            )


def _chunked(items: Iterable[DirectedPair], size: int) -> Iterator[list[DirectedPair]]:
    chunk: list[DirectedPair] = []
    for item in items:
        chunk.append(item)
        if len(chunk) >= size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def _is_valid_cached_value(dist_km: object, time_min: object) -> bool:
    try:
        dist = float(dist_km)
        duration = float(time_min)
    except (TypeError, ValueError):
        return False
    return (
        math.isfinite(dist)
        and math.isfinite(duration)
        and dist >= 0.0
        and duration >= 0.0
    )


def _missing_pairs(
    conn: sqlite3.Connection,
    candidates: Sequence[DirectedPair],
) -> Tuple[list[Tuple[str, str, str, str]], int]:
    """Return missing/invalid canonical pairs and number of valid hits."""
    keys = [item[0] for item in candidates]
    placeholders = ",".join("?" for _ in keys)
    rows = conn.execute(
        "SELECT route_key, dist_km, time_min FROM od_cache "
        f"WHERE route_key IN ({placeholders})",
        keys,
    ).fetchall()
    valid_keys = {
        str(key)
        for key, dist_km, time_min in rows
        if _is_valid_cached_value(dist_km, time_min)
    }
    missing = [item[1:] for item in candidates if item[0] not in valid_keys]
    return missing, len(candidates) - len(missing)


def _prepare_database(conn: sqlite3.Connection) -> str:
    """Create schema and return its directionality provenance label."""
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS od_cache "
        "(route_key TEXT PRIMARY KEY, dist_km REAL, time_min REAL)"
    )
    has_existing_rows = bool(
        conn.execute("SELECT EXISTS(SELECT 1 FROM od_cache LIMIT 1)").fetchone()[0]
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS od_cache_metadata "
        "(meta_key TEXT PRIMARY KEY, meta_value TEXT NOT NULL)"
    )
    prior = conn.execute(
        "SELECT meta_value FROM od_cache_metadata WHERE meta_key='directionality'"
    ).fetchone()
    if prior is not None:
        directionality = str(prior[0])
    elif not has_existing_rows:
        directionality = "directed"
    else:
        # Old builders mirrored one GraphHopper result in both directions.
        # Preserve those rows for compatibility, but label their provenance.
        directionality = "legacy_mixed"

    metadata = {
        "builder_version": "2",
        "coordinate_decimals": str(CACHE_COORD_DECIMALS),
        "key_format": "fixed_decimal_origin_destination",
        "directionality": directionality,
    }
    conn.executemany(
        "INSERT OR REPLACE INTO od_cache_metadata(meta_key, meta_value) VALUES (?, ?)",
        metadata.items(),
    )
    conn.commit()
    return directionality


async def _fetch_batch(
    conn: sqlite3.Connection,
    session: aiohttp.ClientSession,
    gh_url: str,
    pairs: Sequence[Tuple[str, str, str, str]],
    sem: asyncio.Semaphore,
) -> Tuple[int, Counter[str]]:
    tasks = [
        asyncio.create_task(get_route(session, gh_url, lat1, lon1, lat2, lon2, sem))
        for lat1, lon1, lat2, lon2 in pairs
    ]
    rows: list[Tuple[str, float, float]] = []
    errors: Counter[str] = Counter()
    try:
        for future in asyncio.as_completed(tasks):
            try:
                key, dist_km, time_min, error = await future
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                errors[type(exc).__name__] += 1
                continue
            if error is None:
                rows.append((key, dist_km, time_min))
            else:
                errors[error] += 1
    finally:
        pending = [task for task in tasks if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    if rows:
        conn.executemany(
            "INSERT OR REPLACE INTO od_cache(route_key, dist_km, time_min) "
            "VALUES (?, ?, ?)",
            rows,
        )
        conn.commit()
    # Failed routes are intentionally not persisted so a later build retries.
    return len(rows), errors


async def _build_cache_async_unlocked(
    csv_file: str,
    gh_url: str,
    out_file: str,
    workers: int,
    *,
    allow_partial: bool = False,
) -> None:
    try:
        coord_list = _load_coordinates(csv_file)
    except Exception as exc:
        logging.error("Failed to read CSV %s: %s", csv_file, exc)
        raise

    n = len(coord_list)
    total_pairs = n * n
    logging.info("Unique canonical coordinates found: %d", n)
    logging.info("Total directed O-D pairs to scan: %d", total_pairs)

    out_path = Path(out_file)
    if out_path.suffix.lower() == ".json":
        out_path = out_path.with_suffix(".db")

    conn = sqlite3.connect(out_path)
    directionality = _prepare_database(conn)
    logging.info("OD-cache directionality provenance: %s", directionality)

    worker_count = max(1, int(workers))
    sem = asyncio.Semaphore(worker_count)
    # Bound task memory while keeping enough work queued for GraphHopper.
    request_batch_size = max(1000, min(20000, worker_count * 20))
    query_batch_size = 900  # Safe below SQLite's conservative variable limit.

    scanned = 0
    valid_existing = 0
    requested = 0
    saved = 0
    all_errors: Counter[str] = Counter()
    pending_requests: list[Tuple[str, str, str, str]] = []
    started = time.monotonic()

    connector = aiohttp.TCPConnector(limit=worker_count, force_close=False)
    try:
        async with aiohttp.ClientSession(connector=connector) as session:
            for candidate_chunk in _chunked(_iter_directional_pairs(coord_list), query_batch_size):
                missing, hit_count = _missing_pairs(conn, candidate_chunk)
                scanned += len(candidate_chunk)
                valid_existing += hit_count
                pending_requests.extend(missing)

                while len(pending_requests) >= request_batch_size:
                    batch = pending_requests[:request_batch_size]
                    del pending_requests[:request_batch_size]
                    batch_saved, errors = await _fetch_batch(
                        conn, session, gh_url, batch, sem
                    )
                    requested += len(batch)
                    saved += batch_saved
                    all_errors.update(errors)
                    elapsed = max(time.monotonic() - started, 1e-9)
                    logging.info(
                        "Progress: scanned=%d/%d, requested=%d, saved=%d, "
                        "failed=%d (%.1f GraphHopper req/s)",
                        scanned,
                        total_pairs,
                        requested,
                        saved,
                        requested - saved,
                        requested / elapsed,
                    )

            if pending_requests:
                batch_saved, errors = await _fetch_batch(
                    conn, session, gh_url, pending_requests, sem
                )
                requested += len(pending_requests)
                saved += batch_saved
                all_errors.update(errors)
    finally:
        conn.close()

    logging.info(
        "Finished cache build: scanned=%d, already_valid=%d, requested=%d, saved=%d, failed=%d",
        scanned,
        valid_existing,
        requested,
        saved,
        requested - saved,
    )
    if all_errors:
        logging.warning("GraphHopper failures were not cached and will be retried: %s", dict(all_errors))
    failed = requested - saved
    if failed and not allow_partial:
        raise RuntimeError(
            f"OD cache build incomplete: {failed}/{requested} GraphHopper routes failed; "
            "restart GraphHopper and rerun, or explicitly pass --allow-partial"
        )


async def build_cache_async(csv_file, gh_url, out_file, workers, *, allow_partial=False):
    # All supported writers cooperate on the same canonical database lock.
    import fcntl
    from drt_pipeline.travel import _normalise_cache_path
    path = _normalise_cache_path(out_file)
    with open(path + ".writer.lock", "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return await _build_cache_async_unlocked(csv_file, gh_url, path, workers, allow_partial=allow_partial)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", type=str, required=True, help="Path to stage0_requests.csv")
    parser.add_argument(
        "--gh_url", type=str, default="http://localhost:8989", help="GraphHopper URL"
    )
    parser.add_argument("--out", type=str, default="od_cache.json", help="Output SQLite cache path")
    parser.add_argument("--workers", type=int, default=1000, help="Parallel GraphHopper requests")
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help="Exit successfully even when some GraphHopper routes could not be cached",
    )
    args = parser.parse_args()

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    try:
        asyncio.run(
            build_cache_async(
                args.csv,
                args.gh_url,
                args.out,
                args.workers,
                allow_partial=args.allow_partial,
            )
        )
    except KeyboardInterrupt:
        print("\n[INFO] Cancelled by user (Ctrl+C). Exiting gracefully...")
        sys.exit(0)
    except Exception as exc:
        print(f"\n[ERROR] Process failed: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
