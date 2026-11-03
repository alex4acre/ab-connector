# SPDX-License-Identifier: MIT

"""One worker thread per PLC: the only user of that PLC's Session (pycomm3 isn't thread-safe).

- Cyclic tags are grouped by rate; each group is read in one batched request when due. If a read
  takes longer than the group's period, the next one starts right away and the overrun is counted
  (missed cycles are skipped, not queued up).
- On-request reads arriving together are read in one batch; a value younger than the tag's rate is
  served from the cache without contacting the PLC.
- Writes, tree browsing and value previews are queued jobs on the same thread.
- After a connection loss the worker reconnects with back-off (1 s doubling to 30 s).
"""

import queue
import threading
import time
import traceback

from app.config import plc_tag
from app.logix import LogixError, Session, scope_prefix

RECONNECT_MIN_S = 1.0
RECONNECT_MAX_S = 30.0


class WorkerError(Exception):
    pass


def via_tag(entry: dict) -> str:
    """The structure (or array of structures) an entry is read through, e.g. "Program:Main.Stations{4}"."""
    v = entry["via"]
    tag = scope_prefix(entry["scope"]) + v["path"]
    return f"{tag}{{{v['count']}}}" if v.get("count") else tag


def _extract(value, entry):
    for k in entry["via"]["keys"]:
        value = value[k]
    if entry.get("count"):
        if not isinstance(value, list) or len(value) != entry["count"]:
            raise ValueError("unexpected array shape")
        value = list(value)
    elif isinstance(value, (dict, list)):
        raise ValueError("not a single value")
    return value


def read_entries(session, entries: list) -> list:
    """[(value, error)] for entries, in one batched request where possible.

    Entries with "via" are taken out of one read of their structure; if that read fails, or the
    structure doesn't contain the value in the expected form, they are read directly instead.
    """
    results = [None] * len(entries)
    direct = [i for i, e in enumerate(entries) if not e.get("via")]
    structs = list(dict.fromkeys(via_tag(e) for e in entries if e.get("via")))
    replies = session.read([plc_tag(entries[i]) for i in direct] + structs)
    for i, r in zip(direct, replies):
        results[i] = r
    struct_values = dict(zip(structs, replies[len(direct):]))
    fallback = []
    for i, e in enumerate(entries):
        if not e.get("via"):
            continue
        value, error = struct_values[via_tag(e)]
        if error is None and value is not None:
            try:
                results[i] = (_extract(value, e), None)
                continue
            except (KeyError, IndexError, TypeError, ValueError):
                pass
        fallback.append(i)
    if fallback:
        for i, r in zip(fallback, session.read([plc_tag(entries[i]) for i in fallback])):
            results[i] = r
    return results


class _Job:
    def __init__(self, kind, **kw):
        self.kind = kind
        self.__dict__.update(kw)


