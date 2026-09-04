# SPDX-License-Identifier: LGPL-3.0-or-later
"""A graph as somebody writes it down: states with names, lines with names.

This is the authored form -- what lives in /var/lib/statemachined/graphs/, what
the web UI edits, and what `statemachined.graph_set_compiler` turns into the indices
dev/PROTOCOL.md 3.2 puts on the wire. Nothing here has an index in it, and that
is the point: an index is a fact about one device's pools, and a paradigm should
outlive the board it was first run on.

The shape of a graph is the firmware's, one level up:

  * A **state** may raise output lines on entry and on exit, may have a timeout
    that carries it somewhere after a drawn duration, and may be terminal --
    in which case it declares the outcome triald will record.
  * A **transition** watches the input lines and fires when its predicate holds.
    Predicates are three masks (`all`, `any`, `none`) because that is what the
    device evaluates in a scan; anything richer would be a scripting language
    running at 10 kHz.
  * A **duration** is drawn on the device, per visit, from a named distribution
    the whole graph shares.

What this refuses is a graph that could not be run: an unknown state name, a
terminal state with a timeout leading out of it, a pulse with no width. What it
cannot refuse is a graph too big for a particular board -- that needs the
device's `caps`, so it lives in `statemachined.graph_set_compiler`.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .trial_outcome import DECLARABLE_TERMINAL_OUTCOMES

# ------------------------------------------------------------ durations ---


class FixedDuration(BaseModel):
    """Always the same number of milliseconds.

    Still drawn on the device rather than pre-computed on the host, so that
    every duration in a record arrives by the same path and `drawn_ms` means the
    same thing whatever the distribution was.
    """

    model_config = ConfigDict(extra="forbid")
    kind: Literal["fixed"] = "fixed"
    duration_ms: int = Field(ge=0)


class UniformDuration(BaseModel):
    """Uniform over a closed interval, in whole milliseconds."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["uniform"] = "uniform"
    minimum_ms: int = Field(ge=0)
    maximum_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def _refuse_an_inverted_interval(self) -> UniformDuration:
        if self.maximum_ms < self.minimum_ms:
            raise ValueError(
                f"maximum_ms ({self.maximum_ms}) is below minimum_ms ({self.minimum_ms})"
            )
        return self


class ExponentialDuration(BaseModel):
    """Truncated exponential: the flat-hazard foreperiod, bounded at both ends.

    A foreperiod an animal cannot anticipate is the usual reason to want one,
    and the truncation is what makes it a duration rather than an occasional
    very long wait. Integer-only on the device, deliberately -- see
    firmware/core/random/random_distribution.h: a float path would make the
    native simulator's numbers merely close to the firmware's.
    """

    model_config = ConfigDict(extra="forbid")
    kind: Literal["exponential"] = "exponential"
    minimum_ms: int = Field(ge=0)
    maximum_ms: int = Field(ge=0)
    mean_ms: int = Field(gt=0)

    @model_validator(mode="after")
    def _refuse_an_inverted_interval(self) -> ExponentialDuration:
        if self.maximum_ms < self.minimum_ms:
            raise ValueError(
                f"maximum_ms ({self.maximum_ms}) is below minimum_ms ({self.minimum_ms})"
            )
        return self


class ChoiceDuration(BaseModel):
    """One of a fixed list, optionally weighted.

    For a paradigm whose intervals are conditions rather than jitter -- three
    stimulus-onset asynchronies, say, which are meant to be exactly those three.
    """

    model_config = ConfigDict(extra="forbid")
    kind: Literal["choice"] = "choice"
    options_ms: list[int] = Field(min_length=1)
    #: Same length as `options_ms` when given. A shorter list is refused rather
    #: than padded: the unlisted options would silently get weight 1 against
    #: neighbours weighted in the hundreds.
    weights: list[int] | None = None

    @model_validator(mode="after")
    def _refuse_weights_that_do_not_line_up(self) -> ChoiceDuration:
        if self.weights is None:
            return self
        if len(self.weights) != len(self.options_ms):
            raise ValueError(
                f"weights has {len(self.weights)} entries and options_ms has "
                f"{len(self.options_ms)}; they name the same choices, so they must line up"
            )
        if any(weight < 0 for weight in self.weights):
            raise ValueError("a weight is negative")
        if sum(self.weights) == 0:
            raise ValueError("every weight is zero, so nothing could ever be drawn")
        return self


