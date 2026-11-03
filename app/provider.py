# SPDX-License-Identifier: MIT

"""Data Layer nodes for the selected tags: ab-connect/<controller>/<node>.

- cyclic tags: a read returns the latest scanned value at once (never waits for the PLC)
- on-request tags: a read is passed to the PLC worker and answered when the value arrives
  (the Data Layer lets a provider answer later; the node's timeout is raised accordingly)
- writes (only for tags marked writable) go to the PLC; the answer comes when the PLC confirms

Each controller also has ab-connect/<controller>/connected (bool) and .../status (JSON).
"""

import json

import ctrlxdatalayer
from comm.datalayer import NodeClass
from ctrlxdatalayer.metadata_utils import AllowedOperation, MetadataBuilder, ReferenceType
from ctrlxdatalayer.provider_node import NodeCallback, ProviderNode, ProviderNodeCallbacks
from ctrlxdatalayer.variant import Result, Variant, VariantType

ROOT = "ab-connect"
ON_REQUEST_TIMEOUT_MS = 10000

INT_TYPES = {VariantType.INT8, VariantType.UINT8, VariantType.INT16, VariantType.UINT16, VariantType.INT32,
             VariantType.UINT32, VariantType.INT64, VariantType.UINT64}
FLOAT_TYPES = {VariantType.FLOAT32, VariantType.FLOAT64}


def set_value(variant: Variant, dl_type: str, count, value):
    """Store a PLC value in a Variant of the tag's Data Layer type."""
    if count:
        getattr(variant, f"set_array_{dl_type}")(list(value))
    else:
        getattr(variant, f"set_{dl_type}")(value)


def get_value(variant: Variant, dl_type: str, count):
    """Value written by a Data Layer client, converted for the PLC; raises ValueError if it doesn't fit."""
    t = variant.get_type()
    if count:
        if not t.name.startswith("ARRAY_"):
            raise ValueError("an array is expected")
        value = getattr(variant, "get_" + t.name.lower())()
        if len(value) != count:
            raise ValueError(f"{count} elements expected, got {len(value)}")
        return [_convert(v, dl_type) for v in value]
    if t == VariantType.STRING:
        return _convert(variant.get_string(), dl_type)
    if t == VariantType.BOOL8:
        return _convert(variant.get_bool8(), dl_type)
    if t in INT_TYPES or t in FLOAT_TYPES:
        return _convert(getattr(variant, "get_" + t.name.lower())(), dl_type)
    raise ValueError(f"unsupported value type {t.name}")


def _convert(v, dl_type):
    if dl_type == "string":
        if not isinstance(v, str):
            raise ValueError("a string is expected")
        return v
    if dl_type == "bool8":
        return bool(v)
    if dl_type.startswith(("float",)):
        return float(v)
    if isinstance(v, float) and not v.is_integer():
        raise ValueError(f"{v} is not a whole number")
    return int(v)


def metadata(entry: dict) -> Variant:
    ops = AllowedOperation.READ | AllowedOperation.BROWSE
    if entry["writable"]:
        ops |= AllowedOperation.WRITE
    rate = f"every {entry['rate_ms']} ms" if entry["mode"] == "cyclic" else "read on request"
    dims = f"[{entry['count']}]" if entry["count"] else ""
    builder = MetadataBuilder(allowed=ops, description=f"{entry['type']}{dims} {entry['path']} ({entry['scope']}), {rate}")
    builder.set_display_name(entry["node"].rsplit("/", 1)[-1])
    builder.set_node_class(NodeClass.NodeClass.Variable)
    type_address = "types/datalayer/" + (f"array-of-{entry['dl_type']}" if entry["count"] else entry["dl_type"])
    builder.add_reference(ReferenceType.read(), type_address)
    if entry["writable"]:
        builder.add_reference(ReferenceType.write(), type_address)
    return builder.build()


