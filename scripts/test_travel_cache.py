#!/usr/bin/env python3
"""Focused regression tests for canonical/directed OD-cache behaviour."""

from __future__ import annotations

import asyncio
import csv
import multiprocessing
from pathlib import Path
import random
import sqlite3
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from drt_pipeline import travel
from scripts import build_od_cache


def _make_db(path: Path, rows: list[tuple[str, float, float]]) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE od_cache "
            "(route_key TEXT PRIMARY KEY, dist_km REAL, time_min REAL)"
        )
        conn.executemany("INSERT INTO od_cache VALUES (?, ?, ?)", rows)
        conn.commit()
    finally:
        conn.close()


def test_canonical_key_format() -> None:
    assert travel.DEFAULT_PROCESS_MEMORY_CACHE_MAX_ENTRIES == 524_288
    assert travel.process_memory_cache_enabled() is False
    assert travel.make_point_key(36.275, 127.32) == "36.2750,127.3200"
    assert (
        travel.make_route_key(36.275, 127.32, 36.425, 127.46)
        == "36.2750,127.3200_36.4250,127.4600"
    )


def test_legacy_region_center_key_is_readable() -> None:
    with tempfile.TemporaryDirectory(prefix="travel-cache-legacy-") as tmp:
        path = Path(tmp) / "legacy.db"
        _make_db(path, [("36.275,127.32_36.425,127.46", 12.5, 19.25)])
        travel.load_od_cache(str(path))
        assert travel.travel(36.275, 127.32, 36.425, 127.46) == (12.5, 19.25)
        stats = travel.get_od_cache_stats()
        assert stats["hits"] == 1
        assert stats["legacy_key_hits"] == 1
        assert stats["fallbacks"] == 0
        travel.close_od_cache()


def test_directional_values_are_not_reversed() -> None:
    with tempfile.TemporaryDirectory(prefix="travel-cache-directed-") as tmp:
        path = Path(tmp) / "directed.db"
        forward = travel.make_route_key(36.3, 127.3, 36.4, 127.4)
        reverse = travel.make_route_key(36.4, 127.4, 36.3, 127.3)
        _make_db(path, [(forward, 10.0, 20.0), (reverse, 13.0, 24.0)])
        travel.load_od_cache(str(path))
        assert travel.travel(36.3, 127.3, 36.4, 127.4) == (10.0, 20.0)
        assert travel.travel(36.4, 127.4, 36.3, 127.3) == (13.0, 24.0)
        stats = travel.get_od_cache_stats()
        assert stats["canonical_hits"] == 2
        travel.close_od_cache()


def test_process_memory_cache_randomized_equivalence_and_bound() -> None:
    rng = random.Random(42)
    with tempfile.TemporaryDirectory(prefix="travel-cache-memory-") as tmp:
        path = Path(tmp) / "memory.db"
        coordinates = [
            (36.20 + rng.random() * 0.4, 127.20 + rng.random() * 0.4)
            for _ in range(12)
        ]
        rows = []
        for idx, (origin, destination) in enumerate(
            zip(coordinates[:-1], coordinates[1:])
        ):
            rows.append(
                (
                    travel.make_route_key(*origin, *destination),
                    1.25 + idx,
                    2.5 + idx * 0.75,
                )
            )
        # Exercise the legacy spelling path as well as canonical hits/misses.
        rows.append(("36.275,127.32_36.425,127.46", 12.5, 19.25))
        _make_db(path, rows)
        travel.load_od_cache(str(path))

        queries = [
            (*coordinates[idx], *coordinates[idx + 1])
            for idx in range(len(coordinates) - 1)
        ]
        queries.extend(
            [
                (36.275, 127.32, 36.425, 127.46),
                (36.111111, 127.111111, 36.222222, 127.222222),
                (36.222222, 127.222222, 36.111111, 127.111111),
            ]
        )

        travel.configure_process_memory_cache(False, max_entries=5)
        expected = [travel.travel(*query) for query in queries]

        travel.configure_process_memory_cache(True, max_entries=5)
        travel.reset_od_cache_stats()
        first = [travel.travel(*query) for query in queries]
        second = [travel.travel(*query) for query in queries[-5:]]
        assert first == expected
        assert second == expected[-5:]
        stats = travel.get_od_cache_stats()
        assert stats["memory_enabled"] is True
        assert stats["memory_max_entries"] == 5
        assert stats["memory_size"] == 5
        assert stats["memory_hits"] == 5
        assert stats["memory_misses"] == len(queries)
        assert stats["memory_evictions"] == len(queries) - 5

        # Resetting per-solver diagnostics must retain process-wide values.
        warm_query = queries[-1]
        travel.reset_od_cache_stats()
        assert travel.travel(*warm_query) == expected[-1]
        reset_stats = travel.get_od_cache_stats()
        assert reset_stats["memory_hits"] == 1
        assert reset_stats["memory_misses"] == 0
        assert reset_stats["memory_size"] == 5

        # Direction remains part of the key: the reverse miss is not allowed
        # to reuse the forward fallback/result.
        assert queries[-2] != queries[-1]
        travel.configure_process_memory_cache(
            False, max_entries=travel.DEFAULT_PROCESS_MEMORY_CACHE_MAX_ENTRIES
        )
        travel.close_od_cache()