DurationDistribution = Annotated[
    Union[FixedDuration, UniformDuration, ExponentialDuration, ChoiceDuration],
    Field(discriminator="kind"),
]


# -------------------------------------------------------------- actions ---


class OutputActionSpecification(BaseModel):
    """Something done to one output line when a state is entered or left.

    `pulse` is the one that needs a width, and it is the one a reward is written
    with: "pulse the valve for 40 ms on entering Hit". The width is served by
    the device's own clock, which is the whole reason the timing authority is
    down there rather than up here.
    """

    model_config = ConfigDict(extra="forbid")

    line: str = Field(min_length=1)
    kind: Literal["high", "low", "toggle", "pulse"]
    pulse_ms: int | None = None

    @model_validator(mode="after")
    def _refuse_a_pulse_with_no_width(self) -> OutputActionSpecification:
        if self.kind == "pulse":
            if self.pulse_ms is None or self.pulse_ms <= 0:
                # A zero-width pulse raises a line and schedules its fall for
                # the same instant, so whether it reaches a pin depends on when
                # the scan lands. Refuse it rather than let a reward be silently
                # nothing.
                raise ValueError(f"a pulse on {self.line!r} needs a positive pulse_ms")
            if self.pulse_ms > 65535:
                raise ValueError(f"a pulse on {self.line!r} is longer than 65535 ms")
        elif self.pulse_ms is not None:
            raise ValueError(f"pulse_ms means nothing to a {self.kind!r} action on {self.line!r}")
        return self


# ---------------------------------------------------------- transitions ---


class TransitionPredicate(BaseModel):
    """Three sets of input lines, evaluated against the conditioned word.

    `all` and `none` and `any`, and nothing else. It is what the device can
    evaluate inside a 100 us scan, and it is enough for every paradigm this was
    written for: "the left lever and not the abort", "either lever".

    The three are independent masks, ANDed
    (firmware/core/graph/transition.h)::

        (w & all)  == all
        && (any == 0 || (w & any) != 0)      # empty `any` means "don't care"
        && (w & none) == 0

    A line may therefore appear in more than one of them, and two of the three
    ways of doing that are mistakes rather than expressions. See the validator
    below: one is refused, the other is a redundancy this refuses to make a
    person's problem.
    """

    model_config = ConfigDict(extra="forbid")

    all: list[str] = Field(default_factory=list)
    any: list[str] = Field(default_factory=list)
    none: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _refuse_a_predicate_that_names_nothing(self) -> TransitionPredicate:
        if not (self.all or self.any or self.none):
            # It would fire on the first evaluation of every state it is in,
            # which is never what an experimenter meant to write.
            raise ValueError("a transition predicate names no lines, so it would always fire")
        return self

    @model_validator(mode="after")
    def _refuse_a_predicate_nothing_can_satisfy(self) -> TransitionPredicate:
        """A line required high and required not-high. Provably dead.

        The masks are evaluated independently, so this uploads, validates
        against the caps, and runs -- as a transition that can never fire, in a
        graph that looks right in every listing. That is worth a refusal
        precisely because nothing downstream can notice it: a state whose only
        way out is this one hangs until the trial cap, and the outcome is a
        timeout somebody will spend an afternoon on.
        """
        contradictory = sorted(set(self.all) & set(self.none))
        if contradictory:
            named = ", ".join(repr(line) for line in contradictory)
            raise ValueError(
                f"a transition predicate requires {named} to be high and to be low at the same "
                f"time, so it can never fire. Take the line out of `all` or out of `none`"
            )

        # The same fault said differently: a line in `any` cannot satisfy it if
        # `none` forbids that line, so an `any` clause made entirely of
        # forbidden lines can never be satisfied either.
        if self.any and set(self.any) <= set(self.none):
            named = ", ".join(repr(line) for line in sorted(set(self.any)))
            raise ValueError(
                f"a transition predicate needs at least one of {named} high, and forbids every "
                f"one of them in `none`, so it can never fire"
            )
        return self

    def lines_whose_any_membership_does_nothing(self) -> list[str]:
        """Lines in both `all` and `any`, which makes the whole `any` clause moot.

        `all` already requires the line high, so the `any` clause is satisfied
        by it whenever the predicate could fire at all -- and every *other* line
        in `any` stops mattering. Somebody writing "L, and either M or N" as
        `all: [L]`, `any: [L, M, N]` has written "L", and M and N are silently
        ignored.

        A warning rather than a refusal. It is redundant rather than wrong, the
        graph does what the masks say, and refusing a redundancy mid-edit is how
        an editor becomes something people work around. The web UI says so where
        the predicate is written; `POST /api/graphs/{name}/validate` returns it.
        """
        return sorted(set(self.all) & set(self.any))