class TagNode:

    def __init__(self, provider, worker, entry: dict):
        self.provider = provider
        self.worker = worker
        self.entry = entry
        self.address = f"{ROOT}/{entry['controller']}/{entry['node']}"
        self.meta = metadata(entry)
        self.node = ProviderNode(ProviderNodeCallbacks(self.on_create, self.on_remove, self.on_browse,
                                                       self.on_read, self.on_write, self.on_metadata))

    def register(self):
        result = self.provider.register_node(self.address, self.node)
        if self.entry["mode"] == "on-request" or self.entry["writable"]:
            self.provider.set_timeout_node(self.node, ON_REQUEST_TIMEOUT_MS)
        return result

    def unregister(self):
        self.provider.unregister_node(self.address)

    def _answer(self, cb: NodeCallback, value, error):
        if error is not None or value is None:
            cb(Result.FAILED, None)
            return
        try:
            with Variant() as v:
                set_value(v, self.entry["dl_type"], self.entry["count"], value)
                cb(Result.OK, v)
        except Exception:
            cb(Result.TYPE_MISMATCH, None)

    def on_read(self, userdata, address: str, data: Variant, cb: NodeCallback):
        if self.entry["mode"] == "cyclic":
            c = self.worker.cached(self.entry["node"])
            self._answer(cb, c and c["value"], c["error"] if c else "No value yet")
        else:
            self.worker.read_async(self.entry["node"], lambda value, error: self._answer(cb, value, error),
                                   max_age_ms=self.entry["rate_ms"])

    def on_write(self, userdata, address: str, data: Variant, cb: NodeCallback):
        if not self.entry["writable"]:
            cb(Result.PERMISSION_DENIED, None)
            return
        try:
            value = get_value(data, self.entry["dl_type"], self.entry["count"])  # copy now: data is only valid here
        except (ValueError, TypeError):
            cb(Result.TYPE_MISMATCH, None)
            return

        def done(error):
            if error is None:
                self._answer(cb, value, None)
            else:
                cb(Result.FAILED, None)
        self.worker.write_async(self.entry["node"], value, done)

    def on_metadata(self, userdata, address: str, cb: NodeCallback):
        cb(Result.OK, self.meta)

    def on_browse(self, userdata, address: str, cb: NodeCallback):
        with Variant() as v:
            v.set_array_string([])
            cb(Result.OK, v)

    def on_create(self, userdata, address: str, data: Variant, cb: NodeCallback):
        cb(Result.UNSUPPORTED, None)

    def on_remove(self, userdata, address: str, cb: NodeCallback):
        cb(Result.UNSUPPORTED, None)


class StatusNode:
    """ab-connect/<controller>/connected and .../status."""

    def __init__(self, provider, worker, controller: str, kind: str):
        self.provider = provider
        self.worker = worker
        self.kind = kind
        self.address = f"{ROOT}/{controller}/{kind}"
        builder = MetadataBuilder(allowed=AllowedOperation.READ | AllowedOperation.BROWSE,
                                  description="PLC connected" if kind == "connected" else "Connection and scan status (JSON)")
        builder.set_display_name(kind)
        builder.set_node_class(NodeClass.NodeClass.Variable)
        builder.add_reference(ReferenceType.read(), "types/datalayer/" + ("bool8" if kind == "connected" else "string"))
        self.meta = builder.build()
        self.node = ProviderNode(ProviderNodeCallbacks(self.on_create, self.on_remove, self.on_browse,
                                                       self.on_read, self.on_write, self.on_metadata))

    def register(self):
        return self.provider.register_node(self.address, self.node)

    def unregister(self):
        self.provider.unregister_node(self.address)

    def on_read(self, userdata, address, data, cb):
        with Variant() as v:
            if self.kind == "connected":
                v.set_bool8(self.worker.connected)
            else:
                v.set_string(json.dumps(self.worker.status()))
            cb(Result.OK, v)

    def on_write(self, userdata, address, data, cb):
        cb(Result.PERMISSION_DENIED, None)

    def on_metadata(self, userdata, address, cb):
        cb(Result.OK, self.meta)

    def on_browse(self, userdata, address, cb):
        with Variant() as v:
            v.set_array_string([])
            cb(Result.OK, v)

    def on_create(self, userdata, address, data, cb):
        cb(Result.UNSUPPORTED, None)

    def on_remove(self, userdata, address, cb):
        cb(Result.UNSUPPORTED, None)


class NodeRegistry:
    """Keeps the registered Data Layer nodes in line with the configuration."""

    def __init__(self, provider, log=print):
        self.provider = provider
        self.log = log
        self.tags = {}       # address -> (TagNode, entry)
        self.status = {}     # controller -> [StatusNode, StatusNode]

    def sync(self, workers: dict, entries: list):
        """workers: controller -> ControllerWorker; entries: all configured tags."""
        wanted = {}
        for e in entries:
            if e["controller"] in workers:
                wanted[f"{ROOT}/{e['controller']}/{e['node']}"] = e
        for address in [a for a in self.tags if a not in wanted or self.tags[a][1] != wanted[a]]:
            self.tags.pop(address)[0].unregister()
        for address, e in wanted.items():
            if address not in self.tags:
                node = TagNode(self.provider, workers[e["controller"]], e)
                result = node.register()
                if result != Result.OK:
                    self.log(f"Registering {address} failed: {result}")
                self.tags[address] = (node, e)
        for c in [c for c in self.status if c not in workers]:
            for n in self.status.pop(c):
                n.unregister()
        for c, w in workers.items():
            if c not in self.status:
                self.status[c] = [StatusNode(self.provider, w, c, "connected"), StatusNode(self.provider, w, c, "status")]
                for n in self.status[c]:
                    n.register()

    def clear(self):
        for node, _ in self.tags.values():
            node.unregister()
        for nodes in self.status.values():
            for n in nodes:
                n.unregister()
        self.tags, self.status = {}, {}
