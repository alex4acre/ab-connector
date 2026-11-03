# SPDX-License-Identifier: MIT

"""Data Layer provider logic with a stand-in for the ctrlxdatalayer Python API (the real library needs
the ctrlX Data Layer runtime, which isn't available on a build machine) and the simulated PLC."""

import enum
import json
import sys
import threading
import time
import types
import unittest


# --- minimal stand-in for ctrlxdatalayer / comm.datalayer ------------------------------------------
class VariantType(enum.Enum):
    BOOL8 = 1; INT8 = 2; UINT8 = 3; INT16 = 4; UINT16 = 5; INT32 = 6; UINT32 = 7; INT64 = 8; UINT64 = 9
    FLOAT32 = 10; FLOAT64 = 11; STRING = 12; ARRAY_BOOL8 = 13; ARRAY_INT8 = 14; ARRAY_UINT8 = 15
    ARRAY_INT16 = 16; ARRAY_UINT16 = 17; ARRAY_INT32 = 18; ARRAY_UINT32 = 19; ARRAY_INT64 = 20; ARRAY_UINT64 = 21
    ARRAY_FLOAT32 = 22; ARRAY_FLOAT64 = 23; ARRAY_STRING = 24


class Result(enum.Enum):
    OK = 0; FAILED = 1; PERMISSION_DENIED = 2; TYPE_MISMATCH = 3; UNSUPPORTED = 4


class Variant:
    def __init__(self, t=None, v=None):
        self.t, self.v = t, v

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def get_type(self):
        return self.t

    def __getattr__(self, name):
        if name.startswith("set_"):
            def setter(v):
                self.t, self.v = VariantType[name[4:].upper()], v
            return setter
        if name.startswith("get_"):
            return lambda: self.v
        raise AttributeError(name)


class AllowedOperation(enum.IntFlag):
    NONE = 0; READ = 1; WRITE = 0x10; CREATE = 0x100; DELETE = 0x1000; BROWSE = 0x10000


class MetadataBuilder:
    def __init__(self, allowed, description=""):
        self.d = {"ops": allowed, "description": description, "refs": {}}

    def set_display_name(self, n):
        self.d["name"] = n

    def set_node_class(self, c):
        self.d["class"] = c

    def add_reference(self, kind, address):
        self.d["refs"][kind] = address

    def build(self):
        return Variant("META", self.d)


class ReferenceType:
    @classmethod
    def read(cls):
        return "readType"

    @classmethod
    def write(cls):
        return "writeType"


stub_modules = {
    "ctrlxdatalayer": types.ModuleType("ctrlxdatalayer"),
    "ctrlxdatalayer.metadata_utils": types.SimpleNamespace(AllowedOperation=AllowedOperation,
                                                           MetadataBuilder=MetadataBuilder, ReferenceType=ReferenceType),
    "ctrlxdatalayer.provider_node": types.SimpleNamespace(NodeCallback=object, ProviderNode=lambda cbs: cbs,
                                                          ProviderNodeCallbacks=lambda *a: a),
    "ctrlxdatalayer.variant": types.SimpleNamespace(Result=Result, Variant=Variant, VariantType=VariantType),
    "comm": types.ModuleType("comm"),
    "comm.datalayer": types.SimpleNamespace(NodeClass=types.SimpleNamespace(NodeClass=types.SimpleNamespace(Variable="Variable"))),
}
sys.modules.update(stub_modules)

from app.config import default_node, expand  # noqa: E402
from app.fake import FakeLogixDriver  # noqa: E402
from app.logix import Session  # noqa: E402
from app.provider import NodeRegistry, get_value  # noqa: E402
from app.scanner import ControllerWorker  # noqa: E402


class FakeProvider:
    def __init__(self):
        self.nodes, self.timeouts = {}, {}

    def register_node(self, address, node):
        self.nodes[address] = node
        return Result.OK

    def unregister_node(self, address):
        self.nodes.pop(address, None)

    def set_timeout_node(self, node, ms):
        self.timeouts[id(node)] = ms


def call(node_cbs, op, address, data=None):
    """Invoke a node callback like the Data Layer would; returns (Result, value) when answered."""
    on_create, on_remove, on_browse, on_read, on_write, on_metadata = node_cbs
    done = threading.Event()
    box = {}

    def cb(result, variant):
        box["r"] = (result, variant.v if variant is not None else None)
        done.set()
    {"read": lambda: on_read(None, address, data, cb), "write": lambda: on_write(None, address, data, cb),
     "metadata": lambda: on_metadata(None, address, cb)}[op]()
    assert done.wait(5), "no answer"
    return box["r"]


