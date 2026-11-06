"""Travel distance/time lookup with an optional GraphHopper OD cache.

Coordinates are clustered at four decimal places (roughly 11 metres).  Cache
connections are opened read-only per process/thread so a connection inherited
by a multiprocessing worker is never reused after ``fork``.
"""

from __future__ import annotations

from collections import Counter
from functools import lru_cache
import logging
import math
import os
from pathlib import Path
import sqlite3
import threading
from typing import Dict, Iterable, Tuple
from .service_area import configured_area

SPEED_KMPH = 45.0
KM_PER_DEG_LAT = 111.32
CACHE_COORD_DECIMALS = 4

# Daejeon/Sejong mean latitude (36.46 degrees).  Its cosine varies by less
# than 0.8% over the service area, so computing it once is sufficient here.
_SERVICE_AREA = configured_area()
_MEAN_LAT_RAD = math.radians(_SERVICE_AREA.mean_latitude if _SERVICE_AREA is not None else 36.46)
KM_PER_DEG_LON = KM_PER_DEG_LAT * math.cos(_MEAN_LAT_RAD)


class ODCacheError(RuntimeError):
    """Base class for OD-cache failures in strict mode."""


class ODCacheMissError(ODCacheError):
    """Raised when a strict cache lookup cannot return a valid route."""


# ``OD_DB_CONN`` is retained for compatibility with callers that may inspect
# the module, but travel() obtains its connection through _get_od_connection().
OD_DB_CONN: sqlite3.Connection | None = None
CACHE_MISS_WARNING = False

_OD_CACHE_PATH: str | None = None
_OD_CACHE_STRICT = False
_OD_CACHE_GENERATION = 0
_THREAD_STATE = threading.local()
_WARNED_REASONS: set[str] = set()
DEFAULT_PROCESS_MEMORY_CACHE_MAX_ENTRIES = 524_288
_PROCESS_MEMORY_CACHE_ENABLED = False
_PROCESS_MEMORY_CACHE_MAX_ENTRIES = DEFAULT_PROCESS_MEMORY_CACHE_MAX_ENTRIES
_PROCESS_MEMORY_CACHE_PID = os.getpid()
_PROCESS_MEMORY_CACHE_GENERATION = _OD_CACHE_GENERATION
# The concrete type is functools._lru_cache_wrapper (a private implementation
# detail), so keep this unannotated instead of importing a private type.  It is
# initialized after _travel_uncached() is defined.
_PROCESS_MEMORY_CACHE = None
_PROCESS_MEMORY_CACHE_LOCK = threading.RLock()
_PROCESS_MEMORY_CACHE_ROLLED_HITS = 0
_PROCESS_MEMORY_CACHE_ROLLED_MISSES = 0
_PROCESS_MEMORY_CACHE_STATS_BASE_HITS = 0
_PROCESS_MEMORY_CACHE_STATS_BASE_MISSES = 0
_STAT_NAMES = (
    "loads",
    "load_errors",
    "connections_opened",
    "hits",
    "canonical_hits",
    "legacy_key_hits",
    "misses",
    "invalid_rows",
    "lookup_errors",
    "fallbacks",
    "uncached_fallbacks",
    "memory_hits",
    "memory_misses",
    "memory_evictions",
    "memory_clears",
)
_OD_CACHE_STATS: Counter[str] = Counter()


def format_coordinate(value: float) -> str:
    """Return the canonical cache representation of one coordinate."""
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"coordinate must be finite, got {value!r}")
    # Avoid producing the surprising string ``-0.0000``.
    if abs(number) < 0.5 * 10 ** (-CACHE_COORD_DECIMALS):
        number = 0.0
    return f"{number:.{CACHE_COORD_DECIMALS}f}"


def make_point_key(lat: float, lon: float) -> str:
    """Return a canonical ``lat,lon`` cache key."""
    return f"{format_coordinate(lat)},{format_coordinate(lon)}"


def make_route_key(lat1: float, lon1: float, lat2: float, lon2: float) -> str:
    """Return the canonical, directed ``origin_destination`` cache key."""
    return f"{make_point_key(lat1, lon1)}_{make_point_key(lat2, lon2)}"