class TransitionSpecification(BaseModel):
    """One edge: when this holds, go there."""

    model_config = ConfigDict(extra="forbid")

    when: TransitionPredicate
    goto: str = Field(min_length=1)

    #: The predicate must hold continuously for a drawn duration before the
    #: transition fires -- "the lever is held down", not "the lever was touched".
    #: Names a distribution, like a timeout does.
    hold: str | None = None

    #: Fire on the predicate's *rising edge* by default, so a switch already
    #: held when the state is entered does not carry the trial straight through
    #: it. Set this when the state means "wait until the world is already like
    #: this" rather than "wait for something to happen".
    fire_if_already_true_on_entry: bool = False


class StateTimeout(BaseModel):
    """Leave after a drawn duration, whatever the inputs are doing."""

    model_config = ConfigDict(extra="forbid")

    after: str = Field(min_length=1)  # names a distribution
    goto: str = Field(min_length=1)


# --------------------------------------------------------------- states ---


class StateDefinition(BaseModel):
    """One state, by name.

    A state is terminal when it declares an `outcome`. Nothing exits a terminal
    state -- it is where the trial ends and what it ended as -- so a terminal
    state with a timeout or a transition is a graph that says two things at
    once, and is refused.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    outcome: str | None = None
    on_entry: list[OutputActionSpecification] = Field(default_factory=list)
    on_exit: list[OutputActionSpecification] = Field(default_factory=list)
    timeout: StateTimeout | None = None
    transitions: list[TransitionSpecification] = Field(default_factory=list)

    @property
    def is_terminal(self) -> bool:
        return self.outcome is not None

    @model_validator(mode="after")
    def _refuse_a_terminal_state_that_also_leads_somewhere(self) -> StateDefinition:
        if self.outcome is None:
            return self
        if self.outcome not in DECLARABLE_TERMINAL_OUTCOMES:
            legal = ", ".join(sorted(DECLARABLE_TERMINAL_OUTCOMES))
            raise ValueError(
                f"state {self.name!r} declares outcome {self.outcome!r}. Legal outcomes: {legal}"
            )
        if self.timeout is not None or self.transitions:
            raise ValueError(
                f"state {self.name!r} is terminal and also leads somewhere. Nothing exits a "
                "terminal state: it is where the trial ends"
            )
        # Its entry actions do run, and are how a reward is written. Only exit
        # actions are impossible, because there is no exit.
        if self.on_exit:
            raise ValueError(
                f"state {self.name!r} is terminal and has on_exit actions, which can never run"
            )
        return self


class GraphDefinition(BaseModel):
    """A whole paradigm, as authored.

    Everything checkable without a device is checked here. What is left for
    `statemachined.graph_set_compiler` is everything that needs one: whether the line names
    exist on this rig, and whether the whole set fits this board's pools.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    entry: str = Field(min_length=1)
    distributions: dict[str, DurationDistribution] = Field(default_factory=dict)
    states: list[StateDefinition] = Field(min_length=1)

    @property
    def state_names_in_declaration_order(self) -> list[str]:
        return [state.name for state in self.states]

    def state_named(self, state_name: str) -> StateDefinition:
        for state in self.states:
            if state.name == state_name:
                return state
        raise ValueError(f"graph {self.name!r} has no state called {state_name!r}")

    def warnings(self) -> list[dict[str, object]]:
        """Things that are legal, run exactly as written, and are probably not
        what somebody meant.

        Kept apart from the validators on purpose. A refusal is for a graph that
        cannot do what it says; this is for one that does something narrower
        than its author thinks, and the difference is whether an editor should
        stop somebody mid-edit or tell them.
        """
        found: list[dict[str, object]] = []
        for state in self.states:
            for position, transition in enumerate(state.transitions):
                moot = transition.when.lines_whose_any_membership_does_nothing()
                if not moot:
                    continue
                named = ", ".join(repr(line) for line in moot)
                found.append(
                    {
                        "kind": "any_clause_has_no_effect",
                        "state": state.name,
                        "transition": position,
                        "lines": moot,
                        "detail": (
                            f"{named} is in both `all` and `any`, so the `any` clause is "
                            f"satisfied whenever this predicate could fire at all -- every "
                            f"other line in `any` is ignored. Did you mean to leave it out of "
                            f"one of them?"
                        ),
                    }
                )
        return found

    @model_validator(mode="after")
    def _refuse_a_graph_that_could_not_be_run(self) -> GraphDefinition:
        self._refuse_duplicate_state_names()
        self._refuse_names_that_point_at_nothing()
        self._refuse_a_graph_that_cannot_end()
        return self

    def _refuse_duplicate_state_names(self) -> None:
        seen: set[str] = set()
        for state in self.states:
            if state.name in seen:
                raise ValueError(f"graph {self.name!r} has two states called {state.name!r}")
            seen.add(state.name)

    def _refuse_names_that_point_at_nothing(self) -> None:
        state_names = set(self.state_names_in_declaration_order)
        if self.entry not in state_names:
            raise ValueError(f"entry state {self.entry!r} is not a state of graph {self.name!r}")
        for state in self.states:
            if state.timeout is not None:
                self._require_state(state.timeout.goto, f"state {state.name!r}'s timeout")
                self._require_distribution(state.timeout.after, f"state {state.name!r}'s timeout")
            for position, transition in enumerate(state.transitions):
                where = f"transition {position} of state {state.name!r}"
                self._require_state(transition.goto, where)
                if transition.hold is not None:
                    self._require_distribution(transition.hold, where)

    def _require_state(self, state_name: str, where: str) -> None:
        if state_name not in set(self.state_names_in_declaration_order):
            known = ", ".join(self.state_names_in_declaration_order)
            raise ValueError(f"{where} goes to {state_name!r}, which is not a state. Has: {known}")

    def _require_distribution(self, distribution_name: str, where: str) -> None:
        if distribution_name not in self.distributions:
            known = ", ".join(sorted(self.distributions)) or "(none)"
            raise ValueError(
                f"{where} draws from {distribution_name!r}, which is not a distribution "
                f"of this graph. Has: {known}"
            )

    def _refuse_a_graph_that_cannot_end(self) -> None:
        """Reachability, the same rule the firmware enforces at `set_end`.

        Checked here as well as there because the message is the point: this one
        can say *Foreperiod*, and the device can only say state 2. A graph that
        cannot reach a terminal state is a graph that hangs with its outputs
        high until the trial cap fires.
        """
        reachable_state_names: set[str] = set()
        states_to_walk = [self.entry]
        while states_to_walk:
            state_name = states_to_walk.pop()
            if state_name in reachable_state_names:
                continue
            reachable_state_names.add(state_name)
            state = self.state_named(state_name)
            if state.timeout is not None:
                states_to_walk.append(state.timeout.goto)
            states_to_walk.extend(transition.goto for transition in state.transitions)

        if not any(self.state_named(name).is_terminal for name in reachable_state_names):
            raise ValueError(
                f"graph {self.name!r} cannot end: no terminal state is reachable from "
                f"{self.entry!r}, so a trial would run until the cap fired"
            )

        unreachable_state_names = set(self.state_names_in_declaration_order) - reachable_state_names
        if unreachable_state_names:
            # Refused rather than pruned. An unreachable state is usually a
            # typo in the name of the one that should have led to it, and
            # silently dropping it hides that until somebody wonders why a
            # condition never occurs.
            listed = ", ".join(sorted(unreachable_state_names))
            raise ValueError(
                f"graph {self.name!r} cannot reach these states from {self.entry!r}: {listed}"
            )
