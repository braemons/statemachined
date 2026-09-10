# SPDX-License-Identifier: GPL-3.0-or-later
"""The tree that is about to be packaged, run.

Every other test in this repository runs the daemon out of a checkout with uv
building the environment. This is the only thing that runs *the interpreter that
is about to be shipped*, with the dependencies that are about to be shipped, and
it is where the failures peculiar to packaging show up: a `uvicorn[standard]`
extra that resolved differently, a web asset that never made it into the wheel,
a launcher whose shebang names the build machine, a native device compiled
against the wrong libstdc++.

Run by packaging/Makefile's `check`, which every package target depends on --
finding this out on a Pi is finding it out too late.
"""

from __future__ import annotations

import sys
import tempfile
from importlib.resources import files
from pathlib import Path


def main() -> int:
    import statemachined

    print(f"  python        {sys.version.split()[0]}")
    print(f"  statemachined {statemachined.__version__}")

    # Every runtime dependency, imported rather than assumed. `websockets` is
    # here on purpose: it arrives only through the `[standard]` extra, and
    # without it a shipped daemon answers a WebSocket upgrade with 404 while
    # every test in the repository still passes, because Starlette's TestClient
    # implements WebSockets in process.
    import fastapi, httpx, pydantic, serial, uvicorn, websockets, zeroconf  # noqa: F401

    print(f"  fastapi {fastapi.__version__}, uvicorn {uvicorn.__version__}, "
          f"pydantic {pydantic.VERSION}, pyserial {serial.__version__}")

    # The application, built the way the systemd unit builds it -- but pointed
    # at a temporary directory, because this runs as whoever is packaging and
    # /var/lib/braemons is the daemon user's.
    from statemachined.daemon.api.application import create_application
    from statemachined.daemon.rig_configuration import RigConfiguration

    with tempfile.TemporaryDirectory() as scratch:
        here = Path(scratch)
        configuration = RigConfiguration(
            connect_on_startup=False,
            graph_store_directory=here / "graphs",
            state_machine_config_directory=here / "configs",
            trace_directory=here / "trace",
            recording_directory=here / "recordings",
        )
        application = create_application(configuration)
        # The OpenAPI schema rather than `app.routes`: routers arrive through
        # include_router and are not flat there, and this is the same view of
        # the API a client gets.
        paths = set(application.openapi()["paths"])
        for expected in ("/api/device", "/api/device/save", "/api/device/autorun", "/api/trial/result", "/api/state"):
            assert expected in paths, f"{expected} is not a route: {sorted(paths)}"
        print(f"  {len(paths)} API paths, including the ones the web UI calls")

    # Package data, which has gone missing from a wheel before and is invisible
    # until a browser asks for it.
    web = files("statemachined.daemon") / "web"
    assert (web / "index.html").is_file(), "the web UI shell is not in the package"
    elements = sorted(p.name for p in (web / "elements").iterdir() if p.name.endswith(".js"))
    assert len(elements) >= 10, f"only {len(elements)} elements: {elements}"
    print(f"  {len(elements)} web elements, shell and stylesheet")

    return the_device_this_package_ships()


def the_device_this_package_ships() -> int:
    """Greet the packaged firmware over the packaged transport.

    The strongest thing this script does, and the cheapest. `statemachined
    device` is what an operator on a fresh install runs when there is no board
    yet, so a package whose device does not start is a package whose first five
    minutes fail -- and that failure is a compiled binary's failure, which the
    Python checks above cannot see at all: a wrong libstdc++, a wrong
    architecture, a `file` that says ELF and an exec that says ENOEXEC.

    A hello and nothing more. Whether the firmware runs trials correctly is
    what the core's own unit tests and the daemon's integration suite answer;
    what is in question here is only whether the bytes in this package can be
    run by this interpreter on this machine.
    """
    from statemachined.device import native_device_on_a_socket as bridge

    binary = bridge.the_native_device_binary()
    if binary is None:
        # Not fatal. packaging/Makefile builds a package without the device
        # when the machine has no compiler, and says so in NO-DEVICE.txt; the
        # release path always has one. Silence here would let that become the
        # normal case without anybody noticing.
        print("  device: none in this tree (built without a compiler)")
        return 0

    from statemachined.device.request_response_session import RequestResponseSession
    from statemachined.device.serial_link import SerialLink

    device = bridge.NativeDeviceOnASocket()
    device.start()
    try:
        link = SerialLink(device.target_url, timeout=5.0)
        with link:
            session = RequestResponseSession(link)
            # A fixed seed, and hex because the device insists: it is the
            # trial RNG's, and nothing here runs a trial.
            acknowledgement = session.hello(seed="00000000")
    finally:
        device.stop()

    scan_hz = acknowledgement.get("scan_hz")
    assert scan_hz, f"the device greeted without a scan rate: {acknowledgement}"
    print(f"  device: {binary.name} greeted over {device.target_url}, scan_hz {scan_hz}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
