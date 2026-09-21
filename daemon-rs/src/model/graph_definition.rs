// SPDX-License-Identifier: AGPL-3.0-or-later
//! A graph as somebody writes it down: states with names, lines with names.
//!
//! The authored form — what lives in `/var/lib/statemachined/graphs/`, what the
//! web UI edits, and what the compiler turns into the indices the protocol puts
//! on the wire. **Nothing here has an index in it**, and that is the point: an
//! index is a fact about one device's pools, and a paradigm should outlive the
//! board it was first run on.
//!
//! **This is a port, and the refusals are the contract.** A graph file somebody
//! wrote last year must parse identically and be refused identically, so the
//! messages here are the Python ones — see `model/graph_definition.py`, and
//! `tests/documents.rs`, which runs both implementations over the same corpus.
//!
//! What this refuses is a graph that could not be run: an unknown state name, a
//! terminal state with a timeout leading out of it, a pulse with no width. What
//! it cannot refuse is a graph too big for a particular board — that needs the
//! device's caps, so it lives in the compiler.

use std::collections::BTreeSet;

use indexmap::IndexMap;

use serde::{Deserialize, Serialize};

use super::trial_outcome::{declarable, terminal_outcome_for_name};

/// Why a document was refused, with the sentence a person reads.
///
/// One type rather than an error enum per rule: these are read by a human at a
/// rig through an editor, and what matters is that the sentence names the thing
/// to change.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Refused(pub String);

impl std::fmt::Display for Refused {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for Refused {}

/// Shared with the other documents, which are refused the same way.
pub type Result<T> = std::result::Result<T, Refused>;
type Checked<T> = Result<T>;

pub(crate) fn refuse<T>(sentence: impl Into<String>) -> Checked<T> {
    Err(Refused(sentence.into()))
}

// -- durations -----------------------------------------------------------------

/// How long, drawn on the device, from a named distribution the graph shares.
///
/// `#[serde(tag = "kind")]` is pydantic's discriminated union: the `kind` field
/// picks the variant, and an unknown one is refused by name rather than falling
/// through to a default.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum DurationDistribution {
    /// Always the same number of milliseconds.
    ///
    /// Still drawn on the device rather than pre-computed on the host, so that
    /// every duration in a record arrives by the same path and `drawn_ms` means
    /// the same thing whatever the distribution was.
    Fixed { duration_ms: i64 },
    /// Uniform over a closed interval, in whole milliseconds.
    Uniform { minimum_ms: i64, maximum_ms: i64 },
    /// Truncated exponential: the flat-hazard foreperiod, bounded at both ends.
    ///
    /// Integer-only on the device, deliberately — a float path would make the
    /// native simulator's numbers merely close to the firmware's.
    Exponential {
        minimum_ms: i64,
        maximum_ms: i64,
        mean_ms: i64,
    },
    /// One of a fixed list, optionally weighted.
    ///
    /// For a paradigm whose intervals are conditions rather than jitter.
    Choice {
        options_ms: Vec<i64>,
        /// Same length as `options_ms` when given. A shorter list is refused
        /// rather than padded: the unlisted options would silently get weight 1
        /// against neighbours weighted in the hundreds.
        #[serde(default, skip_serializing_if = "Option::is_none")]
        weights: Option<Vec<i64>>,
    },
}

