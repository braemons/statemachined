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

    # Every runtime dependency, imported rather than assumed.
    import grpc, pydantic, serial, uvicorn, zeroconf  # noqa: F401

    print(f"  grpcio {grpc.__version__}, uvicorn {uvicorn.__version__}, "
          f"pydantic {pydantic.VERSION}, pyserial {serial.__version__}")

    # The rpcs, built the way the systemd unit builds them -- but pointed at a
    # temporary directory, because this runs as whoever is packaging and
    # /var/lib/braemons is the daemon user's.
    from statemachined.daemon.api.rig_service import RigService
    from statemachined.daemon.api.servicers import build_servicers
    from statemachined.daemon.api.web_edge import rpcs_of
    from statemachined.daemon.rig_configuration import RigConfiguration

    with tempfile.TemporaryDirectory() as scratch:
        here = Path(scratch)
        configuration = RigConfiguration(
            device_target="loop://",
            connect_on_startup=False,
            graph_store_directory=here / "graphs",
            state_machine_config_directory=here / "configs",
            trace_directory=here / "trace",
            recording_directory=here / "recordings",
        )
        service = RigService(configuration)
        # The edge's own table rather than a list written here: it is built
        # from the proto descriptor, so this asks the package the same question
        # a browser does -- is every rpc in the interface actually implemented
        # in this tree -- and an rpc added to the proto is covered by itself.
        table = rpcs_of(build_servicers(service))
        for expected in (
            "/statemachined.v1.Device/ReadDevice",
            "/statemachined.v1.Device/SaveSettings",
            "/statemachined.v1.Device/WriteAutorun",
            "/statemachined.v1.Trial/ReadResult",
            "/statemachined.v1.State/ReadState",
        ):
            assert expected in table, f"{expected} is not an rpc: {sorted(table)}"
        print(f"  {len(table)} rpcs, including the ones the web UI calls")

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
