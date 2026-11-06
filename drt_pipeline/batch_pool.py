"""One process pool, fair inter-job phases, and reference-parallel budget time.

Only the broker owns solver processes. Parents submit a complete dependency
phase, wait without occupying a worker, then apply results in their own process.
The authenticated Unix socket is private to this local batch user.
"""
from __future__ import annotations

import collections
import concurrent.futures as cf
import json
import multiprocessing as mp
from multiprocessing.connection import Client, Listener
import os
from pathlib import Path
import queue
import random
import threading
import time
import traceback
import uuid

BUDGET_MODE = "reference_parallel_v1"
BAD_CACHE = ("load_errors", "misses", "invalid_rows", "lookup_errors", "fallbacks", "uncached_fallbacks")


class ReferenceClock:
    """Serial monotonic time plus critical-path compute and parent phase CPU.

    Pool startup/IPC/queue waiting is operational overhead, recorded separately.
    Each phase has <= reference_workers tasks, so its critical path is max(task
    elapsed). Dependent rescue waves accumulate separately. Parent preparation
    and application outside phases remain measured monotonic elapsed time.
    """
    def __init__(self):
        self.start = time.monotonic()
        self.adjustment = 0.0
        self.phases = []

    def elapsed(self):
        return time.monotonic() - self.start + self.adjustment

    def account(self, *, wall, parent_cpu, tasks, identity):
        compute = max((t["ended"] - t["started"] for t in tasks), default=0.0)
        charge = compute + parent_cpu
        self.adjustment += charge - wall
        row = {**identity, "actual_wall_sec": wall, "reference_sec": charge,
               "critical_path_compute_sec": compute, "parent_cpu_sec": parent_cpu,
               "queue_sum_sec": sum(t["started"] - t["submitted"] for t in tasks),
               "task_count": len(tasks)}
        self.phases.append(row)
        return row


def worker_init(cpus):
    os.sched_setaffinity(0, cpus)
    for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[name] = "1"


def execute_task(task, cache_config):
    """One task at a time per process; solver resets all mutable objective state."""
    from . import travel
    started = time.monotonic()
    cpu_start = time.process_time()
    record = {k: task[k] for k in ("job_id", "phase_id", "task_id", "hour", "stage", "group", "replica", "seed", "submitted")}
    record.update(started=started, pid=os.getpid(), affinity=sorted(os.sched_getaffinity(0)))
    try:
        path, strict, memory, capacity = cache_config
        current = travel.get_od_cache_stats()
        if path and (current["cache_path"] != str(Path(path).resolve()) or not current["strict"]):
            travel.load_od_cache(path, strict=bool(strict))
        travel.configure_process_memory_cache(memory, max_entries=capacity)
        travel.reset_od_cache_stats()
        random.seed(task["seed"])
        if task.get("probe"):
            probe = task["probe"]
            if probe.get("die"):
                os._exit(17)
            if probe.get("lookup"):
                travel.travel(*probe["lookup"])
            time.sleep(probe.get("sleep", 0))
            if probe.get("fail"):
                raise RuntimeError("injected replica failure")
            result = {"random": random.random(), "pid": os.getpid()}
        else:
            from .alns_sa import solve_od_alns_sa
            result = solve_od_alns_sa(*task["args"], **task["kwargs"])
        stats = travel.get_od_cache_stats()
        if any(stats[k] for k in BAD_CACHE):
            raise RuntimeError(f"strict OD diagnostics: {stats}")
        record.update(ok=True, cache_stats=stats)
        return {"result": result, "record": record}
    except Exception:
        record.update(ok=False, error=traceback.format_exc(), cache_stats=travel.get_od_cache_stats())
        return {"error": record["error"], "record": record}
    finally:
        record.update(ended=time.monotonic(), cpu_sec=time.process_time() - cpu_start)


class SharedPhase:
    def __init__(self, factory, identity, cache_config):
        self.factory, self.identity, self.cache_config = factory, identity, cache_config
        self.phase_id = uuid.uuid4().hex
        self.tasks, self.futures, self.records = [], [], []
        self.thread = None

    def __enter__(self):
        self.wall_start, self.cpu_start = time.monotonic(), time.process_time()
        from .batch_common import atomic_json
        atomic_json(self.factory.journal.parent / "stage.json", {
            "stage": "WAIT_LOCAL" if self.identity["stage"] == "local" else "WAIT_RESCUE",
            "hour": self.identity["hour"], "phase_id": self.phase_id})
        return self

    def submit(self, fn, *args, **kwargs):
        if fn.__name__ != "solve_od_alns_sa":
            raise ValueError("shared pool accepts the existing solver only")
        index = len(self.tasks)
        self.tasks.append({**self.identity, "phase_id": self.phase_id,
            "task_id": f"{self.phase_id}:{index}", "args": args, "kwargs": kwargs,
            "hour": args[0], "group": [str(x.value) for x in args[1]],
            "replica": kwargs["artifact_tag"], "seed": args[5],
            "submitted": time.monotonic()})
        future = cf.Future()
        self.futures.append(future)
        return future

    def seal(self):
        if self.thread:
            raise RuntimeError("phase already submitted")
        def receive():
            try:
                with Client(self.factory.socket, family="AF_UNIX", authkey=self.factory.authkey) as conn:
                    conn.send({"tasks": self.tasks, "cache_config": self.cache_config,
                               "status_path": str(self.factory.journal.parent / "stage.json")})
                    response = conn.recv()
                if "fatal" in response:
                    raise RuntimeError(response["fatal"])
                if len(response["results"]) != len(self.futures):
                    raise RuntimeError("incomplete shared phase")
                for future, outcome in zip(self.futures, response["results"]):
                    self.records.append(outcome["record"])
                    if "error" in outcome:
                        future.set_exception(RuntimeError(outcome["error"]))
                    else:
                        future.set_result(outcome["result"])
            except BaseException as exc:
                for future in self.futures:
                    if not future.done():
                        future.set_exception(RuntimeError(f"broker unavailable: {exc}"))
        self.thread = threading.Thread(target=receive, daemon=True)
        self.thread.start()

    def __exit__(self, typ, value, tb):
        if self.thread:
            self.thread.join()
        self.timing = self.factory.clock.account(
            wall=time.monotonic() - self.wall_start,
            parent_cpu=time.process_time() - self.cpu_start,
            tasks=self.records, identity={**self.identity, "phase_id": self.phase_id})
        with self.factory.journal.open("a") as f:
            f.write(json.dumps({**self.timing, "tasks": self.records}) + "\n")
        if typ is None and (len(self.records) != len(self.tasks) or any(not r["ok"] for r in self.records)):
            raise RuntimeError(f"incomplete/failed replica phase {self.phase_id}")