def _legacy_point_keys(lat: float, lon: float) -> Tuple[str, ...]:
    """Return historical point spellings used by older cache builders.

    Request coordinates were stored as four-decimal strings, while region
    centres were stored as rounded floats (for example ``36.275`` rather than
    ``36.2750``).  Both compact canonical and raw-six-decimal spellings are
    tried to make existing databases usable without rewriting them.
    """
    canonical = make_point_key(lat, lon)
    compact = f"{float(format_coordinate(lat))},{float(format_coordinate(lon))}"
    rounded_raw = f"{round(float(lat), 6)},{round(float(lon), 6)}"
    return tuple(dict.fromkeys((canonical, compact, rounded_raw)))


def _legacy_route_keys(
    lat1: float,
    lon1: float,
    lat2: float,
    lon2: float,
) -> Iterable[str]:
    canonical = make_route_key(lat1, lon1, lat2, lon2)
    for origin in _legacy_point_keys(lat1, lon1):
        for destination in _legacy_point_keys(lat2, lon2):
            key = f"{origin}_{destination}"
            if key != canonical:
                yield key


def _normalise_cache_path(filepath: str) -> str:
    path = Path(filepath).expanduser()
    if path.suffix.lower() == ".json":
        path = path.with_suffix(".db")
    return str(path.resolve())


def _build_process_memory_cache(max_entries: int):
    """Return a bounded C-level LRU wrapper around the historical lookup."""

    @lru_cache(maxsize=max_entries, typed=True)
    def cached(
        lat1: float, lon1: float, lat2: float, lon2: float
    ) -> Tuple[float, float]:
        result = _travel_uncached(lat1, lon1, lat2, lon2)
        # This function runs only on an LRU miss.  Before the wrapper inserts
        # the successful result, a full cache implies that one entry will be
        # evicted.  The solver calls travel from one thread per process; the
        # LRU wrapper itself still provides the standard thread-safe behavior.
        info = cached.cache_info()
        if info.maxsize is not None and info.currsize >= info.maxsize:
            _OD_CACHE_STATS["memory_evictions"] += 1
        return result

    return cached


def _memory_cache_totals_unlocked() -> Tuple[int, int]:
    """Return lifetime LRU hit/miss counts, including prior cache generations."""
    if _PROCESS_MEMORY_CACHE is None:
        return _PROCESS_MEMORY_CACHE_ROLLED_HITS, _PROCESS_MEMORY_CACHE_ROLLED_MISSES
    info = _PROCESS_MEMORY_CACHE.cache_info()
    return (
        _PROCESS_MEMORY_CACHE_ROLLED_HITS + int(info.hits),
        _PROCESS_MEMORY_CACHE_ROLLED_MISSES + int(info.misses),
    )


def _roll_process_memory_cache_unlocked() -> bool:
    """Accumulate LRU counters, clear entries, and report whether any existed."""
    global _PROCESS_MEMORY_CACHE_ROLLED_HITS, _PROCESS_MEMORY_CACHE_ROLLED_MISSES
    if _PROCESS_MEMORY_CACHE is None:
        return False
    info = _PROCESS_MEMORY_CACHE.cache_info()
    _PROCESS_MEMORY_CACHE_ROLLED_HITS += int(info.hits)
    _PROCESS_MEMORY_CACHE_ROLLED_MISSES += int(info.misses)
    had_entries = bool(info.currsize)
    _PROCESS_MEMORY_CACHE.cache_clear()
    return had_entries


def _clear_process_memory_cache(*, count_clear: bool = True) -> None:
    """Drop bounded process-local values while retaining additive counters."""
    global _PROCESS_MEMORY_CACHE_PID, _PROCESS_MEMORY_CACHE_GENERATION
    with _PROCESS_MEMORY_CACHE_LOCK:
        had_entries = _roll_process_memory_cache_unlocked()
        _PROCESS_MEMORY_CACHE_PID = os.getpid()
        _PROCESS_MEMORY_CACHE_GENERATION = _OD_CACHE_GENERATION
        if count_clear and had_entries:
            _OD_CACHE_STATS["memory_clears"] += 1


