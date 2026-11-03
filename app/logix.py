# SPDX-License-Identifier: MIT

"""Logix (ControlLogix / CompactLogix) access: tag definitions per scope, lazy tag tree, read and write.

Tag definitions are the dicts pycomm3's LogixDriver.get_tag_list() returns. The driver is opened
WITHOUT uploading any tags (init_tags=False); definitions are uploaded one scope at a time ("controller"
or "program:<name>") when the tree is browsed or a selected tag needs reading, and merged into the
driver's tag table, which pycomm3 needs for every read and write.

A Session is not thread-safe (nor is pycomm3): only the controller's worker thread uses it.
"""

import re

# Logix atomic type -> Data Layer type (types/datalayer/<name>)
ATOMIC_TYPES = {
    "BOOL": "bool8", "SINT": "int8", "INT": "int16", "DINT": "int32", "LINT": "int64",
    "USINT": "uint8", "UINT": "uint16", "UDINT": "uint32", "ULINT": "uint64",
    "REAL": "float32", "LREAL": "float64", "DWORD": "uint32", "BYTE": "uint8", "WORD": "uint16", "LWORD": "uint64",
}

SEGMENT_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)(?:\[([0-9, ]+)\])?$")


class LogixError(Exception):
    pass


def scope_prefix(scope: str) -> str:
    """"controller" -> "", "program:Main" -> "Program:Main." (prefix of full Logix tag names)."""
    if scope == "controller":
        return ""
    if scope.startswith("program:") and len(scope) > 8:
        return "Program:" + scope[8:] + "."
    raise LogixError(f"Unknown scope {scope!r}")


def is_bool_array(info: dict) -> bool:
    """Logix stores BOOL arrays as packed DWORDs; pycomm3 reports them with data type DWORD and the
    number of BOOL elements as the dimension, and reads "Tag{n}" as n booleans, "Tag[i]" as one."""
    if info.get("data_type_name") != "DWORD":
        return False
    dims = [d for d in (info.get("dimensions") or [])[: info.get("dim", 0)] if d]
    return bool(dims or info.get("array"))


def dl_type(info: dict):
    """Data Layer type of a definition (atomic or string), or None if it isn't a single value."""
    if is_string(info):
        return "string"
    if is_bool_array(info):
        return "bool8"
    if info.get("tag_type") == "atomic":
        return ATOMIC_TYPES.get(info.get("data_type_name"))
    return None


def is_string(info: dict) -> bool:
    dt = info.get("data_type")
    return info.get("tag_type") == "struct" and isinstance(dt, dict) and "string" in dt


def type_label(info: dict) -> str:
    return "BOOL" if is_bool_array(info) else (info.get("data_type_name") or "?")


def element_count(info: dict, top_level: bool) -> int:
    """Number of array elements of a tag (top level: dimensions) or member ("array"); 0 if not an array."""
    if top_level:
        dims = [d for d in (info.get("dimensions") or [])[: info.get("dim", 0)] if d]
        n = 1
        for d in dims:
            n *= d
        return n if dims else 0
    return info.get("array") or 0


def dimensions(info: dict, top_level: bool) -> list:
    if top_level:
        return [d for d in (info.get("dimensions") or [])[: info.get("dim", 0)] if d]
    return [info["array"]] if info.get("array") else []


def index_label(flat: int, dims: list) -> str:
    """Flat element number -> Logix index text, e.g. 7 in [3, 4] -> "1,3"."""
    if len(dims) <= 1:
        return str(flat)
    parts = []
    for d in reversed(dims):
        parts.append(flat % d)
        flat //= d
    return ",".join(str(p) for p in reversed(parts))


def parse_path(path: str) -> list:
    """"Station1.Pos[2]" -> [("Station1", None), ("Pos", "2")]."""
    segments = []
    for part in path.split(".") if path else []:
        m = SEGMENT_RE.match(part)
        if not m:
            raise LogixError(f"Invalid tag path element {part!r}")
        segments.append((m.group(1), m.group(2).replace(" ", "") if m.group(2) else None))
    return segments