class SharedExecutorFactory:
    def __init__(self, socket, authkey, job_id, journal):
        self.socket, self.authkey, self.job_id = socket, authkey, job_id
        self.journal = Path(journal)
        self.clock = ReferenceClock()

    def __call__(self, *, stage, hour, initargs, **unused):
        return SharedPhase(self, {"job_id": self.job_id, "stage": stage, "hour": hour}, initargs)


def serve_pool(socket, authkey, cpus, workers, event_file, stop_event):
    """Bound inflight work to pool size; round-robin jobs with rescue weight 2.

    No unbounded ProcessPoolExecutor backlog: fair selection occurs on each
    freed slot. A rescue job gets two turns per round, local jobs one.
    """
    inbox = queue.Queue()
    listener = Listener(socket, family="AF_UNIX", authkey=authkey)
    os.chmod(socket, 0o600)
    accepting = True
    def handle(conn):
        try:
            message = conn.recv()
            message["done"] = threading.Event()
            inbox.put(message)
            message["done"].wait()
            conn.send(message["response"])
        except (EOFError, BrokenPipeError, OSError):
            pass
        finally:
            conn.close()
    def accept():
        while accepting:
            try:
                conn = listener.accept()
                threading.Thread(target=handle, args=(conn,), daemon=True).start()
            except (OSError, EOFError):
                return
    threading.Thread(target=accept, daemon=True).start()
    pending = collections.deque()
    phases, running = {}, {}
    pool = cf.ProcessPoolExecutor(max_workers=workers, mp_context=mp.get_context("spawn"),
                                  initializer=worker_init, initargs=(cpus,))
    events = Path(event_file).open("a", buffering=1)
    fatal = None
    try:
        while not stop_event.is_set():
            while True:
                try:
                    message = inbox.get_nowait()
                except queue.Empty:
                    break
                tasks = message["tasks"]
                if not tasks:
                    message["response"] = {"results": []}; message["done"].set(); continue
                pid = tasks[0]["phase_id"]
                message.update(remaining=collections.deque(enumerate(tasks)), results=[None]*len(tasks), count=0)
                phases[pid] = message
                pending.append(pid)
                events.write(json.dumps({"event": "phase_submitted", "phase_id": pid,
                    "job_id": tasks[0]["job_id"], "hour": tasks[0]["hour"],
                    "stage": tasks[0]["stage"], "planned": len(tasks), "at": time.monotonic()})+"\n")
            while pending and len(running) < workers:
                pid = pending.popleft()
                phase = phases[pid]
                weight = 1 if phase["tasks"][0]["stage"] == "local" else 2
                for _ in range(weight):
                    if not phase["remaining"] or len(running) >= workers:
                        break
                    index, task = phase["remaining"].popleft()
                    if not phase.get("dispatched"):
                        phase["dispatched"] = True
                        if phase.get("status_path"):
                            from .batch_common import atomic_json
                            atomic_json(phase["status_path"], {"stage": "LOCAL" if task["stage"] == "local" else "RESCUE",
                                "hour": task["hour"], "phase_id": pid, "dispatched_monotonic": time.monotonic()})
                    f = pool.submit(execute_task, task, phase["cache_config"])
                    running[f] = (pid, index)
                if phase["remaining"]:
                    pending.append(pid)
            if running:
                completed, _ = cf.wait(running, timeout=0.1, return_when=cf.FIRST_COMPLETED)
                for f in completed:
                    pid, index = running.pop(f)
                    outcome = f.result()  # BrokenProcessPool invalidates all in-flight jobs.
                    phase = phases[pid]
                    phase["results"][index] = outcome
                    phase["count"] += 1
                    events.write(json.dumps({"event": "task_finished", **outcome["record"]})+"\n")
                    if phase["count"] == len(phase["tasks"]):
                        phase["response"] = {"results": phase["results"]}
                        phase["done"].set()
                        del phases[pid]
            else:
                stop_event.wait(0.05)
    except BaseException:
        fatal = traceback.format_exc()
        events.write(json.dumps({"event": "broker_fatal", "error": fatal})+"\n")
        raise
    finally:
        accepting = False
        for phase in phases.values():
            phase["response"] = {"fatal": fatal or "batch stopped"}
            phase["done"].set()
        for proc in list(pool._processes.values()):
            if proc.is_alive():
                proc.terminate()
        pool.shutdown(wait=True, cancel_futures=True)
        events.close()
        listener.close()
        Path(socket).unlink(missing_ok=True)
