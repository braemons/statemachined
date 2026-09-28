# SPDX-License-Identifier: LGPL-3.0-or-later
"""The board's link, spoken directly: no daemon, no gRPC, no heartbeat.

The hardware suite talks to the board through the daemon, which is right for a
test of what a session gets and wrong for a measurement of what the *board*
costs: every figure it takes includes the daemon's own pings and its gRPC hop.
This is the other half -- the wire in docs/reference/protocol.md §1, as little
of it as a benchmark needs.

    COBS( header || protobuf body || CRC-16/CCITT-FALSE, big-endian ) || 0x00

    header: type (u8, the body's field number in the envelope's oneof),
            flags (u8, bit 0: in_reply_to follows), message_id (u16 LE),
            in_reply_to (u16 LE, when flagged)

Decoded messages are handed back inside the envelope message all the same --
`reply.state_report`, `reply.WhichOneof("body")` -- because it is a convenient
container; it is never what crosses the wire.

The protobuf bindings are generated from proto/statemachined/link/v1/link.proto
when this module loads, into a temporary directory, and loaded under a private
name. They are not committed: nothing but this suite speaks the link from
Python, and a second committed copy of the board's wire is one more thing to
drift.

No pyserial either. USB CDC ignores the baud rate, so a raw termios file
descriptor is the whole of what a serial library would add.
"""

from __future__ import annotations

import importlib.util
import os
import select
import struct
import sys
import tempfile
import termios
import time
from dataclasses import dataclass
from pathlib import Path

#: The repository root, three levels up from this file's directory.
REPOSITORY = Path(__file__).resolve().parents[4]
LINK_PROTO = REPOSITORY / "proto" / "statemachined" / "link" / "v1" / "link.proto"

#: `kProtocolVersion` in firmware/core/protocol/host_link_session.cpp.
PROTOCOL_VERSION = 2


def _load_link_pb2():
    from grpc_tools import protoc

    out = Path(tempfile.mkdtemp(prefix="statemachined-link-pb2-"))
    status = protoc.main(
        [
            "protoc",
            f"--proto_path={LINK_PROTO.parents[3]}",
            f"--python_out={out}",
            str(LINK_PROTO.relative_to(LINK_PROTO.parents[3])),
        ]
    )
    if status != 0:
        raise RuntimeError(f"protoc could not compile {LINK_PROTO}")
    generated = out / "statemachined" / "link" / "v1" / "link_pb2.py"
    # A private module name, never `statemachined.*`: that is the daemon's
    # package, and on a rig box it may well be installed.
    spec = importlib.util.spec_from_file_location("_statemachined_perf_link_pb2", generated)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


pb = _load_link_pb2()


# ------------------------------------------------------------------ framing ---


