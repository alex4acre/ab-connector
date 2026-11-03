# SPDX-License-Identifier: MIT

"""A simulated Logix controller with pycomm3-compatible tag definitions, for tests and development.

get_tag_list(), read(), write() and info behave like pycomm3's LogixDriver for the tag forms this app
uses: "Tag", "Tag.Member", "Arr[3]", "Arr[1,2]", "Arr{10}", "Arr[2].Member", "Program:Main.Tag", and
BOOL members/bits of integers. Values change over time where noted, so cyclic reads have something to show.
"""

import copy
import math
import re
import threading
import time
from collections import namedtuple

Tag = namedtuple("Tag", "tag value type error")

ATOMIC_DEFAULT = {"BOOL": False, "DWORD": False, "REAL": 0.0, "LREAL": 0.0}   # DWORD: elements of a BOOL array


def atomic(name, data_type, dims=None, access="Read/Write"):
    dims = list(dims or [])
    return {"tag_name": name, "dim": len(dims), "dimensions": (dims + [0, 0, 0])[:3], "tag_type": "atomic",
            "data_type": data_type, "data_type_name": data_type, "external_access": access, "alias": False}


def struct(name, udt, dims=None, access="Read/Write"):
    dims = list(dims or [])
    return {"tag_name": name, "dim": len(dims), "dimensions": (dims + [0, 0, 0])[:3], "tag_type": "struct",
            "data_type": udt, "data_type_name": udt["name"], "external_access": access, "alias": False}


def member(data_type, array=0, bit=None):
    if isinstance(data_type, dict):
        m = {"tag_type": "struct", "data_type": data_type, "data_type_name": data_type["name"], "array": array}
    else:
        m = {"tag_type": "atomic", "data_type": data_type, "data_type_name": data_type}
        if bit is not None:
            m["bit"] = bit
        else:
            m["array"] = array
    return m


def udt(name, members: dict):
    return {"name": name, "attributes": list(members), "internal_tags": members}


STRING = {"name": "STRING", "attributes": ["LEN", "DATA"], "string": 82,
          "internal_tags": {"LEN": member("DINT"), "DATA": member("SINT", 82)}}
AXIS_T = udt("Axis_t", {"ActPos": member("REAL"), "ActVel": member("REAL"), "Enabled": member("BOOL", bit=0),
                        "Fault": member("BOOL", bit=1), "StatusBits": member("SINT")})
# Predefined type like Logix's MOTION_INSTRUCTION: the DEAD_ZONE members are listed in the type
# definition, but the controller rejects reading them by name (IOI syntax error); reading the
# whole structure works.
MOTION_INSTRUCTION = udt("MOTION_INSTRUCTION", {
    "FLAGS": member("DINT"), "EN": member("BOOL", bit=31), "DN": member("BOOL", bit=29), "ER": member("BOOL", bit=28),
    "PC": member("BOOL", bit=27), "IP": member("BOOL", bit=26), "AC": member("BOOL", bit=23),
    "ERR": member("INT"), "STATUS": member("SINT"), "STATE": member("SINT"), "SEGMENT": member("DINT"),
    "EXERR": member("SINT"), "DEAD_ZONE1": member("SINT"), "DEAD_ZONE2": member("SINT"), "DEAD_ZONE3": member("SINT")})
MOTION_INSTRUCTION["no_direct_access"] = ["DEAD_ZONE1", "DEAD_ZONE2", "DEAD_ZONE3"]
IOI_ERROR = ("IOI syntax error. A syntax error was detected decoding the Request Path (see extended status) - "
             "Extended status out of memory  (04, 00)")

STATION_T = udt("Station_t", {"Name": member(STRING), "Pos": member("REAL", 6), "Status": member("DINT"),
                              "Count": member("DINT"), "Axis": member(AXIS_T)})