impl DurationDistribution {
    fn validate(&self) -> Checked<()> {
        match self {
            Self::Fixed { duration_ms } => {
                if *duration_ms < 0 {
                    return refuse("duration_ms is negative");
                }
            }
            Self::Uniform {
                minimum_ms,
                maximum_ms,
            } => {
                if *minimum_ms < 0 || *maximum_ms < 0 {
                    return refuse("a bound is negative");
                }
                if maximum_ms < minimum_ms {
                    return refuse(format!(
                        "maximum_ms ({maximum_ms}) is below minimum_ms ({minimum_ms})"
                    ));
                }
            }
            Self::Exponential {
                minimum_ms,
                maximum_ms,
                mean_ms,
            } => {
                if *minimum_ms < 0 || *maximum_ms < 0 {
                    return refuse("a bound is negative");
                }
                if *mean_ms <= 0 {
                    return refuse("mean_ms is not positive");
                }
                if maximum_ms < minimum_ms {
                    return refuse(format!(
                        "maximum_ms ({maximum_ms}) is below minimum_ms ({minimum_ms})"
                    ));
                }
            }
            Self::Choice { options_ms, weights } => {
                if options_ms.is_empty() {
                    return refuse("options_ms is empty");
                }
                if let Some(weights) = weights {
                    if weights.len() != options_ms.len() {
                        return refuse(format!(
                            "weights has {} entries and options_ms has {}; they name the same \
                             choices, so they must line up",
                            weights.len(),
                            options_ms.len()
                        ));
                    }
                    if weights.iter().any(|weight| *weight < 0) {
                        return refuse("a weight is negative");
                    }
                    if weights.iter().sum::<i64>() == 0 {
                        return refuse("every weight is zero, so nothing could ever be drawn");
                    }
                }
            }
        }
        Ok(())
    }
}

// -- actions -------------------------------------------------------------------

/// What an action does. The six kinds the device knows.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ActionKind {
    High,
    Low,
    Toggle,
    Pulse,
    TimerStart,
    TimerCancel,
}

impl ActionKind {
    /// How the graph file spells it, for a message that quotes it back.
    fn spelled(&self) -> &'static str {
        match self {
            Self::High => "high",
            Self::Low => "low",
            Self::Toggle => "toggle",
            Self::Pulse => "pulse",
            Self::TimerStart => "timer_start",
            Self::TimerCancel => "timer_cancel",
        }
    }

    fn addresses_a_timer(&self) -> bool {
        matches!(self, Self::TimerStart | Self::TimerCancel)
    }
}

/// Something done to one output line when a state is entered or left.
///
/// `pulse` is the one that needs a width, and it is the one a reward is written
/// with: "pulse the valve for 40 ms on entering Hit". The width is served by the
/// device's own clock, which is the whole reason the timing authority is down
/// there rather than up here.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OutputActionSpecification {
    /// The output line, for the four kinds that move one. Empty for the two
    /// that address a global timer instead.
    #[serde(default)]
    pub line: String,
    pub kind: ActionKind,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub pulse_ms: Option<i64>,
    /// The global timer this action starts or cancels, by name.
    #[serde(default)]
    pub timer: String,
}

impl OutputActionSpecification {
    fn validate(&self) -> Checked<()> {
        let kind = self.kind.spelled();
        if self.kind.addresses_a_timer() {
            if self.timer.is_empty() {
                return refuse(format!("a '{kind}' action needs a timer"));
            }
            if !self.line.is_empty() {
                return refuse(format!(
                    "a '{kind}' action names a timer, not the line '{}'",
                    self.line
                ));
            }
            if self.pulse_ms.is_some() {
                return refuse(format!("pulse_ms means nothing to a '{kind}' action"));
            }
            return Ok(());
        }
        if self.line.is_empty() {
            return refuse(format!("a '{kind}' action needs a line"));
        }
        if !self.timer.is_empty() {
            return refuse(format!(
                "a '{kind}' action moves a line, not the timer '{}'",
                self.timer
            ));
        }
        if self.kind == ActionKind::Pulse {
            match self.pulse_ms {
                // A zero-width pulse raises a line and schedules its fall for
                // the same instant, so whether it reaches a pin depends on when
                // the scan lands. Refuse it rather than let a reward be
                // silently nothing.
                None | Some(0) => {
                    return refuse(format!(
                        "a pulse on '{}' needs a positive pulse_ms",
                        self.line
                    ))
                }
                Some(width) if width <= 0 => {
                    return refuse(format!(
                        "a pulse on '{}' needs a positive pulse_ms",
                        self.line
                    ))
                }
                Some(width) if width > 65535 => {
                    return refuse(format!(
                        "a pulse on '{}' is longer than 65535 ms",
                        self.line
                    ))
                }
                Some(_) => {}
            }
        } else if self.pulse_ms.is_some() {
            return refuse(format!(
                "pulse_ms means nothing to a '{kind}' action on '{}'",
                self.line
            ));
        }
        Ok(())
    }
}

