# SPDX-License-Identifier: MIT

"""Keeps the PLC workers and the Data Layer nodes in line with config.json."""

import os
import threading

from app.config import Config
from app.scanner import ControllerWorker


class Core:

    def __init__(self, config_path: str, driver_factory, registry=None, log=print):
        self.config_path = config_path
        self.config = Config(config_path)
        self.driver_factory = driver_factory
        self.registry = registry          # NodeRegistry, or None without a Data Layer (development)
        self.log = log
        self.workers = {}
        self.lock = threading.RLock()
        self.file_id = self._file_id()

    def _file_id(self):
        try:
            st = os.stat(self.config_path)
            return st.st_ino, st.st_mtime_ns, st.st_size
        except FileNotFoundError:
            return None

    def apply(self):
        """Start/stop/restart workers for the configured controllers and (re)register Data Layer nodes."""
        with self.lock:
            wanted = {c["name"]: c for c in self.config.controllers() if c.get("enabled", True)}
            for name in list(self.workers):
                w = self.workers[name]
                if name not in wanted or wanted[name]["address"] != w.address:
                    w.stop()
                    del self.workers[name]
            for name, c in wanted.items():
                if name not in self.workers:
                    w = ControllerWorker(name, c["address"], self.driver_factory, log=self.log)
                    self.workers[name] = w
                    w.start()
            for name, w in self.workers.items():
                w.set_entries(self.config.tags(name))
            if self.registry is not None:
                self.registry.sync(self.workers, self.config.tags())
            self.file_id = self._file_id()

    def reload_if_changed(self) -> bool:
        """Pick up a config.json replaced from outside (Manage app data, loading a configuration)."""
        with self.lock:
            if self._file_id() == self.file_id:
                return False
            self.log("config.json changed, reloading")
            self.config = Config(self.config_path)
            self.apply()
            return True

    def stop(self):
        with self.lock:
            if self.registry is not None:
                self.registry.clear()
            for w in self.workers.values():
                w.stop()
            for w in self.workers.values():
                w.join(5)
            self.workers = {}

    def worker(self, name: str) -> ControllerWorker:
        w = self.workers.get(name)
        if w is None:
            raise KeyError(f"Controller {name} is not running (unknown or disabled)")
        return w