class Session:
    """One connection to one Logix controller."""

    def __init__(self, driver_factory, address: str):
        self.driver_factory = driver_factory
        self.address = address
        self.driver = None
        self.scopes = {}          # scope -> {short tag name: definition}
        self.programs = None      # program names, known after the controller scope is loaded

    # --- connection ---------------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self.driver is not None and bool(getattr(self.driver, "connected", False))

    def connect(self):
        self.close()
        driver = self.driver_factory(self.address)
        if not driver.open():
            raise LogixError(f"Cannot connect to {self.address}")
        self.driver = driver
        self.scopes = {}
        self.programs = None

    def close(self):
        if self.driver is not None:
            try:
                self.driver.close()
            except Exception:
                pass
        self.driver = None

    def info(self) -> dict:
        i = getattr(self.driver, "info", {}) or {}
        return {k: i.get(k) for k in ("product_name", "revision", "serial", "name") if i.get(k) is not None}

    # --- definitions --------------------------------------------------------------------------

    def load_scope(self, scope: str, reload: bool = False) -> dict:
        if scope in self.scopes and not reload:
            return self.scopes[scope]
        prefix = scope_prefix(scope)
        program = None if scope == "controller" else scope[8:]
        tags = self.driver.get_tag_list(program, cache=False)
        if scope == "controller":
            self.programs = sorted((self.driver.info or {}).get("programs", {}))
        defs = {}
        for t in tags:
            name = t["tag_name"]
            if prefix and name.startswith(prefix):
                name = name[len(prefix):]
            defs[name] = t
        self.scopes[scope] = defs
        # pycomm3 resolves every read/write through its tag table: keep all loaded scopes in it
        merged = {}
        for s, d in self.scopes.items():
            p = scope_prefix(s)
            merged.update({p + n: t for n, t in d.items()})
        self.driver._tags = merged
        return defs

    def program_names(self) -> list:
        if self.programs is None:
            self.load_scope("controller")
        return self.programs

    def resolve(self, scope: str, path: str):
        """Definition at a path, plus whether it's a top-level tag and whether an index was applied.

        Returns (info, top_level, indexed): info describes the node (a tag or member); if the last
        segment has an index, the node is one element of that array.
        """
        defs = self.load_scope(scope)
        segments = parse_path(path)
        if not segments:
            raise LogixError("Empty tag path")
        name, index = segments[0]
        if name not in defs:
            raise LogixError(f"Tag {name} not found in {scope}")
        info, top, indexed = defs[name], True, index is not None
        for name, index in segments[1:]:
            dt = info.get("data_type")
            if element_count(info, top) and not indexed:
                raise LogixError(f"{name}: choose an element of the array first")
            if not isinstance(dt, dict) or name not in dt.get("internal_tags", {}):
                raise LogixError(f"No member {name}")
            info, top, indexed = dt["internal_tags"][name], False, index is not None
        return info, top, indexed

    def children(self, scope: str, path: str, offset: int = 0, limit: int = 100) -> dict:
        """One level of the tag tree: {"items": [...], "total": n} (arrays are paged)."""
        if not path:
            defs = self.load_scope(scope)
            items = [self._item(n, n, info, True, False) for n, info in sorted(defs.items(), key=lambda x: x[0].lower())]
            return {"items": items, "total": len(items)}
        info, top, indexed = self.resolve(scope, path)
        count = element_count(info, top)
        if count and not indexed:
            dims = dimensions(info, top)
            end = min(count, offset + limit)
            items = []
            for i in range(offset, end):
                label = index_label(i, dims)
                items.append(self._item(f"[{label}]", f"{path}[{label}]", info, top, True))
            return {"items": items, "total": count, "offset": offset}
        dt = info.get("data_type")
        if isinstance(dt, dict) and not is_string(info):
            items = [self._item(m, f"{path}.{m}", dt["internal_tags"][m], False, False) for m in dt.get("attributes", [])]
            return {"items": items, "total": len(items)}
        return {"items": [], "total": 0}

    @staticmethod
    def _item(label: str, path: str, info: dict, top: bool, indexed: bool) -> dict:
        count = 0 if indexed else element_count(info, top)
        single = dl_type(info)
        if count:
            kind = "array"
        elif single:
            kind = "value"
        else:
            kind = "struct"
        return {
            "name": label, "path": path, "kind": kind, "type": type_label(info),
            "elements": count or None, "dims": dimensions(info, top) if count else None,
            "dl_type": single, "has_children": kind != "value",
            "bit": info.get("bit") if info.get("data_type_name") == "BOOL" and "bit" in info else None,
            "access": info.get("external_access"),
        }

    # --- read / write -------------------------------------------------------------------------

    def read(self, tags: list) -> list:
        """[(value, error)] for full Logix tag names (array reads as "Tag{n}")."""
        if not tags:
            return []
        result = self.driver.read(*tags)
        if not isinstance(result, list):
            result = [result]
        return [(r.value, r.error) for r in result]

    def write(self, pairs: list) -> list:
        """[error or None] for [(full Logix tag name, value)]."""
        if not pairs:
            return []
        result = self.driver.write(*pairs)
        if not isinstance(result, list):
            result = [result]
        return [r.error for r in result]


