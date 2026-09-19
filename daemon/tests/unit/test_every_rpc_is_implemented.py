# SPDX-License-Identifier: GPL-3.0-or-later
"""Every rpc the proto declares has a servicer method that overrides the stub.

**Inheriting is the failure this catches.** grpcio's generated servicer defines
every method, and each one sets `UNIMPLEMENTED` and returns. A servicer that
simply does not define `ReadFirmware` is a valid Python class, imports cleanly,
registers without complaint, and answers every call to it with an error — so
"it is defined" is not the question. "Is it *this* class's" is.

Read off the descriptor rather than listed, so an rpc added to the proto with
nothing behind it fails here rather than on a rig.
"""

from __future__ import annotations

import pytest

from statemachined._proto.statemachined.v1 import service_pb2, service_pb2_grpc
from statemachined.daemon.api.servicers import SERVICER_CLASSES


def declared() -> dict[str, list[str]]:
    """`{service: [rpc, ...]}`, from the descriptor."""
    return {
        name: [method.name for method in descriptor.methods]
        for name, descriptor in service_pb2.DESCRIPTOR.services_by_name.items()
    }


def test_the_descriptor_is_readable() -> None:
    """A reader that found nothing would make every test below vacuous."""
    services = declared()
    assert len(services) == 8
    assert sum(len(rpcs) for rpcs in services.values()) == 50


def test_every_service_has_a_servicer() -> None:
    assert set(declared()) == set(SERVICER_CLASSES)


@pytest.mark.parametrize("service_name", sorted(declared()))
def test_every_rpc_is_overridden(service_name: str) -> None:
    servicer = SERVICER_CLASSES[service_name]
    base = getattr(service_pb2_grpc, f"{service_name}Servicer")

    missing = []
    for rpc in declared()[service_name]:
        implementation = getattr(servicer, rpc, None)
        if implementation is None or implementation is getattr(base, rpc, None):
            missing.append(rpc)
    assert not missing, (
        f"{servicer.__name__} inherits these from the generated stub, so they answer "
        f"UNIMPLEMENTED:\n" + "\n".join(f"  {rpc}" for rpc in missing)
    )


def test_a_servicer_that_forgot_one_is_caught() -> None:
    """The check, checked: a class that inherits an rpc must fail it."""

    class Forgetful(service_pb2_grpc.ConfigurationServicer):
        async def ReadConfiguration(self, request, context): ...
        async def PatchConfiguration(self, request, context): ...

    inherited = [
        rpc
        for rpc in declared()["Configuration"]
        if getattr(Forgetful, rpc) is getattr(service_pb2_grpc.ConfigurationServicer, rpc)
    ]
    assert inherited == ["ReadHealth"]


def test_every_servicer_takes_the_service_and_nothing_else() -> None:
    """One constructor shape, so `build_servicers` can build them all."""
    import inspect

    for name, servicer in SERVICER_CLASSES.items():
        parameters = list(inspect.signature(servicer.__init__).parameters)
        assert parameters == ["self", "service"], f"{name} takes {parameters}"
