# SPDX-License-Identifier: LGPL-3.0-or-later
"""What this client raises when the daemon says no.

Public, and in a module of its own, because these are the names a caller writes
in an `except` — and the whole point of `statemachined.v1.Error` travelling as
itself is that there is something worth catching by name.

**One class, four subclasses, and the subclasses are chosen by what a caller
would do about it.** A refusal that needs no different recovery does not need a
class; it needs a readable `error`. `docs/reference/api.md` in the daemon's
repository lists the vocabulary.
"""

from __future__ import annotations


class DaemonRefusedTheRequest(Exception):
    """The daemon refused, and said why.

    The same name the browser client uses, deliberately: one API, two clients,
    one word for the thing that happened.

    Four things, and all four are worth having:

    * `error` — stable and machine-readable: `no_such_graph`, `does_not_fit`,
      `recording_name_taken`, `busy`. **This is the one to branch on.** It is
      additive within an API major version: new kinds appear, none change
      meaning.
    * `context` — *what to change*. A field name, a document name, `device`.
      The daemon guarantees one; where it has nothing more specific it repeats
      `error`, because "which field" with no answer is worse than a coarse one.
    * `detail` — one sentence, for a person. Usually the answer, because these
      sentences are written to be read.
    * `status` — the gRPC code, as its own name. The category rather than the
      case, and the one thing a caller may act on without reading the rest.

    The first three come from `statemachined.v1.Error` in the call's trailing
    metadata, not from parsing `detail`: a client that reads a sentence to find
    out which refusal it was breaks when the sentence is reworded.

    **Where the board refused, `error` is the board's own code** — `busy`,
    `bad_index`, `graph_mismatch` — rather than this daemon's paraphrase of it.
    Those are the words the device's documentation uses.
    """

    def __init__(self, status: str, error: str, detail: str, context: str = "") -> None:
        super().__init__(f"{error}: {detail}" + (f" (change: {context})" if context else ""))
        self.status = status
        self.error = error
        self.detail = detail
        self.context = context

    @property
    def retryable(self) -> bool:
        """Whether retrying the identical request could work.

        Only `unavailable` — nothing answered, or there is no board. Everything
        else is a request to change something, and a loop that retried those
        would hammer a rig about a typo.

        `failed_precondition` is the one that looks retryable and is not: "a
        trial is already running" clears when that trial ends, which is an
        event to wait for rather than a call to repeat.
        """
        return self.status == "unavailable"


class DaemonIsUnavailable(DaemonRefusedTheRequest):
    """Nothing answered: no listener, a closed channel, a deadline.

    Its own class because it is the one a rig script legitimately *waits* on —
    a daemon starting, a box rebooting — and waiting on it should not mean
    catching everything. Nothing refused anything here, so there is no typed
    `Error` to carry and `error` is the gRPC code itself.

    Distinct from :class:`NoBoardIsAttached`, which is the daemon answering.
    """


class NoBoardIsAttached(DaemonRefusedTheRequest):
    """`not_connected` — the daemon is up, and has no device.

    **The daemon answering is what makes this different from silence**, and it
    is why this is worth its own class: the thing to do is plug the board in or
    call `open_link()`, not retry in a loop and not check whether the daemon is
    running. `read_health().device_connected` reports the same fact without
    raising.
    """


class TheRigIsNotInAStateForThat(DaemonRefusedTheRequest):
    """The call means nothing right now, and nothing about it is wrong.

    No config loaded, no graph set committed, a trial already running, a
    recording that is not open. All of them would be fine at another moment,
    which is exactly why they are not a bad request and not a missing thing.

    Its own class because the recovery is always "do the other thing first" —
    and `error` says which: `no_state_machine_config_loaded`, `no_graph_set`,
    `recording_state`, `busy`.
    """


class NoSuchDocument(DaemonRefusedTheRequest):
    """No graph, state machine config or recording by that name.

    `context` is which kind — `graph_name`, `config_name`, `name` — and
    `error` is `no_such_graph`, `no_such_state_machine_config` or
    `no_such_recording`. Its own class because "list them and pick another" is
    a recovery a script can actually perform.
    """