def pycomm3_driver(address: str):
    """A pycomm3 LogixDriver that doesn't upload any tag definitions on connect."""
    from pycomm3 import LogixDriver
    return LogixDriver(address, init_tags=False, init_program_tags=False)


SIOCGIFADDR = 0x8915
SIOCGIFNETMASK = 0x891B
MAX_SCAN_ADDRESSES = 1024


def network_interfaces() -> list:
    """IPv4 interfaces (without loopback): [{"interface", "ip", "network", "broadcast"}]."""
    import fcntl
    import ipaddress
    import socket
    import struct
    found = []
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for _, name in socket.if_nameindex():
            req = struct.pack("256s", name.encode()[:15])
            try:
                ip = socket.inet_ntoa(fcntl.ioctl(probe.fileno(), SIOCGIFADDR, req)[20:24])
                mask = socket.inet_ntoa(fcntl.ioctl(probe.fileno(), SIOCGIFNETMASK, req)[20:24])
            except OSError:
                continue        # no IPv4 address
            if ip.startswith("127."):
                continue
            net = ipaddress.IPv4Network(f"{ip}/{mask}", strict=False)
            found.append({"interface": name, "ip": ip, "network": str(net), "broadcast": str(net.broadcast_address)})
    finally:
        probe.close()
    return found


def discover(address_range: str = None, timeout: float = 2.0) -> dict:
    """Find EtherNet/IP devices with the ListIdentity service.

    Without a range: one broadcast per network interface, to that interface's broadcast address
    (pycomm3's CIPDriver.discover() only uses the addresses the host name resolves to, which on a
    ctrlX CORE misses most ports), plus 255.255.255.255. With a range ("192.168.1.0/24"): every
    address is asked directly - for networks a broadcast can't reach (routers, NAT).
    Returns {"devices": [...], "searched": [...]}.
    """
    import ipaddress
    import select
    import socket
    import time
    from pycomm3 import CIPDriver
    from pycomm3.packets import ListIdentityRequestPacket

    request = ListIdentityRequestPacket()
    message = request.build_request(None, CIPDriver("0.0.0.0")._session, b"\x00" * 8, 0)
    sockets, searched = [], []
    try:
        if address_range:
            try:
                net = ipaddress.IPv4Network(address_range.strip(), strict=False)
            except ValueError as e:
                raise LogixError(f"Invalid range {address_range!r}: use e.g. 192.168.1.0/24") from e
            if net.num_addresses > MAX_SCAN_ADDRESSES:
                raise LogixError(f"Range too large: at most {MAX_SCAN_ADDRESSES} addresses (e.g. a /22)")
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sockets.append(sock)
            for host in (net.hosts() if net.num_addresses > 2 else net):
                try:
                    sock.sendto(message, (str(host), 44818))
                except OSError:
                    pass
            searched.append({"range": str(net)})
        else:
            targets = [(i["ip"], i["broadcast"], i) for i in network_interfaces()] + [(None, "255.255.255.255", None)]
            for ip, broadcast, itf in targets:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                try:
                    if ip:
                        sock.bind((ip, 0))
                    sock.sendto(message, (broadcast, 44818))
                except OSError:
                    sock.close()
                    continue
                sockets.append(sock)
                if itf:
                    searched.append(itf)
        devices = {}
        end = time.monotonic() + timeout
        while sockets and time.monotonic() < end:
            readable, _, _ = select.select(sockets, [], [], max(0.0, end - time.monotonic()))
            for sock in readable:
                try:
                    data, _ = sock.recvfrom(4096)
                    response = request.response_class(request, data)
                except Exception:
                    continue
                if response and response.identity:
                    d = response.identity
                    devices[d.get("ip_address")] = {k: d.get(k) for k in ("ip_address", "product_name", "vendor",
                                                                          "product_type", "revision", "serial")}
        return {"devices": sorted(devices.values(), key=lambda d: [int(x) for x in str(d["ip_address"]).split(".")]),
                "searched": searched}
    finally:
        for sock in sockets:
            sock.close()