// -- transitions ---------------------------------------------------------------

/// Three sets of input lines, evaluated against the conditioned word.
///
/// `all` and `none` and `any`, and nothing else. It is what the device can
/// evaluate inside a 100 us scan, and it is enough for every paradigm this was
/// written for: "the left lever and not the abort", "either lever".
///
/// The three are independent masks, ANDed:
///
/// ```text
///    (w & all) == all
/// && (any == 0 || (w & any) != 0)   // empty `any` means "don't care"
/// && (w & none) == 0
/// ```
///
/// A line may therefore appear in more than one of them, and two of the three
/// ways of doing that are mistakes rather than expressions.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TransitionPredicate {
    #[serde(default)]
    pub all: Vec<String>,
    #[serde(default)]
    pub any: Vec<String>,
    #[serde(default)]
    pub none: Vec<String>,
}

impl TransitionPredicate {
    fn validate(&self) -> Checked<()> {
        if self.all.is_empty() && self.any.is_empty() && self.none.is_empty() {
            // It would fire on the first evaluation of every state it is in,
            // which is never what an experimenter meant to write.
            return refuse("a transition predicate names no lines, so it would always fire");
        }

        // **Provably dead.** The masks are evaluated independently, so this
        // uploads, validates against the caps, and runs — as a transition that
        // can never fire, in a graph that looks right in every listing. That is
        // worth a refusal precisely because nothing downstream can notice it: a
        // state whose only way out is this one hangs until the trial cap, and
        // the outcome is a timeout somebody will spend an afternoon on.
        let all: BTreeSet<&String> = self.all.iter().collect();
        let none: BTreeSet<&String> = self.none.iter().collect();
        let contradictory: Vec<String> = all
            .intersection(&none)
            .map(|line| format!("'{line}'"))
            .collect();
        if !contradictory.is_empty() {
            return refuse(format!(
                "a transition predicate requires {} to be high and to be low at the same time, \
                 so it can never fire. Take the line out of `all` or out of `none`",
                contradictory.join(", ")
            ));
        }

        // The same fault said differently: a line in `any` cannot satisfy it if
        // `none` forbids that line, so an `any` clause made entirely of
        // forbidden lines can never be satisfied either.
        let any: BTreeSet<&String> = self.any.iter().collect();
        if !any.is_empty() && any.is_subset(&none) {
            let named: Vec<String> = any.iter().map(|line| format!("'{line}'")).collect();
            return refuse(format!(
                "a transition predicate needs at least one of {} high, and forbids every one of \
                 them in `none`, so it can never fire",
                named.join(", ")
            ));
        }
        Ok(())
    }

    /// Lines in both `all` and `any`, which makes the whole `any` clause moot.
    ///
    /// `all` already requires the line high, so the `any` clause is satisfied by
    /// it whenever the predicate could fire at all — and every *other* line in
    /// `any` stops mattering.
    ///
    /// **A warning rather than a refusal.** It is redundant rather than wrong,
    /// the graph does what the masks say, and refusing a redundancy mid-edit is
    /// how an editor becomes something people work around.
    pub fn lines_whose_any_membership_does_nothing(&self) -> Vec<String> {
        let all: BTreeSet<&String> = self.all.iter().collect();
        let any: BTreeSet<&String> = self.any.iter().collect();
        all.intersection(&any).map(|line| (*line).clone()).collect()
    }
}

/// One edge: when this holds, go there.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TransitionSpecification {
    pub when: TransitionPredicate,
    pub goto: String,
    /// The predicate must hold continuously for a drawn duration before the
    /// transition fires — "the lever is held down", not "the lever was touched".
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub hold: Option<String>,
    /// Fire on the predicate's *rising edge* by default, so a switch already
    /// held when the state is entered does not carry the trial straight through
    /// it.
    #[serde(default)]
    pub fire_if_already_true_on_entry: bool,
}

/// Leave after a drawn duration, whatever the inputs are doing.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StateTimeout {
    /// Names a distribution.
    pub after: String,
    pub goto: String,
}