def test_process_memory_cache_preserves_exact_legacy_keys() -> None:
    """Raw inputs inside one canonical bucket must not share legacy results."""
    with tempfile.TemporaryDirectory(prefix="travel-cache-legacy-collision-") as tmp:
        path = Path(tmp) / "legacy-collision.db"
        origin_a = (36.123441, 127.100001)
        origin_b = (36.123449, 127.100001)
        destination = (36.500001, 127.500001)
        assert travel.make_point_key(*origin_a) == travel.make_point_key(*origin_b)
        destination_raw = "36.500001,127.500001"
        _make_db(
            path,
            [
                (f"36.123441,127.100001_{destination_raw}", 7.0, 11.0),
                (f"36.123449,127.100001_{destination_raw}", 9.0, 13.0),
            ],
        )
        travel.configure_process_memory_cache(True, max_entries=8)
        travel.load_od_cache(str(path))
        travel.reset_od_cache_stats()

        assert travel.travel(*origin_a, *destination) == (7.0, 11.0)
        assert travel.travel(*origin_b, *destination) == (9.0, 13.0)
        assert travel.travel(*origin_a, *destination) == (7.0, 11.0)
        stats = travel.get_od_cache_stats()
        assert stats["legacy_key_hits"] == 2
        assert stats["memory_misses"] == 2
        assert stats["memory_hits"] == 1
        assert stats["memory_key_semantics"] == "exact_raw_directed_4_tuple_typed"

        travel.configure_process_memory_cache(
            False, max_entries=travel.DEFAULT_PROCESS_MEMORY_CACHE_MAX_ENTRIES
        )
        travel.close_od_cache()


def test_process_memory_cache_invalidation() -> None:
    query = (36.111111, 127.111111, 36.222222, 127.222222)
    with tempfile.TemporaryDirectory(prefix="travel-cache-invalidation-") as tmp:
        tmp_path = Path(tmp)
        empty_path = tmp_path / "empty.db"
        _make_db(empty_path, [])
        travel.configure_process_memory_cache(True, max_entries=8)
        travel.load_od_cache(str(empty_path))
        travel.set_speed_kmph(30.0)
        travel.reset_od_cache_stats()

        dist_30, time_30 = travel.travel(*query)
        assert travel.travel(*query) == (dist_30, time_30)
        travel.set_speed_kmph(60.0)
        dist_60, time_60 = travel.travel(*query)
        assert dist_60 == dist_30
        assert abs(time_60 * 2.0 - time_30) <= 1e-12
        stats = travel.get_od_cache_stats()
        assert stats["memory_hits"] == 1
        assert stats["memory_misses"] == 2
        assert stats["memory_clears"] >= 1

        # A fallback cached in non-strict mode must never mask a later strict
        # miss after the policy changes.
        travel.set_od_cache_strict(True)
        try:
            travel.travel(*query)
        except travel.ODCacheMissError:
            pass
        else:
            raise AssertionError("cached non-strict fallback masked strict miss")
        travel.set_od_cache_strict(False)

        # Reloading a different path with the same route key must return the
        # new database's value, not a prior process-memory entry.
        route_key = travel.make_route_key(*query)
        path_a = tmp_path / "a.db"
        path_b = tmp_path / "b.db"
        _make_db(path_a, [(route_key, 4.0, 8.0)])
        _make_db(path_b, [(route_key, 5.0, 9.0)])
        travel.load_od_cache(str(path_a))
        assert travel.travel(*query) == (4.0, 8.0)
        assert travel.travel(*query) == (4.0, 8.0)
        travel.load_od_cache(str(path_b))
        assert travel.travel(*query) == (5.0, 9.0)
        stats = travel.get_od_cache_stats()
        assert stats["memory_hits"] == 0
        assert stats["memory_misses"] == 1

        travel.set_speed_kmph(45.0)
        travel.configure_process_memory_cache(
            False, max_entries=travel.DEFAULT_PROCESS_MEMORY_CACHE_MAX_ENTRIES
        )
        travel.close_od_cache()


def test_miss_metrics_and_strict_mode() -> None:
    with tempfile.TemporaryDirectory(prefix="travel-cache-strict-") as tmp:
        path = Path(tmp) / "strict.db"
        _make_db(path, [])

        travel.load_od_cache(str(path))
        dist, duration = travel.travel(36.1, 127.1, 36.2, 127.2)
        assert dist > 0.0 and duration > 0.0
        stats = travel.get_od_cache_stats()
        assert stats["misses"] == 1
        assert stats["fallbacks"] == 1

        travel.load_od_cache(str(path), strict=True)
        try:
            travel.travel(36.1, 127.1, 36.2, 127.2)
        except travel.ODCacheMissError:
            pass
        else:
            raise AssertionError("strict cache miss did not raise ODCacheMissError")
        travel.close_od_cache()


