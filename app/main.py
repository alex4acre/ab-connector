# SPDX-License-Identifier: MIT

"""ab-connect: Allen-Bradley Logix tags in the ctrlX Data Layer, selected in a web UI.

In the snap: UI at https://<ctrlx>/ab-connect, configuration in the app data of the active
configuration (ab-connect/config.json), Data Layer nodes under ab-connect/<controller>/...

Development without a PLC:  AB_CONNECT_FAKE=1 python3 -m app.main  -> http://127.0.0.1:12348/ab-connect/
(no Data Layer unless DATALAYER_CONNECTION is set and the ctrlX Data Layer library is available)
"""

import os
import signal
import threading
import time

import app.web
from app.core import Core
from app.logix import pycomm3_driver

SNAP_NAME = "ab-connect"
IN_SNAP = os.environ.get("SNAP_NAME") == SNAP_NAME
TCP_PORT = 12348
SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

__close_app = False


def handler(signum, frame):
    global __close_app
    __close_app = True


def log(message: str):
    print(message, flush=True)


def config_path() -> str:
    if not IN_SNAP:
        return os.environ.get("AB_CONNECT_CONFIG", os.path.join(SRC, "data", "config.json"))
    active = os.path.join(os.environ["SNAP_COMMON"], "solutions", "activeConfiguration")
    waited = 0
    while not os.path.isdir(active) and not __close_app:
        if waited % 10 == 0:
            log(f"Waiting for the active configuration {active}")
        time.sleep(1.0)
        waited += 1
    return os.path.join(active, SNAP_NAME, "config.json")


def driver_factory():
    if not IN_SNAP and os.environ.get("AB_CONNECT_FAKE"):
        from app.fake import FakeLogixDriver
        log("Using a simulated Logix controller")
        return FakeLogixDriver
    return pycomm3_driver


def start_datalayer():
    """(system, provider, NodeRegistry) or (None, None, None) without a Data Layer."""
    connection = "ipc://" if IN_SNAP else os.environ.get("DATALAYER_CONNECTION")
    if not connection:
        log("No Data Layer (development mode)")
        return None, None, None
    import ctrlxdatalayer
    from app.provider import NodeRegistry
    system = ctrlxdatalayer.system.System("")
    system.start(False)
    while not __close_app:
        provider = system.factory().create_provider(connection)
        if provider.start() == ctrlxdatalayer.variant.Result.OK:
            return system, provider, NodeRegistry(provider, log=log)
        provider.close()
        log("Starting the Data Layer provider failed, retrying in 5 s")
        time.sleep(5)
    return system, None, None


def start_webserver():
    if IN_SNAP:
        sock_dir = os.path.join(os.environ["SNAP_DATA"], "package-run", SNAP_NAME)
        os.makedirs(sock_dir, exist_ok=True)
        sock_file = os.path.join(sock_dir, "web.sock")
        try:
            os.unlink(sock_file)
        except OSError:
            pass
        server = app.web.UnixSocketServer(sock_file, app.web.RequestHandler)
        log(f"Listening on {sock_file}")
    else:
        server = app.web.TcpServer(("127.0.0.1", TCP_PORT), app.web.RequestHandler)
        log(f"Listening on http://127.0.0.1:{TCP_PORT}{app.web.BASE}/")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main():
    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)

    path = config_path()
    if __close_app:
        return
    system, provider, registry = start_datalayer()
    if __close_app:
        return
    core = Core(path, driver_factory(), registry=registry, log=log)
    core.apply()            # registers saved selections at once; PLC connections start in the background
    log(f"Configuration: {path} ({len(core.config.controllers())} controllers, {len(core.config.tags())} tags)")
    app.web.core = core
    app.web.www_dir = os.path.join(os.environ["SNAP"], "www") if IN_SNAP else os.path.join(SRC, "www")
    server = start_webserver()

    last_check = time.monotonic()
    while not __close_app:
        time.sleep(0.5)
        if provider is not None and not provider.is_connected():
            log("Data Layer provider disconnected")
        if time.monotonic() - last_check > 5:
            last_check = time.monotonic()
            core.reload_if_changed()

    server.shutdown()
    server.server_close()
    core.stop()
    if provider is not None:
        provider.stop()
        provider.close()
    if system is not None:
        system.stop(False)
    log("Stopped")


if __name__ == "__main__":
    main()