// -- global timers -------------------------------------------------------------

/// A timer that runs beside the state machine rather than inside it.
///
/// **A running timer is an input line that is high.** A transition waits on one
/// exactly as it waits on a lever — `when: {none: [<timer name>]}` is "when that
/// timer ends" — so there is no separate event vocabulary anywhere, on the wire
/// or here.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct GlobalTimerSpecification {
    /// What starts it, or nothing — in which case only a `timer_start` action
    /// does.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub when: Option<TransitionPredicate>,
    /// How long it stays high, by distribution name. The one duration it cannot
    /// do without: a timer that is never high is a line that never moves.
    pub width: String,
    /// How long after the trigger before it goes high.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub delay: Option<String>,
    /// Dead time between pulses when `loops` asks for more than one.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub gap: Option<String>,
    /// A real output line driven alongside the timer's own bit.
    #[serde(default)]
    pub line: String,
    /// Pulses per trigger. 0 runs until something stops it.
    #[serde(default = "one")]
    pub loops: i64,
    /// Drive `line` low while running rather than high, for a rig whose "on" is
    /// not "high". The timer's own bit is unaffected.
    #[serde(default)]
    pub active_low: bool,
    /// Stop when the run that started it ends, instead of running on.
    #[serde(default)]
    pub trial_bound: bool,
}

fn one() -> i64 {
    1
}

// -- states --------------------------------------------------------------------

/// One state, by name.
///
/// A state is terminal when it declares an `outcome`. Nothing exits a terminal
/// state — it is where the trial ends and what it ended as — so a terminal state
/// with a timeout or a transition is a graph that says two things at once, and
/// is refused.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StateDefinition {
    pub name: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub outcome: Option<String>,
    #[serde(default)]
    pub on_entry: Vec<OutputActionSpecification>,
    #[serde(default)]
    pub on_exit: Vec<OutputActionSpecification>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub timeout: Option<StateTimeout>,
    #[serde(default)]
    pub transitions: Vec<TransitionSpecification>,
    /// Terminal states only: the distribution the dwell in this state is drawn
    /// from — how long before another trial may begin. The inter-trial interval,
    /// written where every other duration of a paradigm is written.
    ///
    /// It does not give a terminal state an exit. What it decides is when the
    /// *next* trial may start.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub relight_after: Option<String>,
}

impl StateDefinition {
    pub fn is_terminal(&self) -> bool {
        self.outcome.is_some()
    }

    fn validate(&self) -> Checked<()> {
        if self.name.is_empty() {
            return refuse("a state has no name");
        }
        for action in self.on_entry.iter().chain(&self.on_exit) {
            action.validate()?;
        }
        for transition in &self.transitions {
            transition.when.validate()?;
            if transition.goto.is_empty() {
                return refuse(format!("a transition of state '{}' goes nowhere", self.name));
            }
        }

        let Some(outcome) = &self.outcome else {
            // A dwell is drawn on arriving at the end of a trial, so nothing
            // would ever read one declared here. Refused rather than ignored:
            // silently dropping it would leave somebody believing they had set
            // an inter-trial interval.
            if self.relight_after.is_some() {
                return refuse(format!(
                    "state '{}' declares relight_after but is not terminal. A dwell is how long \
                     a trial's *last* state is held before another may start",
                    self.name
                ));
            }
            return Ok(());
        };

        if terminal_outcome_for_name(outcome).is_none() {
            return refuse(format!(
                "state '{}' declares outcome '{outcome}'. Legal outcomes: {}",
                self.name,
                declarable().join(", ")
            ));
        }
        if self.timeout.is_some() || !self.transitions.is_empty() {
            return refuse(format!(
                "state '{}' is terminal and also leads somewhere. Nothing exits a terminal \
                 state: it is where the trial ends",
                self.name
            ));
        }
        // Its entry actions do run, and are how a reward is written. Only exit
        // actions are impossible, because there is no exit.
        if !self.on_exit.is_empty() {
            return refuse(format!(
                "state '{}' is terminal and has on_exit actions, which can never run",
                self.name
            ));
        }
        Ok(())
    }
}