def _reset_process_memory_cache_after_fork() -> None:
    """Replace inherited LRU/lock state in a child process without reusing values."""
    global _PROCESS_MEMORY_CACHE
    global _PROCESS_MEMORY_CACHE_LOCK
    global _PROCESS_MEMORY_CACHE_PID, _PROCESS_MEMORY_CACHE_GENERATION
    global _PROCESS_MEMORY_CACHE_ROLLED_HITS, _PROCESS_MEMORY_CACHE_ROLLED_MISSES
    global _PROCESS_MEMORY_CACHE_STATS_BASE_HITS
    global _PROCESS_MEMORY_CACHE_STATS_BASE_MISSES

    # Replacing, rather than clearing, avoids touching an internal/external
    # lock that another parent thread could have held at fork time.
    _PROCESS_MEMORY_CACHE_LOCK = threading.RLock()
    _PROCESS_MEMORY_CACHE = _build_process_memory_cache(
        _PROCESS_MEMORY_CACHE_MAX_ENTRIES
    )
    _PROCESS_MEMORY_CACHE_PID = os.getpid()
    _PROCESS_MEMORY_CACHE_GENERATION = _OD_CACHE_GENERATION
    _PROCESS_MEMORY_CACHE_ROLLED_HITS = 0
    _PROCESS_MEMORY_CACHE_ROLLED_MISSES = 0
    _PROCESS_MEMORY_CACHE_STATS_BASE_HITS = 0
    _PROCESS_MEMORY_CACHE_STATS_BASE_MISSES = 0


def _ensure_process_memory_cache_state() -> None:
    """Discard cache state inherited across fork or invalidated by reconfiguration."""
    if _PROCESS_MEMORY_CACHE_PID != os.getpid():
        _reset_process_memory_cache_after_fork()
    elif _PROCESS_MEMORY_CACHE_GENERATION != _OD_CACHE_GENERATION:
        _clear_process_memory_cache()


def configure_process_memory_cache(
    enabled: bool,
    *,
    max_entries: int = DEFAULT_PROCESS_MEMORY_CACHE_MAX_ENTRIES,
) -> None:
    """Configure the bounded solver-wide cache used by this process.

    The key keeps all four directed input coordinates.  SQLite canonical and
    legacy-key resolution therefore still runs unchanged on the first lookup;
    only an identical subsequent call in the same process is reused.
    """
    global _PROCESS_MEMORY_CACHE
    global _PROCESS_MEMORY_CACHE_ENABLED, _PROCESS_MEMORY_CACHE_MAX_ENTRIES
    max_entries = int(max_entries)
    if max_entries < 1:
        raise ValueError("process memory travel cache max_entries must be >= 1")
    enabled = bool(enabled)
    _ensure_process_memory_cache_state()
    with _PROCESS_MEMORY_CACHE_LOCK:
        enabled_changed = enabled != _PROCESS_MEMORY_CACHE_ENABLED
        capacity_changed = max_entries != _PROCESS_MEMORY_CACHE_MAX_ENTRIES
        if enabled_changed or capacity_changed:
            had_entries = _roll_process_memory_cache_unlocked()
            if had_entries:
                _OD_CACHE_STATS["memory_clears"] += 1
            _PROCESS_MEMORY_CACHE_MAX_ENTRIES = max_entries
            _PROCESS_MEMORY_CACHE = _build_process_memory_cache(max_entries)
        _PROCESS_MEMORY_CACHE_ENABLED = enabled


def process_memory_cache_enabled() -> bool:
    """Return the effective process-memory policy without touching hot state."""
    return _PROCESS_MEMORY_CACHE_ENABLED


def clear_process_memory_cache() -> None:
    """Explicitly clear cached travel values (primarily for tests/diagnostics)."""
    _clear_process_memory_cache()


def _close_thread_connection() -> None:
    global OD_DB_CONN
    conn = getattr(_THREAD_STATE, "connection", None)
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:
            logging.debug("Failed to close OD-cache connection", exc_info=True)
    _THREAD_STATE.connection = None
    _THREAD_STATE.pid = None
    _THREAD_STATE.generation = None
    OD_DB_CONN = None


