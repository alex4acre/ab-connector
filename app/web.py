# SPDX-License-Identifier: MIT

"""Web UI and REST API, served behind the ctrlX reverse proxy at /ab-connect."""

import json
import mimetypes
import os
import re
import socketserver
import time
import traceback
import http.server
from urllib.parse import parse_qs, unquote, urlparse

from app import logix
from app.config import MIN_RATE_MS, DEFAULT_RATE_MS, ConfigError, expand, parent_via
from app.logix import LogixError, scope_prefix
from app.scanner import WorkerError, read_entries

BASE = "/ab-connect"
API = BASE + "/api/v1"
PREVIEW_ELEMENTS = 20

core = None          # set by main
www_dir = None       # set by main

ROUTES = [
    ("GET", r"/controllers", "get_controllers"),
    ("POST", r"/controllers", "add_controller"),
    ("PATCH", r"/controllers/([^/]+)", "update_controller"),
    ("DELETE", r"/controllers/([^/]+)", "remove_controller"),
    ("GET", r"/controllers/([^/]+)/browse", "browse"),
    ("POST", r"/controllers/([^/]+)/refresh", "refresh"),
    ("GET", r"/controllers/([^/]+)/preview", "preview"),
    ("GET", r"/discover", "discover"),
    ("GET", r"/tags", "get_tags"),
    ("POST", r"/tags", "add_tags"),
    ("PATCH", r"/tags", "update_tag"),
    ("POST", r"/tags/delete", "delete_tags"),
    ("POST", r"/admin/cfg/load", "cfg_load"),
    ("POST", r"/admin/cfg/save", "cfg_save"),
]

mimetypes.add_type("text/javascript", ".js")


class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def short(value):
    """Value for the UI: long arrays cut down, with the total length."""
    if isinstance(value, list) and len(value) > PREVIEW_ELEMENTS:
        return {"first": value[:PREVIEW_ELEMENTS], "length": len(value)}
    return value


class RequestHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # keep the journal for app messages

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_PATCH(self):
        self.dispatch("PATCH")

    def do_DELETE(self):
        self.dispatch("DELETE")

    def dispatch(self, method: str):
        url = urlparse(self.path)
        path = url.path
        self.query = parse_qs(url.query)
        try:
            if path.startswith(API):
                sub = path[len(API):].rstrip("/")
                matched = False
                for route_method, pattern, handler in ROUTES:
                    m = re.fullmatch(pattern, sub)
                    if m:
                        matched = True
                        if route_method == method:
                            return getattr(self, handler)(*[unquote(g) for g in m.groups()])
                raise HttpError(405 if matched else 404, "Method not allowed" if matched else "Not found")
            if method == "GET" and (path == BASE or path.startswith(BASE + "/")):
                return self.send_static(path[len(BASE):].lstrip("/") or "index.html")
            raise HttpError(404, "Not found")
        except HttpError as e:
            self.send_json(e.status, {"error": str(e)})
        except KeyError as e:
            self.send_json(404, {"error": str(e.args[0]) if e.args else "Not found"})
        except (ConfigError, LogixError, ValueError) as e:
            self.send_json(400, {"error": str(e)})
        except WorkerError as e:
            self.send_json(503, {"error": str(e)})
        except Exception as e:
            traceback.print_exc()
            self.send_json(500, {"error": str(e)})

    # --- helpers ---------------------------------------------------------------------------------

    def q(self, name, default=""):
        return (self.query.get(name) or [default])[0]

    def read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > 1024 * 1024:
            raise HttpError(413, "Request body too large")
        try:
            data = json.loads(self.rfile.read(length) or b"null")
        except ValueError as e:
            raise HttpError(400, f"Invalid JSON: {e}") from e
        if not isinstance(data, dict):
            raise HttpError(400, "A JSON object is expected")
        return data

    def send_bytes(self, status, body, content_type, cache=False):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=600" if cache else "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status, obj):
        self.send_bytes(status, json.dumps(obj).encode("utf-8"), "application/json")

    def send_static(self, rel):
        root = os.path.realpath(www_dir)
        full = os.path.realpath(os.path.join(root, rel))
        if not full.startswith(root + os.sep) or not os.path.isfile(full):
            raise HttpError(404, "Not found")
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/"):
            ctype += "; charset=utf-8"
        with open(full, "rb") as f:
            self.send_bytes(200, f.read(), ctype)

    # --- controllers -----------------------------------------------------------------------------

    def get_controllers(self):
        out = []
        for c in core.config.controllers():
            w = core.workers.get(c["name"])
            out.append({**c, "status": w.status() if w else None})
        self.send_json(200, out)

    def add_controller(self):
        d = self.read_json()
        core.config.add_controller(str(d.get("name", "")).strip(), str(d.get("address", "")).strip())
        core.apply()
        self.send_json(201, {"added": d.get("name")})

    def update_controller(self, name):
        core.config.update_controller(name, self.read_json())
        core.apply()
        self.send_json(200, {"updated": name})

    def remove_controller(self, name):
        core.config.remove_controller(name)
        core.apply()
        self.send_json(200, {"removed": name})

    def discover(self):
        """EtherNet/IP devices: broadcast on every network interface, or ask each address of ?range=."""
        self.send_json(200, logix.discover(self.q("range") or None))

    def browse(self, name):
        """One level of the tag tree. No scope: the scopes (controller, programs)."""
        scope, path = self.q("scope"), self.q("path")
        offset, limit = int(self.q("offset", "0")), min(int(self.q("limit", "100")), 500)
        w = core.worker(name)
        if not scope:
            programs = w.call(lambda s: s.program_names())
            items = [{"name": "Controller tags", "scope": "controller", "path": "", "kind": "scope", "has_children": True}]
            items += [{"name": p, "scope": f"program:{p}", "path": "", "kind": "scope", "has_children": True} for p in programs]
            return self.send_json(200, {"items": items, "total": len(items)})
        scope_prefix(scope)  # validates
        result = w.call(lambda s: s.children(scope, path, offset, limit), timeout=120)
        for item in result["items"]:
            item["scope"] = scope
        self.send_json(200, result)

    def refresh(self, name):
        """Upload the tag definitions again (after a program download)."""
        w = core.worker(name)

        def reload(s):
            scopes = list(s.scopes)
            s.scopes, s.programs = {}, None
            for sc in scopes:
                s.load_scope(sc)
            return scopes
        self.send_json(200, {"reloaded": w.call(reload, timeout=120)})

    def preview(self, name):
        """Read one tree node's value now (arrays: the first elements)."""
        scope, path = self.q("scope"), self.q("path")
        w = core.worker(name)

        def read(s):
            leaves = expand(s, scope, path)
            if len(leaves) > 50:
                return {"values": None, "note": f"{len(leaves)} values - expand to see them"}
            results = read_entries(s, [dict(l, scope=scope) for l in leaves])
            return {"values": [{"path": l["path"], "type": l["type"], "count": l["count"],
                                "value": v[:PREVIEW_ELEMENTS] if isinstance(v, list) else v, "error": err}
                               for l, (v, err) in zip(leaves, results)]}
        self.send_json(200, w.call(read))

    # --- tags ------------------------------------------------------------------------------------

    def get_tags(self):
        name = self.q("controller") or None
        now = time.monotonic()
        out = []
        for t in core.config.tags(name):
            w = core.workers.get(t["controller"])
            c = w.cached(t["node"]) if w else None
            out.append({**t, "value": short(c["value"]) if c else None, "error": c["error"] if c else None,
                        "age_ms": round((now - c["time"]) * 1000) if c and c["time"] else None})
        self.send_json(200, out)

    def add_tags(self):
        d = self.read_json()
        name, scope, path = d.get("controller"), d.get("scope"), d.get("path")
        mode = d.get("mode", "cyclic")
        rate = d.get("rate_ms", DEFAULT_RATE_MS)
        writable = bool(d.get("writable", False))
        core.config.controller(name)
        def prepare(s):
            leaves = expand(s, scope, path)
            # A single structure member the controller won't read by name (e.g. internal members
            # of predefined types): read it through its parent structure instead.
            single = [l for l in leaves if not l.get("via") and "." in l["path"]]
            if single:
                results = read_entries(s, [dict(l, scope=scope) for l in single])
                for leaf, (value, error) in zip(single, results):
                    if error is not None:
                        leaf["via"] = parent_via(leaf)
            return leaves
        leaves = core.worker(name).call(prepare, timeout=120)
        new = core.config.add_tags(name, scope, leaves, mode, rate, writable)
        core.apply()
        self.send_json(201, {"added": len(new), "skipped": len(leaves) - len(new), "tags": new})

    def update_tag(self):
        d = self.read_json()
        entry = core.config.update_tag(d.get("controller"), d.get("node"), d.get("changes") or {})
        core.apply()
        self.send_json(200, entry)

    def delete_tags(self):
        d = self.read_json()
        nodes = d.get("nodes") or []
        if not isinstance(nodes, list):
            raise HttpError(400, '"nodes" must be a list')
        core.config.remove_tags(d.get("controller"), nodes)
        core.apply()
        self.send_json(200, {"removed": len(nodes)})

    # --- loading / saving ctrlX configurations (registered in the package manifest) --------------

    def cfg_load(self):
        data = self.read_json()
        if data.get("phase") == "load":
            core.reload_if_changed()
        self.send_bytes(204, b"", "application/json")

    def cfg_save(self):
        self.send_bytes(204, b"", "application/json")


class UnixSocketServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def get_request(self):
        request, _ = super().get_request()
        return request, ("local", 0)


class TcpServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


__all__ = ["RequestHandler", "UnixSocketServer", "TcpServer", "MIN_RATE_MS"]
