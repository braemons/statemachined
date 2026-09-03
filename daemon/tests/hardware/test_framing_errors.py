# SPDX-License-Identifier: GPL-3.0-or-later
"""The line layer, exercised with lines a well-behaved host would never send.

Every one of these is checked on the host too, against the same core code. What
only a board can answer is whether the *device* survives them: a firmware that
handles a 600-byte line correctly in a unit test and overruns a 512-byte buffer
on silicon is a firmware that passes the host suite.

The rule these all turn on is that a line is verified before it is parsed. A
half-parsed `graph_state` acted on in part is the failure the whole line layer
exists to prevent, so a line that fails its CRC must produce a refusal and
nothing else -- no state change, no partial effect.
"""

from __future__ import annotations

from statemachined.device.message_vocabulary import ErrorCode, Field, MsgType
from statemachined.device.message_framing import command_line, statemachined_line


def test_a_corrupted_line_is_refused_and_counted(device):
    """One flipped byte in the CRC, which is what a noisy cable produces."""
    before = device.state()["bad_lines"]

    good = command_line(MsgType.PING, device.next_message_id())
    # The CRC is the last four hex digits before `"}`. Change one, leave the
    # rest of the line perfectly valid: the point is that the device rejects it
    # on arithmetic, before it has parsed anything.
    digit = good[-3]
    corrupt = good[:-3] + ("0" if digit != "0" else "1") + good[-2:]
    device.send_line(corrupt)

    error = device.read_error()
    assert error["code"] == ErrorCode.BAD_CRC
    # No message_id: the line never verified, so there is nothing to attribute
    # it to. Reported anyway -- a host waiting for an answer it will never get
    # is worse than one told its line was unusable.
    assert Field.IN_REPLY_TO not in error

    assert device.state()["bad_lines"] == before + 1, "a bad line was refused but not counted"


def test_an_over_long_line_is_refused_rather_than_truncated(device):
    """The failure that would otherwise be a buffer overrun.

    A device that silently truncated would then parse the front half of a
    message and act on it, which is the same class of failure as acting on a
    line with a bad CRC.
    """
    max_line = device.ack["caps"]["max_line"]
    device.send_line("x" * (max_line + 64))

    error = device.read_error()
    assert error["code"] == ErrorCode.TOO_LONG

    # Still answering afterwards is half the test: the reader has to resynchronise
    # on the next newline rather than stay confused about where it is.
    assert device.request(MsgType.PING)[Field.MSG_TYPE] == MsgType.PONG


def test_a_line_that_passes_its_crc_but_is_not_json_is_refused(device):
    """A good CRC over bad bytes. The CRC says "arrived intact", not "means something"."""
    device.send_line(statemachined_line('{"msg_type":"ping",'))
    assert device.read_error()["code"] == ErrorCode.BAD_JSON
    assert device.request(MsgType.PING)[Field.MSG_TYPE] == MsgType.PONG


def test_a_message_with_no_message_id_is_refused_by_name(device):
    """Valid JSON, valid CRC, and unanswerable: there is nothing to reply to."""
    device.send_line(statemachined_line('{"msg_type":"ping"'))
    error = device.read_error()
    assert error["code"] == ErrorCode.BAD_JSON
    assert error["context"] == "message_id"


def test_an_unrecognised_message_type_is_refused_with_its_id(device):
    """Unlike the cases above, this line verified -- so the refusal names it."""
    message_id = device.next_message_id()
    device.send_line(command_line("teleport", message_id))
    error = device.read_error()
    assert error["code"] == ErrorCode.UNKNOWN_TYPE
    assert error[Field.IN_REPLY_TO] == message_id


def test_a_type_the_device_only_ever_sends_is_not_a_command(device):
    """`pong` from a host is as unrecognisable as `teleport`.

    Worth asserting separately because these names *are* in the device's
    vocabulary; the risk is a dispatch that matches on the name without asking
    which direction it travels.
    """
    error = device.refuse(MsgType.PONG)
    assert error.code == ErrorCode.UNKNOWN_TYPE


def test_members_the_device_does_not_know_are_ignored(device):
    """Forward compatibility, so a newer host can talk to an older board.

    The alternative -- refusing anything unrecognised -- would mean every field
    added to any message broke every device in a rack until it was reflashed.
    """
    message_id = device.next_message_id()
    answer = device.exchange(
        command_line(MsgType.PING, message_id, invented_by_a_newer_host={"nested": [1, 2]})
    )
    assert answer[Field.MSG_TYPE] == MsgType.PONG, f"an unknown member was refused: {answer}"
    assert answer[Field.IN_REPLY_TO] == message_id


def test_a_resent_command_gets_the_first_answer_back_verbatim(device):
    """The one-deep duplicate guard, which is what makes a blind retry safe.

    `ping` is the sharpest probe available: its reply carries `up_us`, which
    moves continuously. A second answer with the *same* `up_us` can only be the
    cached line -- if the device had re-executed the command, the clock would
    have moved between the two.
    """
    line = command_line(MsgType.PING, device.next_message_id())

    first = device.exchange(line)
    assert first[Field.MSG_TYPE] == MsgType.PONG
    second = device.exchange(line)  # the same bytes, as a retry after a lost reply

    assert second == first, "a resend was executed again instead of answered from the cache"


def test_message_id_zero_is_a_message_id_like_any_other(device):
    """A regression test for firmware that read 0 as "no id could be read".

    The counter is a u16 and wraps through zero, so a long-lived session
    reaches it; a board that refused it would drop one command in 65536 and
    name no id while doing so. Sent as a raw line because the CLI's own counter
    deliberately starts at 1 and never produces this.
    """
    reply = device.exchange(command_line(MsgType.PING, 0))
    assert reply[Field.MSG_TYPE] == MsgType.PONG
    assert reply[Field.IN_REPLY_TO] == 0

    # And the guard still works at zero: the same id resent is still a repeat.
    assert device.exchange(command_line(MsgType.PING, 0)) == reply


def test_every_reply_carries_a_crc_the_host_can_check(device):
    """The obligation runs both ways.

    `parse_reply` checks the CRC before it parses and raises otherwise, so
    every reply this suite has read has already been checked -- but that is
    incidental to each of those tests. Here it is the assertion.
    """
    for msg_type in (MsgType.PING, MsgType.STATE):
        reply = device.request(msg_type)
        assert Field.CRC in reply
        assert len(reply[Field.CRC]) == 4