def _open_readonly_connection(path: str) -> sqlite3.Connection:
    # URI read-only mode prevents an accidental database creation or write.
    conn = sqlite3.connect(
        f"file:{path}?mode=ro",
        uri=True,
        timeout=5.0,
        check_same_thread=True,
    )
    conn.execute("PRAGMA query_only=ON")
    conn.execute("SELECT dist_km, time_min FROM od_cache WHERE 0")
    return conn


def _get_od_connection() -> sqlite3.Connection | None:
    global OD_DB_CONN
    if _OD_CACHE_PATH is None:
        return None

    pid = os.getpid()
    conn = getattr(_THREAD_STATE, "connection", None)
    if (
        conn is not None
        and getattr(_THREAD_STATE, "pid", None) == pid
        and getattr(_THREAD_STATE, "generation", None) == _OD_CACHE_GENERATION
    ):
        return conn

    # A forked worker inherits the parent's Python object and file descriptor.
    # Close that copy, then establish its own SQLite read connection.
    _close_thread_connection()
    try:
        conn = _open_readonly_connection(_OD_CACHE_PATH)
    except Exception as exc:
        _OD_CACHE_STATS["load_errors"] += 1
        if _OD_CACHE_STRICT:
            raise ODCacheError(
                f"failed to open OD cache {_OD_CACHE_PATH}: {exc}"
            ) from exc
        _warn_once(
            "connection_error",
            f"Failed to open OD cache {_OD_CACHE_PATH}: {exc}. "
            "Falling back to Manhattan travel.",
        )
        return None

    _THREAD_STATE.connection = conn
    _THREAD_STATE.pid = pid
    _THREAD_STATE.generation = _OD_CACHE_GENERATION
    OD_DB_CONN = conn
    _OD_CACHE_STATS["connections_opened"] += 1
    return conn


def load_od_cache(filepath: str, *, strict: bool = False) -> None:
    """Configure and validate a precomputed GraphHopper SQLite OD cache.

    ``strict=False`` preserves the historical behaviour: load errors, misses,
    and invalid rows use the Manhattan fallback after emitting one warning.
    Set ``strict=True`` in validation runs to fail immediately instead.
    """
    global _OD_CACHE_PATH, _OD_CACHE_STRICT, _OD_CACHE_GENERATION
    global CACHE_MISS_WARNING

    _close_thread_connection()
    _OD_CACHE_GENERATION += 1
    _clear_process_memory_cache(count_clear=False)
    _OD_CACHE_PATH = _normalise_cache_path(filepath)
    _OD_CACHE_STRICT = bool(strict)
    CACHE_MISS_WARNING = False
    _WARNED_REASONS.clear()
    reset_od_cache_stats()
    _OD_CACHE_STATS["loads"] += 1

    try:
        _get_od_connection()
    except ODCacheError:
        _OD_CACHE_PATH = None
        raise

    if OD_DB_CONN is None:
        # A non-strict validation failure disables repeated open attempts.
        _OD_CACHE_PATH = None
        return
    logging.info("Connected to read-only OD cache SQLite DB at %s", _OD_CACHE_PATH)


def close_od_cache() -> None:
    """Close this thread's cache connection and disable cache lookups."""
    global _OD_CACHE_PATH, _OD_CACHE_GENERATION
    _close_thread_connection()
    _OD_CACHE_PATH = None
    _OD_CACHE_GENERATION += 1
    _clear_process_memory_cache(count_clear=False)


def set_od_cache_strict(strict: bool) -> None:
    """Enable or disable fail-fast handling for the currently loaded cache."""
    global _OD_CACHE_STRICT
    strict = bool(strict)
    if strict != _OD_CACHE_STRICT:
        _OD_CACHE_STRICT = strict
        _clear_process_memory_cache()


