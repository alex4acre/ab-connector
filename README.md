# ab-connect

ctrlX CORE app: browse the tags of Allen-Bradley ControlLogix / CompactLogix controllers in a web UI, select the ones you need, and use them as typed nodes in the ctrlX Data Layer.

It's a rewrite of [datalayer.ab / cip-connect](https://github.com/S-Gilk/datalayer.ab). Open it from the ctrlX sidebar (**ab-connect**), or at `https://<ctrlx>/ab-connect/`.

## Using it

1. **Add a controller.** Give it a name and an address: the IP address, plus the CPU slot for a ControlLogix chassis (e.g. `192.168.1.21/2`). To find it:
   - **Find on network** broadcasts a ListIdentity request on **every** network interface of the ctrlX, to that interface's broadcast address, and lists the EtherNet/IP devices that answer.
   - **Scan** asks every address of a range directly (e.g. `192.168.1.0/24`, at most 1024 addresses), for networks a broadcast doesn't reach, such as behind a router or from a virtual ctrlX.
2. **Browse.** The tree loads one level at a time: controller tags and programs, then tags, then UDT members or array elements. Large arrays come in pages of 100. The eye icon reads a node's current value.
3. **Select.** Set the mode, rate and write access in the bar above the tree, then click **＋** on a tag, member, element, array or whole UDT.
4. **Adjust.** In **Selected tags**, change the mode, rate or write access per tag, watch the live values, or remove tags.

Drag the divider to the right of the controller pane to make it wider or narrower (double-click resets it). The browser remembers the width.

**Reload tag definitions** fetches the PLC's tag list again, e.g. after a program download that changed tags.

## In the Data Layer

```
ab-connect/<controller>/connected                         bool, PLC connected
ab-connect/<controller>/status                            JSON: connection, scan groups (timing, overruns)
ab-connect/<controller>/controller/<Tag>/<Member>/...     controller-scoped tags
ab-connect/<controller>/programs/<Program>/<Tag>/...      program-scoped tags
```

- **A plain value** (DINT, REAL, STRING, BOOL, …) is one node of the matching type.
- **An array of plain values** is one array node, e.g. `REAL[6]` becomes an array of 6 `float32` values, read in one request.
- **A UDT** becomes a folder of its members. **An array of UDTs** becomes numbered folders: `Stations/0/...`, `Stations/1/...`.
- **A selected structure is read whole:** a UDT, an array of UDTs, or a predefined type such as `MOTION_INSTRUCTION` is read in **one request**, and its values are taken from that. Some members of predefined types (e.g. `DEAD_ZONE1` of a motion instruction) can't be read by name; this way they are read too. When a single member is selected, it's read directly. If the controller refuses it by name, it's read through its parent structure instead. If a structure read ever fails, its values are read one by one.
- **One element** of an array can be selected on its own, e.g. `arTemps/3`. For multi-dimensional arrays, `aiGrid[1,2]` becomes `aiGrid/1_2`.
- **Node metadata** carries the type (e.g. `types/datalayer/array-of-float32`), so OPC UA and other clients see proper types. Nodes not marked writable refuse writes.

Type mapping: `BOOL`→`bool8`, `SINT/INT/DINT/LINT`→`int8/16/32/64`, `USINT/UINT/UDINT/ULINT`→`uint8/16/32/64`, `REAL`→`float32`, `LREAL`→`float64`, strings (`STRING` and custom string types)→`string`. Logix BOOL arrays, which pycomm3 reports as `DWORD`, become arrays of `bool8`.

### Cyclic or on request

- **Cyclic:** the app reads the tag at its rate, and a Data Layer read returns the latest value at once. Tags with the same rate form a scan group that's read in one batched request. If a read takes longer than the period, the next one starts right away and an overrun is counted (in the UI and `status`); missed cycles are skipped, not queued.
- **On request:** the PLC is read when someone reads the node, and the Data Layer gets its answer when the value arrives, without blocking other reads. Requests arriving together share one PLC read, and a value younger than the tag's rate is served from the cache.

The minimum rate is 50 ms, and the UI warns below 100 ms. Throughput depends on the PLC's communication load, including its *System Overhead Time Slice*.

## Why it starts fast

- The PLC connection is opened **without uploading any tag definitions**. pycomm3 normally uploads all of them on connect, which is what made the old app slow.
- Definitions are uploaded **per scope** (controller, or one program), only when you browse that scope or a selected tag lives in it.
- Selected tags are stored **with their types**. At startup the Data Layer nodes are registered **straight away from the saved selection, without contacting the PLC**. Until the PLC is reachable, reads report an error and `connected` is false.
- One connection and one worker thread per PLC; the old app used three connections per PLC.

## Configuration

`ab-connect/config.json` in the app data of the active configuration:
- it's saved with configurations and backups;
- you can edit or replace it via Manage app data;
- changes are picked up within 5 s, and immediately when a configuration is loaded.

Logs go to the system journal, not the app data.

```json
{"version": 1,
 "controllers": [{"name": "Line1", "address": "192.168.1.21", "enabled": true}],
 "tags": [{"controller": "Line1", "scope": "controller", "path": "Station1.Pos", "node": "controller/Station1/Pos",
           "type": "REAL", "dl_type": "float32", "count": 6, "mode": "cyclic", "rate_ms": 500, "writable": false}]}
```

`scope` is `controller` or `program:<name>`. `path` is the Logix tag path, e.g. `Tag`, `Tag.Member`, `Arr[3]`, `Grid[1,2]`.

## Limits

- **Controllers:** ControlLogix and CompactLogix only. SLC 500 / PLC-5 / MicroLogix (data-file addressing) and Micro800 (no tag-list upload) would need another driver.
- **The old `cip-connect` configuration isn't imported.**

## Develop and test

```bash
python3 -m unittest discover -s . -p "test_*.py"     # PLC layer, scan engine, Data Layer provider (simulated PLC)
AB_CONNECT_FAKE=1 python3 -m app.main                # UI with a simulated Logix PLC: http://127.0.0.1:12348/ab-connect/
python3 tests/ui/ui_check.py <folder>                # end-to-end UI check in headless Firefox (see the script)
```

`app/fake.py` simulates a Logix controller with UDTs, strings, BOOL arrays, multi-dimensional arrays, programs and changing values. Its tag definitions have pycomm3's structure.

## Build

```bash
snapcraft pack --build-for=amd64 --use-lxd   # ctrlX COREvirtual
snapcraft pack --build-for=arm64 --use-lxd   # ctrlX CORE X3/X5/X7
```

Uses [pycomm3](https://github.com/ottowayi/pycomm3) 1.2.16 (MIT).