def _forked_lookup(result_queue: multiprocessing.Queue) -> None:
    result = travel.travel(36.3, 127.3, 36.4, 127.4)
    result_queue.put((result, travel.get_od_cache_stats()))
    travel.close_od_cache()


def test_forked_worker_reopens_connection() -> None:
    if "fork" not in multiprocessing.get_all_start_methods():
        return
    with tempfile.TemporaryDirectory(prefix="travel-cache-fork-") as tmp:
        path = Path(tmp) / "fork.db"
        key = travel.make_route_key(36.3, 127.3, 36.4, 127.4)
        _make_db(path, [(key, 7.0, 11.0)])
        travel.configure_process_memory_cache(True, max_entries=8)
        travel.load_od_cache(str(path))
        # Warm the parent LRU.  The child must discard this value and execute
        # its own SQLite lookup after fork.
        assert travel.travel(36.3, 127.3, 36.4, 127.4) == (7.0, 11.0)
        parent_pid = travel.get_od_cache_stats()["connection_pid"]

        ctx = multiprocessing.get_context("fork")
        result_queue = ctx.Queue()
        process = ctx.Process(target=_forked_lookup, args=(result_queue,))
        process.start()
        process.join(timeout=10)
        assert process.exitcode == 0
        result, child_stats = result_queue.get(timeout=2)
        assert tuple(result) == (7.0, 11.0)
        assert child_stats["connection_pid"] != parent_pid
        assert child_stats["connections_opened"] >= 2
        assert child_stats["memory_pid"] == child_stats["pid"]
        assert child_stats["memory_hits"] == 0
        assert child_stats["memory_misses"] == 1
        assert child_stats["memory_size"] == 1
        travel.configure_process_memory_cache(
            False, max_entries=travel.DEFAULT_PROCESS_MEMORY_CACHE_MAX_ENTRIES
        )
        travel.close_od_cache()


def test_builder_computes_both_directions() -> None:
    calls: list[str] = []

    async def fake_get_route(session, gh_url, lat1, lon1, lat2, lon2, sem):
        key = f"{lat1},{lon1}_{lat2},{lon2}"
        calls.append(key)
        if (lat1, lon1) == (lat2, lon2):
            return key, 0.0, 0.0, None
        direction_bias = 1.0 if (lat1, lon1) < (lat2, lon2) else 2.0
        return key, direction_bias, direction_bias * 10.0, None

    with tempfile.TemporaryDirectory(prefix="travel-cache-builder-") as tmp:
        tmp_path = Path(tmp)
        csv_path = tmp_path / "requests.csv"
        with csv_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=["pickup_lat", "pickup_lon", "drop_lat", "drop_lon"],
            )
            writer.writeheader()
            writer.writerow(
                {
                    "pickup_lat": 36.11111,
                    "pickup_lon": 127.11111,
                    "drop_lat": 36.22222,
                    "drop_lon": 127.22222,
                }
            )

        out_json = tmp_path / "cache.json"
        original = build_od_cache.get_route
        build_od_cache.get_route = fake_get_route
        try:
            asyncio.run(
                build_od_cache.build_cache_async(
                    str(csv_path), "http://unused.invalid", str(out_json), workers=4
                )
            )
        finally:
            build_od_cache.get_route = original

        coords = build_od_cache._load_coordinates(str(csv_path))
        expected_count = len(coords) ** 2
        assert len(calls) == expected_count

        conn = sqlite3.connect(out_json.with_suffix(".db"))
        try:
            row_count = conn.execute("SELECT COUNT(*) FROM od_cache").fetchone()[0]
            assert row_count == expected_count
            origin = ("36.1111", "127.1111")
            destination = ("36.2222", "127.2222")
            forward_key = f"{origin[0]},{origin[1]}_{destination[0]},{destination[1]}"
            reverse_key = f"{destination[0]},{destination[1]}_{origin[0]},{origin[1]}"
            forward = conn.execute(
                "SELECT dist_km, time_min FROM od_cache WHERE route_key=?", (forward_key,)
            ).fetchone()
            reverse = conn.execute(
                "SELECT dist_km, time_min FROM od_cache WHERE route_key=?", (reverse_key,)
            ).fetchone()
            assert forward == (1.0, 10.0)
            assert reverse == (2.0, 20.0)
            directionality = conn.execute(
                "SELECT meta_value FROM od_cache_metadata WHERE meta_key='directionality'"
            ).fetchone()[0]
            assert directionality == "directed"
        finally:
            conn.close()


def main() -> None:
    tests = [
        test_canonical_key_format,
        test_legacy_region_center_key_is_readable,
        test_directional_values_are_not_reversed,
        test_process_memory_cache_randomized_equivalence_and_bound,
        test_process_memory_cache_preserves_exact_legacy_keys,
        test_process_memory_cache_invalidation,
        test_miss_metrics_and_strict_mode,
        test_forked_worker_reopens_connection,
        test_builder_computes_both_directions,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")


if __name__ == "__main__":
    main()
