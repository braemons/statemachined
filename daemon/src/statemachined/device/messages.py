# SPDX-License-Identifier: LGPL-3.0-or-later
"""The protocol's vocabulary, as enums rather than string literals.

The mirror of firmware/core/protocol/msg_type.h. `StrEnum` members *are* their
wire strings, so a parsed message compares directly against one
(`msg[Field.MSG_TYPE] == MsgType.HELLO_ACK`) with no `.value` anywhere, and a
misspelling is an AttributeError at the call site instead of a command the
device answers with `unknown_type`.

Not shared with emulation/tests/statemachined_protocol.py, deliberately. That
module is a second implementation written from dev/PROTOCOL.md so that a test
asks the device an independent question; handing it this vocabulary would make
both ends agree by construction, which is exactly the agreement the emulator
test exists to not assume.
"""

from __future__ import annotations

from enum import StrEnum


class Field(StrEnum):
    """The three members the framing itself owns.

    `MESSAGE_ID` is emphatically not a sequence number -- it orders nothing, a
    gap in it is not an error, and its whole job is letting a resend be
    recognised as one. See dev/PROTOCOL.md §1.2.
    """

    MSG_TYPE = "msg_type"
    MESSAGE_ID = "message_id"
    IN_REPLY_TO = "in_reply_to"
    CRC = "crc"


class MsgType(StrEnum):
    """Every `msg_type` on the wire, in both directions."""

    # Host -> device.
    HELLO = "hello"
    GRAPH_BEGIN = "graph_begin"
    GRAPH_DIST = "graph_dist"
    GRAPH_STATE = "graph_state"
    GRAPH_TRANSITION = "graph_transition"
    GRAPH_ACTION = "graph_action"
    GRAPH_END = "graph_end"
    CONFIGURE = "configure"
    START = "start"
    CANCEL = "cancel"
    PING = "ping"
    STATE = "state"

    # Device -> host.
    HELLO_ACK = "hello_ack"
    ACK = "ack"
    GRAPH_OK = "graph_ok"
    ARMED = "armed"
    STARTED = "started"
    CANCEL_ACK = "cancel_ack"
    RESULT_BEGIN = "result_begin"
    RESULT_PATH = "result_path"
    RESULT_END = "result_end"
    EVENT = "event"
    ERROR = "error"
    LOG = "log"
    PONG = "pong"
    STATE_REPORT = "state_report"


#: Device messages that answer nothing and may arrive at any time, so a reader
#: waiting for a reply routes them aside instead of mistaking one for its
#: answer.
UNSOLICITED = frozenset(
    {
        MsgType.EVENT,
        MsgType.LOG,
        MsgType.RESULT_BEGIN,
        MsgType.RESULT_PATH,
        MsgType.RESULT_END,
    }
)


class ErrorCode(StrEnum):
    """The `code` in an `error`. The bridge switches on this; the human
    `message` and the `context` naming what to change are for reading."""

    BAD_CRC = "bad_crc"
    TOO_LONG = "too_long"
    BAD_JSON = "bad_json"
    UNKNOWN_TYPE = "unknown_type"
    BAD_PROTO = "bad_proto"
    NOT_READY = "not_ready"
    BAD_ORDER = "bad_order"
    BAD_INDEX = "bad_index"
    TOO_MANY = "too_many"
    BAD_GRAPH = "bad_graph"
    GRAPH_MISMATCH = "graph_mismatch"
    UNKNOWN_TRIAL = "unknown_trial"
    BUSY = "busy"
    INTERNAL = "internal"
