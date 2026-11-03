# SPDX-License-Identifier: MIT

"""REST API with the simulated PLC (no Data Layer): structure reads, preview, discovery input checks."""

import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

import app.web
from app.core import Core
from app.fake import FakeLogixDriver

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


class WebTest(unittest.TestCase):

    def setUp(self):
        FakeLogixDriver.reset()
        self.dir = tempfile.mkdtemp()
        self.core = Core(os.path.join(self.dir, "data", "config.json"), FakeLogixDriver, log=lambda m: None)
        self.core.apply()
        app.web.core = self.core
        app.web.www_dir = os.path.join(ROOT, "www")
        self.server = app.web.TcpServer(("127.0.0.1", 0), app.web.RequestHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}/ab-connect/api/v1"
        self.call("POST", "/controllers", {"name": "Line1", "address": "10.9.9.9"})
        self.wait(lambda: self.core.workers["Line1"].connected)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.core.stop()
        shutil.rmtree(self.dir)

    def call(self, method, path, body=None):
        req = urllib.request.Request(self.base + path, method=method,
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=30) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def wait(self, cond, timeout=8):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if cond():
                return
            time.sleep(0.05)
        self.fail("timed out")

    def tags(self):
        return {t["node"]: t for t in self.call("GET", "/tags?controller=Line1")[1]}

    def test_motion_instruction_whole_structure(self):
        status, body = self.call("POST", "/tags", {"controller": "Line1", "scope": "controller", "path": "MAJTest",
                                                   "mode": "cyclic", "rate_ms": 200})
        self.assertEqual((status, body["added"]), (201, 15))
        self.assertTrue(all(t["via"]["path"] == "MAJTest" for t in body["tags"]))
        self.wait(lambda: self.tags()["controller/MAJTest/DEAD_ZONE3"]["value"] is not None)
        tags = self.tags()
        self.assertEqual([tags[f"controller/MAJTest/DEAD_ZONE{i}"]["value"] for i in (1, 2, 3)], [3, 4, 5])
        self.assertEqual(tags["controller/MAJTest/FLAGS"]["value"], 33554432)
        self.assertTrue(all(t["error"] is None for t in tags.values()))
        # one PLC request per scan for all 15 members
        w = self.core.workers["Line1"]
        reads, calls = w.status()["groups"][0]["reads"], w.session.driver.read_calls
        time.sleep(0.7)
        self.assertEqual(w.session.driver.read_calls - calls, w.status()["groups"][0]["reads"] - reads)

    def test_single_unaddressable_member_read_through_parent(self):
        status, body = self.call("POST", "/tags", {"controller": "Line1", "scope": "controller",
                                                   "path": "MAJTest.DEAD_ZONE1", "mode": "cyclic", "rate_ms": 200})
        self.assertEqual(body["tags"][0]["via"], {"path": "MAJTest", "count": None, "keys": ["DEAD_ZONE1"]})
        status, body = self.call("POST", "/tags", {"controller": "Line1", "scope": "controller",
                                                   "path": "MAJTest.FLAGS", "mode": "cyclic", "rate_ms": 200})
        self.assertNotIn("via", body["tags"][0])        # readable by name: read directly
        self.wait(lambda: self.tags()["controller/MAJTest/DEAD_ZONE1"]["value"] == 3)
        with open(self.core.config_path) as f:
            saved = json.load(f)
        self.assertEqual(saved["tags"][0]["via"]["path"], "MAJTest")

    def test_preview_structure(self):
        status, body = self.call("GET", "/controllers/Line1/preview?scope=controller&path=MAJTest")
        self.assertEqual(status, 200)
        values = {v["path"]: v for v in body["values"]}
        self.assertEqual(values["MAJTest.DEAD_ZONE2"]["value"], 4)
        self.assertTrue(all(v["error"] is None for v in body["values"]))
        status, body = self.call("GET", "/controllers/Line1/preview?scope=program:MainProgram&path=arBig")
        self.assertEqual(len(body["values"][0]["value"]), 20)      # long arrays cut for the preview

    def test_array_of_udts_one_request(self):
        status, body = self.call("POST", "/tags", {"controller": "Line1", "scope": "controller", "path": "Stations",
                                                   "mode": "cyclic", "rate_ms": 200})
        self.assertEqual(body["added"], 36)
        self.assertEqual(body["tags"][0]["via"], {"path": "Stations", "count": 4, "keys": [0, "Name"]})
        self.wait(lambda: self.tags()["controller/Stations/3/Name"]["value"] == "Station 3")

    def test_discover_range_checks(self):
        self.assertEqual(self.call("GET", "/discover?range=not-a-range")[0], 400)
        self.assertEqual(self.call("GET", "/discover?range=10.0.0.0/16")[0], 400)


if __name__ == "__main__":
    unittest.main()