// -- the graph -----------------------------------------------------------------

/// Something legal that is probably not what somebody meant.
///
/// Kept apart from the refusals on purpose. A refusal is for a graph that
/// cannot do what it says; this is for one that does something narrower than
/// its author thinks, and the difference is whether an editor should stop
/// somebody mid-edit or tell them.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Warning {
    pub kind: String,
    pub state: String,
    pub transition: usize,
    pub lines: Vec<String>,
    pub detail: String,
}

/// A whole paradigm, as authored.
///
/// Everything checkable without a device is checked here. What is left for the
/// compiler is everything that needs one: whether the line names exist on this
/// rig, and whether the whole set fits this board's pools.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct GraphDefinition {
    pub name: String,
    pub entry: String,
    #[serde(default)]
    pub distributions: IndexMap<String, DurationDistribution>,
    /// Global timers, by name. Declared on a graph because that is where the
    /// distributions they name are declared, but *pooled across the set* by the
    /// compiler — a timer outlives the run that started it, so it cannot belong
    /// to one graph.
    #[serde(default)]
    pub timers: IndexMap<String, GlobalTimerSpecification>,
    pub states: Vec<StateDefinition>,
}

impl GraphDefinition {
    /// Parse and check, which is the only way to get one.
    ///
    /// **Parsing and validation are one step**, as they are in pydantic: a
    /// `GraphDefinition` that exists has been checked, so nothing downstream
    /// has to wonder.
    pub fn from_json(text: &str) -> Checked<Self> {
        let graph: Self = serde_json::from_str(text).map_err(|problem| {
            // serde's own sentence, which names the field and the line. The
            // shape differs from pydantic's; what must match is *that* it is
            // refused, not the wording of a parse error.
            Refused(problem.to_string())
        })?;
        graph.validate()?;
        Ok(graph)
    }

    pub fn state_names_in_declaration_order(&self) -> Vec<&str> {
        self.states.iter().map(|state| state.name.as_str()).collect()
    }

    pub fn state_named(&self, name: &str) -> Option<&StateDefinition> {
        self.states.iter().find(|state| state.name == name)
    }

    pub fn warnings(&self) -> Vec<Warning> {
        let mut found = Vec::new();
        for state in &self.states {
            for (position, transition) in state.transitions.iter().enumerate() {
                let moot = transition.when.lines_whose_any_membership_does_nothing();
                if moot.is_empty() {
                    continue;
                }
                let named: Vec<String> = moot.iter().map(|line| format!("'{line}'")).collect();
                found.push(Warning {
                    kind: "any_clause_has_no_effect".into(),
                    state: state.name.clone(),
                    transition: position,
                    detail: format!(
                        "{} is in both `all` and `any`, so the `any` clause is satisfied \
                         whenever this predicate could fire at all -- every other line in `any` \
                         is ignored. Did you mean to leave it out of one of them?",
                        named.join(", ")
                    ),
                    lines: moot,
                });
            }
        }
        found
    }

    /// The document's own rules, for a graph reached inside another document.
    pub fn validate_document(&self) -> Checked<()> {
        self.validate()
    }

    fn validate(&self) -> Checked<()> {
        if self.name.is_empty() {
            return refuse("a graph has no name");
        }
        if self.entry.is_empty() {
            return refuse(format!("graph '{}' names no entry state", self.name));
        }
        if self.states.is_empty() {
            return refuse(format!("graph '{}' has no states", self.name));
        }
        for distribution in self.distributions.values() {
            distribution.validate()?;
        }
        for timer in self.timers.values() {
            if timer.width.is_empty() {
                return refuse("a global timer has no width");
            }
            if !(0..=255).contains(&timer.loops) {
                return refuse(format!("a global timer loops {} times", timer.loops));
            }
            if let Some(when) = &timer.when {
                when.validate()?;
            }
        }
        for state in &self.states {
            state.validate()?;
        }
        self.refuse_duplicate_state_names()?;
        self.refuse_names_that_point_at_nothing()?;
        self.refuse_a_graph_that_cannot_end()
    }