CONTROLLER_TAGS = [
    atomic("bRun", "BOOL"), atomic("iPartCount", "DINT"), atomic("rSpeed", "REAL"),
    atomic("arTemps", "REAL", [10]), atomic("aiGrid", "INT", [3, 4]), atomic("abFlags", "DWORD", [32]),   # BOOL[32], reported as pycomm3 does
    struct("sRecipe", STRING), struct("Station1", STATION_T), struct("Stations", STATION_T, [4]),
    atomic("lCycles", "LINT"), atomic("rReadOnly", "REAL", access="Read Only"),
    struct("MAJTest", MOTION_INSTRUCTION),
]
PROGRAM_TAGS = {
    "MainProgram": [atomic("Step", "DINT"), atomic("Timer_ms", "DINT"), struct("Conveyor", AXIS_T),
                    atomic("arBig", "REAL", [2000])],
    "Diagnostics": [atomic("ErrorCode", "DINT"), struct("LastError", STRING)],
}


def initial_value(info, top=True):
    dims = [d for d in info["dimensions"][: info["dim"]]] if top else ([info["array"]] if info.get("array") else [])
    count = 1
    for d in dims:
        count *= d
    one = _single(info)
    return [copy.deepcopy(one) for _ in range(count)] if dims else one


def _single(info):
    dt = info["data_type"]
    if isinstance(dt, dict):
        if "string" in dt:
            return ""
        return {m: initial_value(dt["internal_tags"][m], top=False) for m in dt["attributes"]}
    return ATOMIC_DEFAULT.get(dt, 0)


