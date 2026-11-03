# SPDX-License-Identifier: MIT

"""Config expansion and the PLC worker against the simulated Logix controller."""

import os
import shutil
import tempfile
import threading
import time
import unittest

from app import fake
from app.config import Config, ConfigError, default_node, expand, plc_tag
from app.fake import FakeLogixDriver
from app.logix import Session
from app.scanner import ControllerWorker, WorkerError


class Recorder:
    def __init__(self):
        self.done = threading.Event()
        self.args = None

    def __call__(self, *args):
        self.args = args
        self.done.set()

    def wait(self, timeout=5):
        assert self.done.wait(timeout), "no callback"
        return self.args


class ExpandTest(unittest.TestCase):

    def setUp(self):
        FakeLogixDriver.reset()
        self.s = Session(FakeLogixDriver, "10.0.0.1")
        self.s.connect()

    def test_value_array_struct(self):
        self.assertEqual(expand(self.s, "controller", "rSpeed"),
                         [{"path": "rSpeed", "type": "REAL", "dl_type": "float32", "count": None}])
        self.assertEqual(expand(self.s, "controller", "arTemps"),
                         [{"path": "arTemps", "type": "REAL", "dl_type": "float32", "count": 10}])
        leaves = expand(self.s, "controller", "Station1")
        self.assertEqual([l["path"] for l in leaves],
                         ["Station1.Name", "Station1.Pos", "Station1.Status", "Station1.Count", "Station1.Axis.ActPos",
                          "Station1.Axis.ActVel", "Station1.Axis.Enabled", "Station1.Axis.Fault", "Station1.Axis.StatusBits"])
        self.assertEqual(leaves[1]["count"], 6)
        self.assertEqual(leaves[0]["dl_type"], "string")
        self.assertEqual(len(expand(self.s, "controller", "Stations")), 4 * 9)
        self.assertEqual(expand(self.s, "controller", "Stations[2].Pos[1]")[0]["count"], None)
        self.assertEqual(expand(self.s, "controller", "aiGrid")[0]["count"], 12)

    def test_bool_array_reported_as_dword(self):
        item = [i for i in self.s.children("controller", "")["items"] if i["name"] == "abFlags"][0]
        self.assertEqual((item["type"], item["dl_type"], item["elements"]), ("BOOL", "bool8", 32))
        self.assertEqual(expand(self.s, "controller", "abFlags"),
                         [{"path": "abFlags", "type": "BOOL", "dl_type": "bool8", "count": 32}])
        self.assertEqual(expand(self.s, "controller", "abFlags[5]"),
                         [{"path": "abFlags[5]", "type": "BOOL", "dl_type": "bool8", "count": None}])
        self.s.write([("abFlags[5]", True)])
        values = self.s.read(["abFlags{32}", "abFlags[5]"])
        self.assertEqual((values[0][0][5], sum(values[0][0]), values[1][0]), (True, 1, True))

    def test_node_names_and_plc_tags(self):
        self.assertEqual(default_node("controller", "Stations[2].Pos"), "controller/Stations/2/Pos")
        self.assertEqual(default_node("controller", "aiGrid[1,2]"), "controller/aiGrid/1_2")
        self.assertEqual(default_node("program:MainProgram", "Conveyor.ActPos"), "programs/MainProgram/Conveyor/ActPos")
        e = {"scope": "program:MainProgram", "path": "arBig", "count": 2000}
        self.assertEqual(plc_tag(e), "Program:MainProgram.arBig{2000}")

    def test_config_add_update_remove(self):
        d = tempfile.mkdtemp()
        try:
            c = Config(os.path.join(d, "cfg", "config.json"))
            c.add_controller("Line1", "10.0.0.1")
            with self.assertRaises(ConfigError):
                c.add_controller("Line1", "10.0.0.2")
            new = c.add_tags("Line1", "controller", expand(self.s, "controller", "Station1"), "cyclic", 500, False)
            self.assertEqual(len(new), 9)
            self.assertEqual(c.add_tags("Line1", "controller", expand(self.s, "controller", "Station1.Pos"), "cyclic", 500, False), [])
            with self.assertRaises(ConfigError):
                c.update_tag("Line1", "controller/Station1/Pos", {"rate_ms": 10})
            c.update_tag("Line1", "controller/Station1/Pos", {"mode": "on-request", "writable": True})
            again = Config(c.path)
            pos = [t for t in again.tags("Line1") if t["node"] == "controller/Station1/Pos"][0]
            self.assertEqual((pos["mode"], pos["writable"], pos["count"]), ("on-request", True, 6))
            again.remove_controller("Line1")
            self.assertEqual(again.tags(), [])
        finally:
            shutil.rmtree(d)


