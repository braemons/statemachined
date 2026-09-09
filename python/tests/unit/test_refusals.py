# SPDX-License-Identifier: LGPL-3.0-or-later
"""What survives a refusal, and why it has to.

The daemon's rule is that every refusal names what to change, and where the
*device* refused, the code is the device's own. That is worth a great deal at
two in the morning and nothing at all if a client turns it into
`HTTPStatusError: 409`. These tests are the claim that it survives.
"""

from __future__ import annotations

import httpx
import pytest
from statemachined.client import (
    Conflict,
    Invalid,
    NotConnected,
    NotFound,
    Refused,
    StatemachinedClient,
    StatemachinedError,
    TransportError,
)


def test_a_refusal_keeps_the_daemons_own_code_and_field(client, rig):
    rig.will_refuse(
        409,
        "graph_not_in_set",
        "graph 'catch' is not in committed set 8, which holds go-nogo, 2afc",
        "graph",
    )

    with pytest.raises(Conflict) as refused:
        client.trial.configure(193, graph="catch")

    assert refused.value.error == "graph_not_in_set"
    assert refused.value.context == "graph"
    assert refused.value.status_code == 409
    assert "committed set 8" in refused.value.detail


def test_the_message_says_what_was_being_attempted(client, rig):
    """`doing` is on every call for the person reading the traceback.

    "the rig refused" is half an answer; "the rig refused while arming trial
    193" is the half that says where to look.
    """
    rig.will_refuse(409, "no_graph_set", "no graph set is committed", "graph")

    with pytest.raises(Conflict) as refused:
        client.trial.configure(193, graph="go-nogo")

    assert "arming trial 193" in str(refused.value)
    assert "no_graph_set" in str(refused.value)
    assert "(change: graph)" in str(refused.value)


def test_a_devices_own_refusal_arrives_with_the_devices_own_word(client, rig):
    """Not flattened into the daemon's paraphrase.

    `busy`, `bad_index` and `graph_mismatch` are the words the protocol
    documentation uses, and a caller switching on them should be switching on
    the device's answer.
    """
    rig.will_refuse(409, "busy", "a run is in flight", "trial_id")

    with pytest.raises(Conflict) as refused:
        client.trial.start(2)

    assert refused.value.error == "busy"


def test_no_device_is_its_own_class_because_the_recovery_is_its_own(client, rig):
    rig.will_refuse(503, "not_connected", "no device is connected", "device")

    with pytest.raises(NotConnected):
        client.device.autorun()


def test_a_graph_that_is_not_there_is_a_not_found(client, rig):
    rig.will_refuse(404, "no_such_graph", "the store has no graph called 'nope'", "graph_name")

    with pytest.raises(NotFound) as refused:
        client.graphs.read("nope")

    assert refused.value.error == "no_such_graph"


def test_a_line_map_the_board_refuses_is_invalid_and_nothing_was_kept(client, rig):
    # Refused *before anything is kept*, so a rig answering this is still
    # running on the map it had. The class says which kind of refusal it was;
    # the guarantee is the daemon's.
    rig.will_refuse(
        422,
        "line_map_does_not_match_the_board",
        "this board has no pin 'D14'",
        "line_map",
    )

    with pytest.raises(Invalid) as refused:
        client.device.set_lines({"input_lines": [{"name": "lever", "pin_label": "D14"}]})

    assert refused.value.error == "line_map_does_not_match_the_board"


def test_fastapis_own_validation_shape_still_names_the_field(client, rig):
    """The one refusal the daemon does not write itself.

    FastAPI's `detail` is a list of objects and its `loc` is where the fault is.
    A client that stringified the list would hand a person a Python repr instead
    of the field name, which is the whole value of a 422.
    """
    rig.will_answer(
        {
            "detail": [
                {
                    "type": "greater_than_equal",
                    "loc": ["body", "cap_milliseconds"],
                    "msg": "Input should be greater than or equal to 0",
                }
            ]
        },
        422,
    )

    with pytest.raises(Invalid) as refused:
        client.trial.configure(1, graph="g", cap_milliseconds=-1)

    assert refused.value.context == "cap_milliseconds"
    assert "body -> cap_milliseconds" in refused.value.detail


def test_a_refusal_from_something_that_is_not_the_daemon_still_arrives(rig):
    """A proxy in front of a rig answers HTML, and this must not raise here.

    A client that assumed the daemon's shape while building the exception would
    hide the one clue there was.
    """
    rig.will_answer_with_text("<html><body>502 Bad Gateway</body></html>", 502)
    client = StatemachinedClient(
        "http://rig.test", http_client=httpx.Client(transport=httpx.MockTransport(rig))
    )

    with pytest.raises(Refused) as refused:
        client.health()

    assert refused.value.error == "http_502"
    assert "Bad Gateway" in refused.value.detail
    assert type(refused.value) is Refused


def test_a_rig_that_does_not_answer_is_not_a_refusal(rig):
    """The distinction the trial loop depends on.

    A refusal is an answer and tells you what to change. A silence tells you
    only that whatever you were about to do has not happened -- which for
    `configure` means the trial is not armed, and the two must not be caught by
    the same `except` by accident.
    """

    def nothing_is_listening(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = StatemachinedClient(
        "http://rig.test", http_client=httpx.Client(transport=httpx.MockTransport(nothing_is_listening))
    )

    with pytest.raises(TransportError) as failed:
        client.trial.start(1)

    assert "starting trial 1" in str(failed.value)
    assert not isinstance(failed.value, Refused)


def test_everything_this_client_raises_is_one_class(client, rig):
    """So a caller can wrap a session loop in one `except` and mean it."""
    rig.will_refuse(409, "busy", "a run is in flight")
    with pytest.raises(StatemachinedError):
        client.trial.start(1)


def test_an_answer_that_is_not_json_says_so_rather_than_raising_valueerror(client, rig):
    rig.will_answer_with_text("not json at all", 200)
    with pytest.raises(TransportError, match="not JSON"):
        client.health()
