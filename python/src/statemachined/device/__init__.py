# SPDX-License-Identifier: LGPL-3.0-or-later
"""Driving a board yourself: the wire, and the class that speaks it.

The `[device]` tier. `StatemachinedDevice` opens the port, greets the board,
pushes the wiring, uploads a graph set and runs trials -- everything a daemon
does to a device, without a daemon in the way. It is what a bench script wants,
and it is the same class the daemon drives, so a paradigm that runs here runs on
a rig.

```python
from statemachined.device import StatemachinedDevice
from statemachined.model.graph_definition import GraphDefinition
from statemachined.model.line_map import LineMap

board = StatemachinedDevice(
    "/dev/ttyACM0",
    line_map=LineMap.model_validate(wiring),
)
board.connect_and_greet()
board.push_wiring()
board.upload_graph_set([GraphDefinition.model_validate(go_nogo)], set_version=1)

result = board.run_trial_to_completion(1, "go-nogo", cap_milliseconds=30_000)
# `.name`, not the member: TrialOutcome is an IntEnum and since Python 3.11
# those print as their number. The `.tdr` taxonomy crosses the wire by name.
print(result.outcome.name, result.total_duration_microseconds)
```

**Greeting a board takes the rig, and this is the second thing that can do it.**
The greeting is the handover: a board that was arming its own trials stops until
it is told to again, deliberately, so that a daemon which crashed cannot leave a
board rewarding an animal nobody is watching. Which means that on a rig box with
`statemachined serve` running, this class is a *second* claimant on the same
board. Over a tty the port is busy and you find out immediately; over
`socket://` you will not -- the connection succeeds and two hosts are then
driving a link whose whole design is one command in flight (dev/PROTOCOL.md
§1.2). Do not point this at a board a daemon is holding. If something else on
the network should own it, talk to that instead: `statemachined.client`.

**What is not here, and is not an oversight.** No trace ring, no recordings, no
graph store, no observer list -- see `statemachined.daemon`. Results and state
visits arrive through the callbacks this class was constructed with, or through
`wait_for_trial_result`, and whatever you do not keep is gone. That is the
honest shape of being the only listener.
"""

def _the_extra_that_is_missing(module: str, extra: str, needs: str) -> str:
    return (
        f"statemachined.{module} needs {needs}, which is the `{extra}` extra and is not "
        f"installed.\n"
        f"  pip install 'statemachined[{extra}]'   (or: uv add 'statemachined[{extra}]')\n"
        f"This package is tiered on purpose: the base is the documents and the HTTP "
        f"client, so something that only talks to a daemon never installs a serial "
        f"library or a web framework."
    )

try:
    from .statemachined_device import (
        DeviceNotConnected,
        NoGraphSetCommitted,
        ObservedStateVisit,
        StatemachinedDevice,
    )
except ModuleNotFoundError as missing:  # pragma: no cover - an install-shape error
    # `No module named 'serial'` is true and useless. Every other refusal in this
    # project names what to change, and an import is where somebody meets the
    # tiering for the first time.
    if missing.name not in {"serial", "serial.tools"}:
        raise
    raise ModuleNotFoundError(
        _the_extra_that_is_missing("device", "device", "pyserial")
    ) from missing

__all__ = [
    "DeviceNotConnected",
    "NoGraphSetCommitted",
    "ObservedStateVisit",
    "StatemachinedDevice",
]