class WorkerTest(unittest.TestCase):

    def setUp(self):
        FakeLogixDriver.reset()
        probe = Session(FakeLogixDriver, "10.0.0.2")
        probe.connect()
        self.entries = []
        for path, mode, rate, writable in (("rSpeed", "cyclic", 100, False), ("arTemps", "cyclic", 100, False),
                                           ("Station1.Name", "cyclic", 300, False), ("Station1.Status", "on-request", 200, True),
                                           ("rReadOnly", "on-request", 200, True)):
            for leaf in expand(probe, "controller", path):
                self.entries.append({"controller": "L", "scope": "controller", "path": leaf["path"],
                                     "node": default_node("controller", leaf["path"]), "type": leaf["type"],
                                     "dl_type": leaf["dl_type"], "count": leaf["count"], "mode": mode,
                                     "rate_ms": rate, "writable": writable})
        self.w = ControllerWorker("L", "10.0.0.2", FakeLogixDriver, log=lambda m: None)
        self.w.set_entries(self.entries)
        self.w.start()

    def tearDown(self):
        self.w.stop()
        self.w.join(5)

    def wait_for(self, cond, timeout=5):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if cond():
                return
            time.sleep(0.02)
        self.fail("timed out")

    def test_cyclic_groups_batched(self):
        self.wait_for(lambda: (self.w.cached("controller/arTemps") or {}).get("value") is not None)
        self.assertEqual(self.w.cached("controller/arTemps")["value"][:3], [20.0, 21.0, 22.0])
        self.assertEqual(self.w.cached("controller/Station1/Name")["value"], "Infeed")
        start = self.w.status()["groups"][0]["reads"]
        time.sleep(1.0)
        st = self.w.status()
        self.assertTrue(st["connected"])
        self.assertEqual([(g["rate_ms"], g["tags"]) for g in st["groups"]], [(100, 2), (300, 1)])
        g100 = st["groups"][0]
        # ~10 reads per second at 100 ms: never a busy loop, and still reading even if this machine
        # pauses the thread now and then (the schedule catches up instead of queueing missed cycles)
        self.assertTrue(4 <= g100["reads"] - start <= 12, g100)
        # each group read is ONE request to the PLC
        reads = self.w.session.driver.read_calls
        self.assertLessEqual(reads, g100["reads"] + st["groups"][1]["reads"] + 2)
        self.assertNotIn("controller/Station1/Status", self.w.cache)  # on-request tags aren't polled

    def test_on_request_read_write_and_cache(self):
        self.wait_for(lambda: self.w.connected)
        r = Recorder()
        self.w.read_async("controller/Station1/Status", r)
        self.assertEqual(r.wait(), (0, None))
        w = Recorder()
        self.w.write_async("controller/Station1/Status", 42, w)
        self.assertEqual(w.wait(), (None,))
        calls = self.w.session.driver.read_calls
        r2 = Recorder()
        self.w.read_async("controller/Station1/Status", r2, max_age_ms=5000)   # fresh: from cache
        self.assertEqual(r2.wait(), (42, None))
        self.assertEqual(self.w.session.driver.read_calls, calls)
        denied = Recorder()
        self.w.write_async("controller/rSpeed", 1.0, denied)
        self.assertIn("read-only", denied.wait()[0])
        plc_denied = Recorder()
        self.w.write_async("controller/rReadOnly", 1.0, plc_denied)
        self.assertIn("read only", plc_denied.wait()[0])

    def test_browse_via_call(self):
        self.wait_for(lambda: self.w.connected)
        items = self.w.call(lambda s: s.children("controller", "Station1"))["items"]
        self.assertEqual(items[1]["name"], "Pos")
        with self.assertRaises(Exception):
            self.w.call(lambda s: s.children("controller", "Nope"))

    def test_disconnect_and_reconnect(self):
        self.wait_for(lambda: self.w.connected)
        fake.set_offline("10.0.0.2", True)
        self.wait_for(lambda: not self.w.connected)
        self.assertEqual(self.w.cached("controller/rSpeed")["error"], "PLC not connected")
        r = Recorder()
        self.w.read_async("controller/Station1/Status", r)
        self.assertEqual(r.wait(), (None, "PLC not connected"))
        with self.assertRaises(WorkerError):
            self.w.call(lambda s: s.children("controller", ""))
        fake.set_offline("10.0.0.2", False)
        self.wait_for(lambda: self.w.connected, timeout=8)
        self.wait_for(lambda: self.w.cached("controller/rSpeed")["error"] is None)


if __name__ == "__main__":
    unittest.main()