def crc16_ccitt_false(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return crc


def cobs_encode(data: bytes) -> bytes:
    out = bytearray()
    block = bytearray()
    for byte in data:
        if byte == 0:
            out.append(len(block) + 1)
            out += block
            block.clear()
        else:
            block.append(byte)
            if len(block) == 254:
                out.append(255)
                out += block
                block.clear()
    out.append(len(block) + 1)
    out += block
    return bytes(out)


def cobs_decode(data: bytes) -> bytes:
    out = bytearray()
    i = 0
    while i < len(data):
        code = data[i]
        if code == 0 or i + code > len(data):
            raise ValueError("bad COBS")
        out += data[i + 1 : i + code]
        i += code
        if code < 255 and i < len(data):
            out.append(0)
    return bytes(out)


FLAG_IN_REPLY_TO = 0x01


def frame(field: str, body, message_id: int) -> bytes:
    """A command's frame: header, the body alone, CRC, COBS, delimiter."""
    type_number = pb.HostMessage.DESCRIPTOR.fields_by_name[field].number
    payload = struct.pack("<BBH", type_number, 0, message_id) + body.SerializeToString()
    return cobs_encode(payload + struct.pack(">H", crc16_ccitt_false(payload))) + b"\x00"


@dataclass
class Received:
    message_id: int
    in_reply_to: int | None
    #: The body, inside a DeviceMessage so `WhichOneof("body")` names it.
    message: object


def unframe(encoded: bytes) -> Received:
    raw = cobs_decode(encoded)
    payload, crc = raw[:-2], struct.unpack(">H", raw[-2:])[0]
    if crc16_ccitt_false(payload) != crc:
        raise ValueError("bad CRC")
    type_number, flags, message_id = struct.unpack_from("<BBH", payload)
    at = 4
    in_reply_to = None
    if flags & FLAG_IN_REPLY_TO:
        (in_reply_to,) = struct.unpack_from("<H", payload, at)
        at += 2
    field = pb.DeviceMessage.DESCRIPTOR.fields_by_number[type_number]
    message = pb.DeviceMessage()
    getattr(message, field.name).ParseFromString(payload[at:])
    # ParseFromString on an empty body does not select the oneof member.
    getattr(message, field.name).SetInParent()
    return Received(message_id, in_reply_to, message)


# ---------------------------------------------------------------- the link ---


@dataclass
class Exchange:
    """One command and its answer, timed on the host."""

    reply: object
    round_trip_s: float
    #: Frames the board sent unasked while this one was in flight.
    unsolicited: list


class Link:
    """A greeted link to one board. Strictly one command in flight.

    Everything the board sends unasked -- a `visit`, a trial's result frames, a
    `log` -- is kept in `unsolicited` on the exchange it arrived during, so a
    benchmark can count it rather than having it mistaken for a reply.
    """

    def __init__(self, path: str, *, seed: int = 1):
        self.path = path
        self.fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        attributes = termios.tcgetattr(self.fd)
        # cfmakeraw, by hand: no echo, no line discipline, no CR/LF mapping.
        attributes[0] = 0
        attributes[1] = 0
        attributes[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
        attributes[3] = 0
        attributes[6][termios.VMIN] = 0
        attributes[6][termios.VTIME] = 0
        termios.tcsetattr(self.fd, termios.TCSANOW, attributes)
        termios.tcflush(self.fd, termios.TCIOFLUSH)
        self._buffer = bytearray()
        self._message_id = 0
        # Resynchronise the receiver: back-to-back delimiters are not a frame.
        os.write(self.fd, b"\x00\x00")
        self.hello_ack = self.command(
            "hello", pb.Hello(proto=PROTOCOL_VERSION, seed=seed)
        ).reply
        if self.hello_ack.WhichOneof("body") != "hello_ack":
            raise RuntimeError(f"the board answered hello with {self.hello_ack}")

    def close(self) -> None:
        os.close(self.fd)

    def __enter__(self) -> Link:
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # -- frames --

    def _read_frame(self, deadline: float):
        while True:
            end = self._buffer.find(0)
            if end >= 0:
                encoded = bytes(self._buffer[:end])
                del self._buffer[: end + 1]
                if encoded:
                    return unframe(encoded)
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            ready, _, _ = select.select([self.fd], [], [], remaining)
            if ready:
                chunk = os.read(self.fd, 4096)
                if chunk:
                    self._buffer += chunk

    def send(self, field: str, body) -> int:
        self._message_id = (self._message_id + 1) & 0xFFFF
        encoded = frame(field, body, self._message_id)
        view = memoryview(encoded)
        while view:
            try:
                written = os.write(self.fd, view)
            except BlockingIOError:
                select.select([], [self.fd], [], 1.0)
                continue
            view = view[written:]
        return self._message_id

    def command(self, field: str, body, *, timeout_s: float = 2.0) -> Exchange:
        """Send one command and wait for the frame that answers it."""
        started = time.perf_counter()
        message_id = self.send(field, body)
        deadline = time.monotonic() + timeout_s
        unsolicited = []
        while True:
            reply = self._read_frame(deadline)
            if reply is None:
                raise TimeoutError(f"no reply to {field} (message_id {message_id})")
            if reply.in_reply_to == message_id:
                return Exchange(reply.message, time.perf_counter() - started, unsolicited)
            unsolicited.append(reply.message)

    def drain(self, quiet_s: float = 0.2) -> list:
        """Everything the board sends until it has been quiet for `quiet_s`."""
        frames = []
        while True:
            one = self._read_frame(time.monotonic() + quiet_s)
            if one is None:
                return frames
            frames.append(one.message)

    # -- the commands a benchmark uses --

    def ping(self) -> Exchange:
        return self.command("ping", pb.Ping())

    def state(self) -> Exchange:
        return self.command("state", pb.StateRequest())

    def scan(self):
        """The board's state_report, for its scan counters (`overruns`,
        `worst_gap`, `tx_stalls`), which are flat in it."""
        return self.state().reply.state_report

    def pins(self, direction: int) -> Exchange:
        return self.command("pins", pb.Pins(dir=direction))

    def autorun_query(self) -> Exchange:
        # Every field absent makes `autorun` a question: nothing changes.
        return self.command("autorun", pb.Autorun())

    def profile(self, *, reset: bool = False):
        """The board's ProfileReport; `enabled` is false on a build without it."""
        return self.command("profile", pb.Profile(reset=reset)).reply.profile_report

    # -- trials, on a set a daemon committed earlier (it survives a reconnect) --

    def configure(self, trial_id: int, *, graph_index: int, cap_ms: int) -> Exchange:
        set_version = self.hello_ack.hello_ack.set_version
        return self.command(
            "configure",
            pb.Configure(
                trial_id=trial_id,
                set_version=set_version,
                graph_index=graph_index,
                cap_ms=cap_ms,
            ),
        )

    def start(self, trial_id: int) -> Exchange:
        return self.command("start", pb.Start(trial_id=trial_id))

    def cancel(self, trial_id: int) -> Exchange:
        return self.command("cancel", pb.Cancel(trial_id=trial_id))

    def wait_for_result(self, trial_id: int, *, timeout_s: float) -> list:
        """Everything the board sends until the trial's `result_end`."""
        frames = []
        deadline = time.monotonic() + timeout_s
        while True:
            one = self._read_frame(deadline)
            if one is None:
                raise TimeoutError(f"no result for trial {trial_id}")
            message = one.message
            frames.append(message)
            if (
                message.WhichOneof("body") == "result_end"
                and message.result_end.trial_id == trial_id
            ):
                return frames