class ProviderTest(unittest.TestCase):

    def setUp(self):
        FakeLogixDriver.reset()
        probe = Session(FakeLogixDriver, "10.0.0.3")
        probe.connect()
        self.entries = []
        for path, mode, writable in (("rSpeed", "cyclic", False), ("Station1.Pos", "cyclic", True),
                                     ("Station1.Status", "on-request", True), ("sRecipe", "on-request", True)):
            for leaf in expand(probe, "controller", path):
                self.entries.append({"controller": "L", "scope": "controller", "path": leaf["path"],
                                     "node": default_node("controller", leaf["path"]), "type": leaf["type"],
                                     "dl_type": leaf["dl_type"], "count": leaf["count"], "mode": mode,
                                     "rate_ms": 100, "writable": writable})
        self.w = ControllerWorker("L", "10.0.0.3", FakeLogixDriver, log=lambda m: None)
        self.w.set_entries(self.entries)
        self.w.start()
        self.p = FakeProvider()
        self.reg = NodeRegistry(self.p, log=lambda m: None)
        self.reg.sync({"L": self.w}, self.entries)

    def tearDown(self):
        self.w.stop()
        self.w.join(5)

    def node(self, rel):
        return self.p.nodes["ab-connect/L/" + rel]

    def wait_cached(self, rel):
        end = time.monotonic() + 5
        while time.monotonic() < end:
            c = self.w.cached(rel)
            if c and c["value"] is not None:
                return
            time.sleep(0.02)
        self.fail("no value")

    def test_registered_before_any_plc_read(self):
        self.assertEqual(sorted(self.p.nodes), [
            "ab-connect/L/connected", "ab-connect/L/controller/Station1/Pos", "ab-connect/L/controller/Station1/Status",
            "ab-connect/L/controller/rSpeed", "ab-connect/L/controller/sRecipe", "ab-connect/L/status"])

    def test_metadata_types(self):
        r, meta = call(self.node("controller/Station1/Pos"), "metadata", "")
        self.assertEqual(meta["refs"], {"readType": "types/datalayer/array-of-float32", "writeType": "types/datalayer/array-of-float32"})
        self.assertIn("REAL[6] Station1.Pos", meta["description"])
        r, meta = call(self.node("controller/rSpeed"), "metadata", "")
        self.assertEqual(meta["refs"], {"readType": "types/datalayer/float32"})
        self.assertFalse(meta["ops"] & AllowedOperation.WRITE)

    def test_cyclic_read_from_cache(self):
        self.wait_cached("controller/Station1/Pos")
        self.assertEqual(call(self.node("controller/Station1/Pos"), "read", ""),
                         (Result.OK, [12.5, 0.0, 340.25, 0.0, 90.0, 0.0]))

    def test_on_request_read_and_writes(self):
        self.assertEqual(call(self.node("controller/sRecipe"), "read", ""), (Result.OK, "Bolts M8"))
        self.assertEqual(call(self.node("controller/Station1/Status"), "write", "", Variant(VariantType.INT32, 9)),
                         (Result.OK, 9))
        self.assertEqual(self.w.session.read(["Station1.Status"]), [(9, None)])
        arr = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
        self.assertEqual(call(self.node("controller/Station1/Pos"), "write", "", Variant(VariantType.ARRAY_FLOAT64, arr))[0], Result.OK)
        self.assertEqual(call(self.node("controller/Station1/Pos"), "write", "", Variant(VariantType.ARRAY_FLOAT64, [1.0]))[0],
                         Result.TYPE_MISMATCH)
        self.assertEqual(call(self.node("controller/rSpeed"), "write", "", Variant(VariantType.FLOAT32, 1.0))[0],
                         Result.PERMISSION_DENIED)
        self.assertEqual(call(self.node("controller/Station1/Status"), "write", "", Variant(VariantType.FLOAT64, 2.5))[0],
                         Result.TYPE_MISMATCH)

    def test_status_nodes(self):
        end = time.monotonic() + 5
        while not self.w.connected and time.monotonic() < end:
            time.sleep(0.02)
        self.assertEqual(call(self.p.nodes["ab-connect/L/connected"], "read", ""), (Result.OK, True))
        status = json.loads(call(self.p.nodes["ab-connect/L/status"], "read", "")[1])
        self.assertTrue(status["connected"])
        self.assertEqual(status["tags"], 4)

    def test_sync_adds_changes_and_removes(self):
        changed = [dict(e) for e in self.entries if e["path"] != "rSpeed"]
        changed[0]["writable"] = False       # Station1.Pos: metadata must change -> re-registered
        self.reg.sync({"L": self.w}, changed)
        self.assertNotIn("ab-connect/L/controller/rSpeed", self.p.nodes)
        r, meta = call(self.node("controller/Station1/Pos"), "metadata", "")
        self.assertNotIn("writeType", meta["refs"])
        self.reg.sync({}, [])
        self.assertEqual(self.p.nodes, {})

    def test_value_conversion(self):
        self.assertEqual(get_value(Variant(VariantType.INT64, 5), "int16", None), 5)
        self.assertEqual(get_value(Variant(VariantType.FLOAT64, 5.0), "int32", None), 5)
        self.assertEqual(get_value(Variant(VariantType.INT32, 1), "bool8", None), True)
        with self.assertRaises(ValueError):
            get_value(Variant(VariantType.STRING, "x"), "float32", None)
        with self.assertRaises(ValueError):
            get_value(Variant(VariantType.INT32, 1), "float32", 3)


if __name__ == "__main__":
    unittest.main()