class FakeLogixDriver:
    """Shared by all connections to the same simulated controller (state is per address)."""

    _states = {}
    _lock = threading.Lock()

    def __init__(self, address, **kwargs):
        self.address = address
        self.connected = False
        self._tags = {}
        self.info = {"product_name": "1756-L83E/B (simulated)", "revision": {"major": 33, "minor": 11},
                     "serial": "c0ffee01", "name": "SimPLC"}
        self.read_calls = 0
        self.uploads = []
        with FakeLogixDriver._lock:
            if address not in FakeLogixDriver._states:
                FakeLogixDriver._states[address] = self._build_state()
        self.state = FakeLogixDriver._states[address]

    @staticmethod
    def _build_state():
        values = {t["tag_name"]: initial_value(t) for t in CONTROLLER_TAGS}
        for prog, tags in PROGRAM_TAGS.items():
            for t in tags:
                values[f"Program:{prog}.{t['tag_name']}"] = initial_value(t)
        values["Station1"]["Name"] = "Infeed"
        values["Station1"]["Pos"] = [12.5, 0.0, 340.25, 0.0, 90.0, 0.0]
        values["sRecipe"] = "Bolts M8"
        for i, s in enumerate(values["Stations"]):
            s["Name"] = f"Station {i}"
        values["arTemps"] = [20.0 + i for i in range(10)]
        values["MAJTest"].update({"FLAGS": 33554432, "DEAD_ZONE1": 3, "DEAD_ZONE2": 4, "DEAD_ZONE3": 5})
        return {"values": values, "start": time.monotonic(), "offline": False}

    @classmethod
    def reset(cls):
        cls._states = {}

    # --- connection -----------------------------------------------------------------------------

    def open(self):
        if self.state["offline"] or self.address.startswith("0."):
            return False
        self.connected = True
        return True

    def close(self):
        self.connected = False

    def _check(self):
        if self.state["offline"]:
            self.connected = False
        if not self.connected:
            raise ConnectionError("not connected")

    # --- tag list -------------------------------------------------------------------------------

    def get_tag_list(self, program=None, cache=True):
        self._check()
        self.uploads.append(program)
        if program is None:
            self.info["programs"] = {p: {"instance_id": i} for i, p in enumerate(PROGRAM_TAGS)}
            tags = copy.deepcopy(CONTROLLER_TAGS)
        else:
            tags = []
            for t in copy.deepcopy(PROGRAM_TAGS[program]):
                t["tag_name"] = f"Program:{program}.{t['tag_name']}"
                tags.append(t)
        if cache:
            self._tags = {t["tag_name"]: t for t in tags}
        return tags

    # --- values ---------------------------------------------------------------------------------

    def _animate(self):
        t = time.monotonic() - self.state["start"]
        v = self.state["values"]
        v["rSpeed"] = round(100 + 50 * math.sin(t / 3), 3)
        v["iPartCount"] = int(t * 2)
        v["Station1"]["Axis"]["ActPos"] = round(180 * math.sin(t / 5), 3)
        v["Program:MainProgram.Timer_ms"] = int(t * 1000)

    def _locate(self, tag):
        """(container, key, info, count) for a tag string; count is the {n} element count or None."""
        m = re.match(r"^(.*?)(?:\{(\d+)\})?$", tag)
        name, count = m.group(1), int(m.group(2)) if m.group(2) else None
        prefix = ""
        if name.startswith("Program:"):
            prog, _, rest = name[8:].partition(".")
            prefix, name = f"Program:{prog}.", rest
        parts = name.split(".")
        base, _, idx = parts[0].partition("[")
        full = prefix + base
        if full not in self._tags:
            raise KeyError(f"Tag doesn't exist - {full}")
        info, top = self._tags[full], True
        container, key = self.state["values"], full
        parts = [parts[0]] + parts[1:]
        for n, part in enumerate(parts):
            pname, _, pidx = part.partition("[")
            if n > 0:
                if part.isdigit() and not isinstance(info.get("data_type"), dict):   # bit of an integer
                    return container, key, {"bit_of_int": int(part), **info}, count
                dt = info["data_type"]
                if not isinstance(dt, dict) or pname not in dt["internal_tags"]:
                    raise KeyError(f"Tag doesn't exist - {pname}")
                if pname in dt.get("no_direct_access", []):
                    raise KeyError(IOI_ERROR)
                container, key = container[key], pname
                info, top = dt["internal_tags"][pname], False
            if pidx:
                dims = [d for d in info["dimensions"][: info["dim"]]] if top else [info.get("array")]
                flat = 0
                for i, d in zip([int(x) for x in pidx.rstrip("]").split(",")], dims):
                    if i >= d:
                        raise IndexError("index out of range")
                    flat = flat * d + i
                container, key = container[key], flat
                info = dict(info, dim=0, dimensions=[0, 0, 0], array=0)
        return container, key, info, count

    def read(self, *tags):
        self._check()
        self.read_calls += 1
        self._animate()
        out = []
        for tag in tags:
            try:
                container, key, info, count = self._locate(tag)
                value = container[key]
                if "bit_of_int" in info:
                    value = bool(value >> info["bit_of_int"] & 1)
                elif count is not None:
                    value = list(value[:count]) if isinstance(value, list) else [value]
                out.append(Tag(tag, copy.deepcopy(value), info.get("data_type_name"), None))
            except (KeyError, IndexError, TypeError) as e:
                out.append(Tag(tag, None, None, str(e)))
        return out if len(out) > 1 else out[0]

    def write(self, *pairs):
        self._check()
        if len(pairs) == 2 and isinstance(pairs[0], str):
            pairs = (pairs,)
        out = []
        for tag, value in pairs:
            try:
                container, key, info, count = self._locate(tag)
                if info.get("external_access") == "Read Only":
                    raise PermissionError("tag is read only")
                if "bit_of_int" in info:
                    b = info["bit_of_int"]
                    container[key] = (container[key] | 1 << b) if value else (container[key] & ~(1 << b))
                elif count is not None:
                    container[key][:count] = list(value)
                else:
                    container[key] = value
                out.append(Tag(tag, value, info.get("data_type_name"), None))
            except (KeyError, IndexError, TypeError, PermissionError) as e:
                out.append(Tag(tag, None, None, str(e)))
        return out if len(out) > 1 else out[0]


def set_offline(address: str, offline: bool):
    """Simulate the PLC going away (or coming back)."""
    FakeLogixDriver._states[address]["offline"] = offline
