# SPDX-License-Identifier: MIT

"""config.json in the app data: controllers and the selected tags.

Every selected tag is stored with its type, so the Data Layer nodes can be registered at startup
without contacting the PLC. Selecting a UDT (or an array of UDTs) stores one entry per value member;
an array of plain values is one entry (one Data Layer array node, read in one request).

{"version": 1,
 "controllers": [{"name": "Line1", "address": "192.168.1.21", "enabled": true}],
 "tags": [{"controller": "Line1", "scope": "controller", "path": "Station1.Pos", "node": "controller/Station1/Pos",
           "type": "REAL", "dl_type": "float32", "count": 6, "mode": "cyclic", "rate_ms": 500, "writable": false}]}

Logix tag paths: "Tag", "Tag.Member", "Arr[3]", "Grid[1,2]"; scope "controller" or "program:<name>".
"""

import json
import os
import re
import tempfile
import threading

from app.logix import ATOMIC_TYPES, dl_type, type_label, element_count, index_label, is_string, dimensions, scope_prefix

NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
NODE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_-]+$")
MODES = ("cyclic", "on-request")
MIN_RATE_MS = 50
DEFAULT_RATE_MS = 500
MAX_LEAVES = 2000


class ConfigError(ValueError):
    pass


def default_node(scope: str, path: str) -> str:
    """Data Layer path (below ab-connect/<controller>/) for a Logix tag path."""
    base = "controller" if scope == "controller" else "programs/" + scope[8:]
    parts = []
    for seg in path.split("."):
        name, _, idx = seg.partition("[")
        parts.append(name)
        if idx:
            parts.append(idx.rstrip("]").replace(",", "_").replace(" ", ""))
    return "/".join([base] + parts)


def plc_tag(entry: dict) -> str:
    """Full Logix tag name to read, e.g. "Program:Main.Station1.Pos{6}"."""
    tag = scope_prefix(entry["scope"]) + entry["path"]
    return f"{tag}{{{entry['count']}}}" if entry.get("count") else tag


def expand(session, scope: str, path: str) -> list:
    """Value entries (path, type, dl_type, count[, via]) for a selected tree node.

    When a structure (or an array of structures) is selected, every value in it gets
    "via": {"path": <selected path>, "count": <elements or None>, "keys": [...]}: the scanner reads
    the whole structure in one request and takes the value out of it by those keys. That is one
    request instead of one per member, and it also covers members the controller won't let you
    address by name (e.g. internal members of predefined types like MOTION_INSTRUCTION).
    """
    info, top, indexed = session.resolve(scope, path)
    if top and info.get("external_access") == "None":
        raise ConfigError(f"{path} has External Access 'None' in the PLC and can't be read")
    root_count = 0 if indexed else element_count(info, top)
    leaves = []

    def walk(p, info, top, indexed, keys):
        if len(leaves) > MAX_LEAVES:
            raise ConfigError(f"More than {MAX_LEAVES} values: select a smaller part of the structure")
        count = 0 if indexed else element_count(info, top)
        single = dl_type(info)
        leaf = None
        if count:
            if single:
                leaf = {"path": p, "type": type_label(info), "dl_type": single, "count": count}
            else:
                dims = dimensions(info, top)
                for i in range(count):
                    walk(f"{p}[{index_label(i, dims)}]", info, top, True, keys + [i])
                return
        elif single:
            leaf = {"path": p, "type": type_label(info), "dl_type": single, "count": None}
        else:
            dt = info.get("data_type")
            if isinstance(dt, dict) and not is_string(info):
                for m in dt.get("attributes", []):
                    walk(f"{p}.{m}", dt["internal_tags"][m], False, False, keys + [m])
                return
            raise ConfigError(f"{p}: type {info.get('data_type_name')} is not supported")
        if keys:
            leaf["via"] = {"path": path, "count": root_count or None, "keys": keys}
        leaves.append(leaf)

    walk(path, info, top, indexed, [])
    return leaves


def parent_via(leaf: dict):
    """For a single structure member "A.B.Member" (or "A.B.Member[2]"): read it through "A.B"."""
    path = leaf["path"]
    if "." not in path:
        return None
    parent, member = path.rsplit(".", 1)
    name, _, idx = member.partition("[")
    keys = [name] + ([int(idx.rstrip("]"))] if idx and "," not in idx else [])
    if idx and "," in idx:
        return None
    return {"path": parent, "count": None, "keys": keys}