    fn refuse_duplicate_state_names(&self) -> Checked<()> {
        let mut seen: BTreeSet<&str> = BTreeSet::new();
        for state in &self.states {
            if !seen.insert(state.name.as_str()) {
                return refuse(format!(
                    "graph '{}' has two states called '{}'",
                    self.name, state.name
                ));
            }
        }
        Ok(())
    }

    fn refuse_names_that_point_at_nothing(&self) -> Checked<()> {
        let names: BTreeSet<&str> = self.state_names_in_declaration_order().into_iter().collect();
        if !names.contains(self.entry.as_str()) {
            return refuse(format!(
                "entry state '{}' is not a state of graph '{}'",
                self.entry, self.name
            ));
        }
        for state in &self.states {
            if let Some(timeout) = &state.timeout {
                let where_ = format!("state '{}'s timeout", state.name);
                self.require_state(&timeout.goto, &where_)?;
                self.require_distribution(&timeout.after, &where_)?;
            }
            if let Some(relight) = &state.relight_after {
                self.require_distribution(
                    relight,
                    &format!("state '{}'s relight_after", state.name),
                )?;
            }
            for (position, transition) in state.transitions.iter().enumerate() {
                let where_ = format!("transition {position} of state '{}'", state.name);
                self.require_state(&transition.goto, &where_)?;
                if let Some(hold) = &transition.hold {
                    self.require_distribution(hold, &where_)?;
                }
            }
        }
        Ok(())
    }

    fn require_state(&self, name: &str, where_: &str) -> Checked<()> {
        if self.state_named(name).is_none() {
            return refuse(format!(
                "{where_} goes to '{name}', which is not a state. Has: {}",
                self.state_names_in_declaration_order().join(", ")
            ));
        }
        Ok(())
    }

    fn require_distribution(&self, name: &str, where_: &str) -> Checked<()> {
        if !self.distributions.contains_key(name) {
            let known: Vec<&str> = self.distributions.keys().map(String::as_str).collect();
            return refuse(format!(
                "{where_} draws from '{name}', which is not a distribution of this graph. Has: {}",
                if known.is_empty() {
                    "(none)".to_string()
                } else {
                    known.join(", ")
                }
            ));
        }
        Ok(())
    }

    /// Reachability, the same rule the firmware enforces at `set_end`.
    ///
    /// Checked here as well as there because **the message is the point**: this
    /// one can say *Foreperiod*, and the device can only say state 2. A graph
    /// that cannot reach a terminal state is a graph that hangs with its outputs
    /// high until the trial cap fires.
    fn refuse_a_graph_that_cannot_end(&self) -> Checked<()> {
        let mut reachable: BTreeSet<&str> = BTreeSet::new();
        let mut to_walk: Vec<&str> = vec![self.entry.as_str()];
        while let Some(name) = to_walk.pop() {
            if !reachable.insert(name) {
                continue;
            }
            let state = self
                .state_named(name)
                .expect("names were checked before reachability");
            if let Some(timeout) = &state.timeout {
                to_walk.push(&timeout.goto);
            }
            to_walk.extend(state.transitions.iter().map(|t| t.goto.as_str()));
        }

        if !reachable
            .iter()
            .any(|name| self.state_named(name).is_some_and(StateDefinition::is_terminal))
        {
            return refuse(format!(
                "graph '{}' cannot end: no terminal state is reachable from '{}', so a trial \
                 would run until the cap fired",
                self.name, self.entry
            ));
        }

        // Refused rather than pruned. An unreachable state is usually a typo in
        // the name of the one that should have led to it, and silently dropping
        // it hides that until somebody wonders why a condition never occurs.
        let declared: BTreeSet<&str> =
            self.state_names_in_declaration_order().into_iter().collect();
        let unreachable: Vec<&str> = declared.difference(&reachable).copied().collect();
        if !unreachable.is_empty() {
            return refuse(format!(
                "graph '{}' cannot reach these states from '{}': {}",
                self.name,
                self.entry,
                unreachable.join(", ")
            ));
        }
        Ok(())
    }
}