def reset_od_cache_stats() -> None:
    """Reset lookup counters for the current process."""
    global _PROCESS_MEMORY_CACHE_STATS_BASE_HITS
    global _PROCESS_MEMORY_CACHE_STATS_BASE_MISSES
    _ensure_process_memory_cache_state()
    with _PROCESS_MEMORY_CACHE_LOCK:
        (
            _PROCESS_MEMORY_CACHE_STATS_BASE_HITS,
            _PROCESS_MEMORY_CACHE_STATS_BASE_MISSES,
        ) = _memory_cache_totals_unlocked()
    _OD_CACHE_STATS.clear()


def get_od_cache_stats(*, reset: bool = False) -> Dict[str, object]:
    """Return hit/miss/fallback counters for the current process.

    Multiprocessing workers keep independent counters.  Callers that need a
    run-wide total should collect and sum the dictionaries returned by each
    worker.
    """
    _ensure_process_memory_cache_state()
    with _PROCESS_MEMORY_CACHE_LOCK:
        memory_total_hits, memory_total_misses = _memory_cache_totals_unlocked()
        memory_info = _PROCESS_MEMORY_CACHE.cache_info()
        memory_hits = max(
            0, memory_total_hits - _PROCESS_MEMORY_CACHE_STATS_BASE_HITS
        )
        memory_misses = max(
            0, memory_total_misses - _PROCESS_MEMORY_CACHE_STATS_BASE_MISSES
        )
        memory_lookups = memory_hits + memory_misses
    result: Dict[str, object] = {name: int(_OD_CACHE_STATS[name]) for name in _STAT_NAMES}
    result.update(
        {
            "cache_path": _OD_CACHE_PATH,
            "cache_enabled": _OD_CACHE_PATH is not None,
            "strict": _OD_CACHE_STRICT,
            "pid": os.getpid(),
            "connection_pid": getattr(_THREAD_STATE, "pid", None),
            "memory_enabled": _PROCESS_MEMORY_CACHE_ENABLED,
            "memory_max_entries": _PROCESS_MEMORY_CACHE_MAX_ENTRIES,
            "memory_size": int(memory_info.currsize),
            "memory_pid": _PROCESS_MEMORY_CACHE_PID,
            "memory_hits": memory_hits,
            "memory_misses": memory_misses,
            "memory_lookups": memory_lookups,
            "memory_hit_rate": (
                memory_hits / memory_lookups if memory_lookups else 0.0
            ),
            "memory_policy": "functools_lru",
            "memory_key_semantics": "exact_raw_directed_4_tuple_typed",
        }
    )
    if reset:
        reset_od_cache_stats()
    return result


def _warn_once(reason: str, message: str) -> None:
    if reason not in _WARNED_REASONS:
        logging.warning(message)
        _WARNED_REASONS.add(reason)


def set_speed_kmph(speed_kmph: float) -> None:
    """Update global travel speed used by the Manhattan fallback."""
    global SPEED_KMPH
    try:
        speed = float(speed_kmph)
    except (TypeError, ValueError):
        raise ValueError(f"invalid speed_kmph: {speed_kmph!r}")
    if speed <= 0:
        raise ValueError(f"speed_kmph must be > 0, got {speed}")
    if speed != SPEED_KMPH:
        SPEED_KMPH = speed
        # Non-strict misses cache Manhattan fallback values derived from speed.
        _clear_process_memory_cache()