def check_entry(e: dict):
    if e.get("mode") not in MODES:
        raise ConfigError(f"{e.get('node')}: mode must be one of {', '.join(MODES)}")
    rate = e.get("rate_ms")
    if not isinstance(rate, int) or isinstance(rate, bool) or rate < MIN_RATE_MS:
        raise ConfigError(f"{e.get('node')}: rate must be a whole number of milliseconds, at least {MIN_RATE_MS}")
    if not isinstance(e.get("writable"), bool):
        raise ConfigError(f"{e.get('node')}: writable must be true or false")
    node = e.get("node") or ""
    if not node or not all(NODE_SEGMENT_RE.match(s) for s in node.split("/")):
        raise ConfigError(f"Data Layer path {node!r}: use letters, digits, _ and -, separated by /")
    if e.get("dl_type") not in set(ATOMIC_TYPES.values()) | {"string"}:
        raise ConfigError(f"{node}: unsupported type")


class Config:

    def __init__(self, path: str):
        self.path = path
        self.lock = threading.RLock()
        self.data = {"version": 1, "controllers": [], "tags": []}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                self.data = json.load(f)
            self.data.setdefault("controllers", [])
            self.data.setdefault("tags", [])

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(suffix=".tmp", dir=os.path.dirname(self.path))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=1)
                f.write("\n")
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    # --- controllers ------------------------------------------------------------------------------

    def controllers(self) -> list:
        with self.lock:
            return [dict(c) for c in self.data["controllers"]]

    def controller(self, name: str) -> dict:
        for c in self.data["controllers"]:
            if c["name"] == name:
                return c
        raise KeyError(f"Controller {name} not found")

    def add_controller(self, name: str, address: str):
        with self.lock:
            if not NAME_RE.match(name or ""):
                raise ConfigError("Controller name: start with a letter, then letters, digits, _ or - (max. 64)")
            if not address or not re.match(r"^[A-Za-z0-9.:/_-]+$", address):
                raise ConfigError("Address: IP address, optionally with a route, e.g. 192.168.1.21 or 192.168.1.21/0")
            if any(c["name"] == name for c in self.data["controllers"]):
                raise ConfigError(f"Controller {name} already exists")
            self.data["controllers"].append({"name": name, "address": address, "enabled": True})
            self.save()

    def update_controller(self, name: str, changes: dict):
        with self.lock:
            c = self.controller(name)
            if "address" in changes:
                if not re.match(r"^[A-Za-z0-9.:/_-]+$", changes["address"] or ""):
                    raise ConfigError("Invalid address")
                c["address"] = changes["address"]
            if "enabled" in changes:
                c["enabled"] = bool(changes["enabled"])
            self.save()

    def remove_controller(self, name: str):
        with self.lock:
            self.controller(name)
            self.data["controllers"] = [c for c in self.data["controllers"] if c["name"] != name]
            self.data["tags"] = [t for t in self.data["tags"] if t["controller"] != name]
            self.save()

    # --- tags ---------------------------------------------------------------------------------------

    def tags(self, controller: str = None) -> list:
        with self.lock:
            return [dict(t) for t in self.data["tags"] if controller is None or t["controller"] == controller]

    def add_tags(self, controller: str, scope: str, leaves: list, mode: str, rate_ms: int, writable: bool) -> list:
        """Add value entries; existing ones (same Data Layer path) are left as they are. Returns the new entries."""
        with self.lock:
            self.controller(controller)
            existing = {t["node"] for t in self.data["tags"] if t["controller"] == controller}
            new = []
            for leaf in leaves:
                e = {"controller": controller, "scope": scope, "path": leaf["path"],
                     "node": default_node(scope, leaf["path"]), "type": leaf["type"], "dl_type": leaf["dl_type"],
                     "count": leaf["count"], "mode": mode, "rate_ms": rate_ms, "writable": writable}
                if leaf.get("via"):
                    e["via"] = leaf["via"]
                check_entry(e)
                if e["node"] not in existing:
                    new.append(e)
                    existing.add(e["node"])
            self.data["tags"].extend(new)
            self.save()
            return new

    def update_tag(self, controller: str, node: str, changes: dict) -> dict:
        with self.lock:
            entry = self._find(controller, node)
            updated = dict(entry)
            for k in ("mode", "rate_ms", "writable", "node"):
                if k in changes:
                    updated[k] = changes[k]
            check_entry(updated)
            if updated["node"] != node and any(t["controller"] == controller and t["node"] == updated["node"]
                                               for t in self.data["tags"]):
                raise ConfigError(f"Data Layer path {updated['node']} is already used")
            entry.clear()
            entry.update(updated)
            self.save()
            return dict(entry)

    def remove_tags(self, controller: str, nodes: list):
        with self.lock:
            gone = set(nodes)
            self.data["tags"] = [t for t in self.data["tags"] if not (t["controller"] == controller and t["node"] in gone)]
            self.save()

    def _find(self, controller: str, node: str) -> dict:
        for t in self.data["tags"]:
            if t["controller"] == controller and t["node"] == node:
                return t
        raise KeyError(f"No tag {node} on {controller}")