class ControllerWorker(threading.Thread):

    def __init__(self, name: str, address: str, driver_factory, log=print):
        super().__init__(name=f"plc-{name}", daemon=True)
        self.plc_name = name
        self.address = address
        self.session = Session(driver_factory, address)
        self.log = log
        self.jobs = queue.Queue()
        self.stopping = False
        self.entries = {}          # node -> entry
        self.groups = {}           # rate_ms -> {"nodes": [...], "due": t, "last_ms", "overruns", "reads", "last_read"}
        self.cache = {}            # node -> {"value", "error", "time"}
        self.cache_lock = threading.Lock()
        self.connected = False
        self.last_error = None
        self.retry_at = 0.0
        self.retry_s = RECONNECT_MIN_S
        self.on_request_reads = 0

    # --- API for other threads ----------------------------------------------------------------------

    def stop(self):
        self.stopping = True
        self.jobs.put(_Job("wake"))

    def set_entries(self, entries: list):
        self.jobs.put(_Job("entries", entries=[dict(e) for e in entries]))

    def call(self, func, timeout: float = 30.0):
        """Run func(session) on the worker thread and return its result (browse, preview, ...)."""
        done = threading.Event()
        box = {}
        self.jobs.put(_Job("call", func=func, done=done, box=box))
        if not done.wait(timeout):
            raise WorkerError("The PLC didn't answer in time")
        if "error" in box:
            raise box["error"]
        return box["result"]

    def cached(self, node: str):
        with self.cache_lock:
            return self.cache.get(node)

    def read_async(self, node: str, callback, max_age_ms: float = 0):
        """callback(value, error), possibly from the worker thread. Serves fresh cached values directly."""
        c = self.cached(node)
        if c and c["error"] is None and max_age_ms and (time.monotonic() - c["time"]) * 1000 < max_age_ms:
            callback(c["value"], None)
            return
        self.jobs.put(_Job("read", node=node, callback=callback))

    def write_async(self, node: str, value, callback):
        self.jobs.put(_Job("write", node=node, value=value, callback=callback))

    def status(self) -> dict:
        groups = [{"rate_ms": r, "tags": len(g["nodes"]), "last_ms": g["last_ms"], "overruns": g["overruns"],
                   "reads": g["reads"]} for r, g in sorted(self.groups.items())]
        return {"name": self.plc_name, "address": self.address, "connected": self.connected,
                "error": self.last_error, "info": self.session.info() if self.connected else {},
                "groups": groups, "on_request_reads": self.on_request_reads,
                "tags": len(self.entries)}

    # --- worker thread ----------------------------------------------------------------------------

    def run(self):
        while not self.stopping:
            try:
                self._ensure_connected()
                job = self._next_job()
                if job is not None:
                    self._handle(job)
                self._read_due_groups()
            except Exception as e:     # never let the worker die
                traceback.print_exc()
                self._lost(f"Unexpected error: {e}")
        self.session.close()

    def _next_job(self):
        now = time.monotonic()
        if self.connected and self.groups:
            timeout = max(0.0, min(g["due"] for g in self.groups.values()) - now)
        elif not self.connected:
            timeout = max(0.0, min(self.retry_at - now, 1.0))
        else:
            timeout = 1.0
        try:
            return self.jobs.get(timeout=timeout)
        except queue.Empty:
            return None

    def _ensure_connected(self):
        if self.connected or time.monotonic() < self.retry_at:
            return
        try:
            self.session.connect()
            self._load_scopes()
            self.connected = True
            self.last_error = None
            self.retry_s = RECONNECT_MIN_S
            self.log(f"{self.plc_name}: connected to {self.address}")
            now = time.monotonic()
            for g in self.groups.values():
                g["due"] = now
        except Exception as e:
            self._lost(str(e) or e.__class__.__name__)

    def _load_scopes(self):
        for scope in sorted({e["scope"] for e in self.entries.values()}):
            self.session.load_scope(scope)

    def _lost(self, error: str):
        if self.connected or self.last_error != error:
            self.log(f"{self.plc_name}: {error}")
        self.connected = False
        self.last_error = error
        self.session.close()
        self.retry_at = time.monotonic() + self.retry_s
        self.retry_s = min(self.retry_s * 2, RECONNECT_MAX_S)
        with self.cache_lock:
            for node in self.entries:
                c = self.cache.get(node)
                self.cache[node] = {"value": c["value"] if c else None, "error": "PLC not connected",
                                    "time": c["time"] if c else 0.0}
        # Fail queued requests now rather than leaving them waiting
        while True:
            try:
                job = self.jobs.get_nowait()
            except queue.Empty:
                break
            if job.kind == "entries":
                self._set_entries(job.entries)
            elif job.kind == "call":
                job.box["error"] = WorkerError(f"PLC not connected: {error}")
                job.done.set()
            elif job.kind in ("read", "write"):
                job.callback(None, "PLC not connected") if job.kind == "read" else job.callback(f"PLC not connected")

    def _handle(self, job):
        if job.kind in ("read", "write") and not self.connected:
            if job.kind == "read":
                job.callback(None, "PLC not connected")
            else:
                job.callback("PLC not connected")
            return
        if job.kind == "entries":
            self._set_entries(job.entries)
            if self.connected:
                self._load_scopes()
        elif job.kind == "call":
            if not self.connected:
                job.box["error"] = WorkerError(f"PLC not connected: {self.last_error}" if self.last_error
                                               else "Connecting to the PLC…")
                job.done.set()
                return
            try:
                job.box["result"] = job.func(self.session)
            except (LogixError, ValueError, KeyError) as e:
                job.box["error"] = e
            except Exception as e:
                job.box["error"] = WorkerError(str(e))
                self._lost(str(e))
            job.done.set()
        elif job.kind == "read":
            # take every read waiting now, and serve them with one batched request
            reads = [job]
            others = []
            while True:
                try:
                    j = self.jobs.get_nowait()
                except queue.Empty:
                    break
                (reads if j.kind == "read" else others).append(j)
            self._serve_reads(reads)
            for j in others:
                self._handle(j)
        elif job.kind == "write":
            self._write(job)

    def _set_entries(self, entries):
        self.entries = {e["node"]: e for e in entries}
        with self.cache_lock:
            self.cache = {n: c for n, c in self.cache.items() if n in self.entries}
        old = self.groups
        groups = {}
        now = time.monotonic()
        for e in entries:
            if e["mode"] == "cyclic":
                g = groups.setdefault(e["rate_ms"], {"nodes": [], "due": now, "last_ms": None, "overruns": 0, "reads": 0})
                g["nodes"].append(e["node"])
        for r, g in groups.items():
            if r in old:
                g.update({k: old[r][k] for k in ("last_ms", "overruns", "reads")})
        self.groups = groups

    def _read_nodes(self, nodes: list) -> dict:
        """Read entries in one batch -> {node: (value, error)}. Raises on connection problems."""
        entries = [self.entries[n] for n in nodes if n in self.entries]
        results = read_entries(self.session, entries)
        out = {}
        now = time.monotonic()
        with self.cache_lock:
            for e, (value, error) in zip(entries, results):
                if error is None and value is None:
                    error = "No value"
                self.cache[e["node"]] = {"value": value if error is None else (self.cache.get(e["node"]) or {}).get("value"),
                                         "error": error, "time": now}
                out[e["node"]] = (value, error)
        return out

    def _read_due_groups(self):
        if not self.connected:
            return
        for rate, g in sorted(self.groups.items()):
            now = time.monotonic()
            if g["due"] > now:
                continue
            start = now
            try:
                self._read_nodes(g["nodes"])
            except Exception as e:
                self._lost(f"Read failed: {e}")
                return
            end = time.monotonic()
            g["last_ms"] = round((end - start) * 1000, 1)
            g["reads"] += 1
            g["due"] += rate / 1000.0
            if g["due"] < end:              # took longer than the period: skip missed cycles
                g["overruns"] += 1
                g["due"] = end

    def _serve_reads(self, jobs):
        self.on_request_reads += 1
        nodes = list(dict.fromkeys(j.node for j in jobs))
        unknown = [n for n in nodes if n not in self.entries]
        try:
            results = self._read_nodes([n for n in nodes if n in self.entries])
        except Exception as e:
            self._lost(f"Read failed: {e}")
            for j in jobs:
                j.callback(None, "PLC not connected")
            return
        for j in jobs:
            if j.node in unknown:
                j.callback(None, "Tag is not configured")
            else:
                j.callback(*results[j.node])

    def _write(self, job):
        entry = self.entries.get(job.node)
        if entry is None:
            job.callback("Tag is not configured")
            return
        if not entry.get("writable"):
            job.callback("Tag is read-only (enable writing in ab-connect)")
            return
        try:
            error = self.session.write([(plc_tag(entry), job.value)])[0]
        except Exception as e:
            self._lost(f"Write failed: {e}")
            job.callback("PLC not connected")
            return
        if error is None:
            with self.cache_lock:
                self.cache[job.node] = {"value": job.value, "error": None, "time": time.monotonic()}
        job.callback(error)
