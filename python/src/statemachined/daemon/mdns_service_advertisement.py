# SPDX-License-Identifier: LGPL-3.0-or-later
"""Telling the network this rig exists. docs/developer/daemon.md §5.

vstimd advertises `_vstimd._tcp` with a stable `id=` TXT record *"so clients can
match a device across name collisions"*. This is the same record for
`_statemachined._tcp`, and it exists for the same reason: a Pi whose hostname is
generated at boot cannot be hand-configured into a console's URL list, and two
rigs called `raspberrypi.local` is the normal case rather than the pathological
one.

**The identity is not the hostname.** `/etc/machine-id` survives a rename and a
DHCP lease and is different on every box, so a console that has seen this rig
before recognises it after somebody renames it. It is hashed rather than
published: a machine-id is meant to be treated as confidential, and what a
console needs is a *stable* identifier, not that particular one.

**Advertising is never fatal.** A daemon whose Avahi is not running, whose
network is down, or which cannot resolve an address of its own still owns a
device and still serves the API somebody may reach by IP. So every failure here
is logged and swallowed, and `is_advertising` says which happened rather than
leaving a caller to infer it.
"""

from __future__ import annotations

import hashlib
import socket
from dataclasses import dataclass, field
from pathlib import Path

#: The service type. `_statemachined._tcp` beside vstimd's `_vstimd._tcp` and
#: triald's, so one browse of the local domain finds every braemons daemon on a
#: rig.
SERVICE_TYPE = "_statemachined._tcp.local."

MACHINE_ID_PATH = Path("/etc/machine-id")


def stable_rig_identifier(machine_id_path: Path = MACHINE_ID_PATH) -> str:
    """Sixteen hex digits that mean "this box", across renames and reboots.

    Falls back to the hostname when there is no machine-id -- a container, a
    Mac, a developer's checkout. That is a weaker promise (a rename changes it),
    and it is the right fallback anyway: something stable-ish beats a value that
    changes every restart, which would make a console show one rig as many.
    """
    try:
        seed = machine_id_path.read_text().strip()
    except OSError:
        seed = ""
    if not seed:
        seed = socket.gethostname()
    return hashlib.sha256(f"statemachined:{seed}".encode()).hexdigest()[:16]


def text_records_for(
    *,
    rig_identifier: str,
    port: int,
    device_target: str,
    version: str,
) -> dict[bytes, bytes]:
    """What a console can act on without opening a connection first.

    Deliberately small. A TXT record is not an API: everything a console needs
    beyond "which rig, where, and can I speak its version" is one `GET
    /api/device` away, and duplicating that here would give a browser two
    answers that disagree the moment a board is unplugged.

    `path` is here because the elements contract (docs/developer/daemon.md §5) is a URL a
    console has to construct, and constructing it from a convention rather than
    from the record is how a console breaks when the daemon is behind a proxy.
    """
    return {
        b"id": rig_identifier.encode(),
        b"version": version.encode(),
        b"api": b"/api",
        b"elements": b"/elements/statemachined.js",
        b"device": device_target.encode(),
        b"port": str(port).encode(),
    }


@dataclass
class MdnsServiceAdvertisement:
    """One `_statemachined._tcp` registration, for as long as the daemon runs.

    Started with the app and unregistered with it, because a record that
    outlives its daemon is worse than no record: a console shows a rig that
    cannot answer, and the person believes the rig is broken rather than absent.
    """

    port: int
    device_target: str = ""
    version: str = "0.0.0"
    instance_name: str = ""
    #: Where a failure goes. Print by default, so that a daemon started by hand
    #: says why it is not discoverable instead of being quietly invisible.
    report: object = print

    is_advertising: bool = field(default=False, init=False)
    last_failure: str = field(default="", init=False)
    _zeroconf: object | None = field(default=None, init=False)
    _service_info: object | None = field(default=None, init=False)

    def service_name(self) -> str:
        """`<instance>._statemachined._tcp.local.`

        The hostname by default, because that is what somebody standing next to
        the rig would call it. Collisions are Zeroconf's problem and the `id=`
        record is what survives its answer.
        """
        instance = self.instance_name or socket.gethostname().split(".")[0]
        return f"{instance}.{SERVICE_TYPE}"

    def build_service_info(self):
        """The record, as zeroconf wants it. Separate so a test can read it."""
        from zeroconf import ServiceInfo

        rig_identifier = stable_rig_identifier()
        return ServiceInfo(
            SERVICE_TYPE,
            self.service_name(),
            port=self.port,
            properties=text_records_for(
                rig_identifier=rig_identifier,
                port=self.port,
                device_target=self.device_target,
                version=self.version,
            ),
            server=f"{socket.gethostname().split('.')[0]}.local.",
        )

    def start(self) -> bool:
        """Register. Returns whether it worked; never raises."""
        try:
            from zeroconf import Zeroconf

            self._service_info = self.build_service_info()
            self._zeroconf = Zeroconf()
            self._zeroconf.register_service(self._service_info)
        except Exception as exc:  # noqa: BLE001 -- see the module docstring
            self.last_failure = f"{type(exc).__name__}: {exc}"
            self._report(
                f"mDNS: not advertising ({self.last_failure}). "
                "The API is still served; a console will need the address by hand."
            )
            self._close_quietly()
            return False
        self.is_advertising = True
        return True

    def stop(self) -> None:
        """Withdraw the record, then close. Also never raises."""
        if self._zeroconf is not None and self._service_info is not None:
            try:
                self._zeroconf.unregister_service(self._service_info)
            except Exception as exc:  # noqa: BLE001
                self.last_failure = f"{type(exc).__name__}: {exc}"
        self._close_quietly()
        self.is_advertising = False

    def _close_quietly(self) -> None:
        if self._zeroconf is not None:
            try:
                self._zeroconf.close()
            except Exception:  # noqa: BLE001
                pass
        self._zeroconf = None
        self._service_info = None

    def _report(self, text: str) -> None:
        if callable(self.report):
            self.report(text)