def manhattan_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Approximate Manhattan distance from latitude/longitude in kilometres."""
    dlat_km = abs(lat2 - lat1) * KM_PER_DEG_LAT
    dlon_km = abs(lon2 - lon1) * KM_PER_DEG_LON
    return dlat_km + dlon_km


def _valid_cache_row(row: Tuple[object, object] | None) -> Tuple[float, float] | None:
    if row is None:
        return None
    try:
        dist_km = float(row[0])
        time_min = float(row[1])
    except (TypeError, ValueError):
        return None
    if not math.isfinite(dist_km) or not math.isfinite(time_min):
        return None
    if dist_km < 0.0 or time_min < 0.0:
        return None
    return dist_km, time_min


def _fallback_travel(
    lat1: float,
    lon1: float,
    lat2: float,
    lon2: float,
) -> Tuple[float, float]:
    _OD_CACHE_STATS["fallbacks"] += 1
    dist = manhattan_km(lat1, lon1, lat2, lon2)
    return dist, (dist / SPEED_KMPH) * 60.0


def _travel_uncached(
    lat1: float, lon1: float, lat2: float, lon2: float
) -> Tuple[float, float]:
    """Run the historical SQLite/canonical/legacy/fallback lookup path."""
    global CACHE_MISS_WARNING

    conn = _get_od_connection()
    if conn is None:
        _OD_CACHE_STATS["uncached_fallbacks"] += 1
        return _fallback_travel(lat1, lon1, lat2, lon2)

    canonical_key = make_route_key(lat1, lon1, lat2, lon2)
    try:
        row = conn.execute(
            "SELECT dist_km, time_min FROM od_cache WHERE route_key=?",
            (canonical_key,),
        ).fetchone()
        valid = _valid_cache_row(row)
        if valid is not None:
            _OD_CACHE_STATS["hits"] += 1
            _OD_CACHE_STATS["canonical_hits"] += 1
            return valid
        if row is not None:
            _OD_CACHE_STATS["invalid_rows"] += 1

        legacy_keys = tuple(_legacy_route_keys(lat1, lon1, lat2, lon2))
        if legacy_keys:
            placeholders = ",".join("?" for _ in legacy_keys)
            legacy_rows = conn.execute(
                "SELECT route_key, dist_km, time_min FROM od_cache "
                f"WHERE route_key IN ({placeholders})",
                legacy_keys,
            ).fetchall()
            rows_by_key = {str(item[0]): (item[1], item[2]) for item in legacy_rows}
            for key in legacy_keys:
                legacy_row = rows_by_key.get(key)
                valid = _valid_cache_row(legacy_row)
                if valid is not None:
                    _OD_CACHE_STATS["hits"] += 1
                    _OD_CACHE_STATS["legacy_key_hits"] += 1
                    return valid
                if legacy_row is not None:
                    _OD_CACHE_STATS["invalid_rows"] += 1
    except Exception as exc:
        _OD_CACHE_STATS["lookup_errors"] += 1
        if _OD_CACHE_STRICT:
            raise ODCacheError(
                f"OD cache lookup failed for {canonical_key}: {exc}"
            ) from exc
        _warn_once(
            "lookup_error",
            f"OD cache lookup failed for {canonical_key}: {exc}. "
            "Falling back to Manhattan travel; further lookup-error warnings suppressed.",
        )
        return _fallback_travel(lat1, lon1, lat2, lon2)

    _OD_CACHE_STATS["misses"] += 1
    if _OD_CACHE_STRICT:
        raise ODCacheMissError(f"OD cache miss or invalid route for {canonical_key}")
    if not CACHE_MISS_WARNING:
        _warn_once(
            "cache_miss",
            f"Cache miss for key {canonical_key}. Falling back to Manhattan distance; "
            "further miss warnings suppressed.",
        )
        CACHE_MISS_WARNING = True
    return _fallback_travel(lat1, lon1, lat2, lon2)


# Create the initial wrapper only after _travel_uncached exists.  Forked
# children replace it through the registered callback, so neither cached
# values nor the wrapper's internal lock/counters leak across process IDs.
_PROCESS_MEMORY_CACHE = _build_process_memory_cache(
    _PROCESS_MEMORY_CACHE_MAX_ENTRIES
)
if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_process_memory_cache_after_fork)


def travel(lat1: float, lon1: float, lat2: float, lon2: float) -> Tuple[float, float]:
    """Return directed travel ``(distance_km, time_min)``.

    The canonical key is tried first.  Historical compact-float key spellings
    are queried only after a canonical miss, keeping the common lookup path
    fast while allowing old region-centre rows to remain useful.
    """
    if not _PROCESS_MEMORY_CACHE_ENABLED:
        return _travel_uncached(lat1, lon1, lat2, lon2)
    # Keep the four positional values exactly as supplied.  In particular, do
    # not canonicalize/round here: legacy six-decimal rows and raw-coordinate
    # Manhattan fallback can differ inside one fixed-4dp canonical bucket.
    return _PROCESS_MEMORY_CACHE(lat1, lon1, lat2, lon2)
