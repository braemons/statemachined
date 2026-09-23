// SPDX-License-Identifier: AGPL-3.0-or-later
//! Names into indices: the one translation the daemon exists to do.
//!
//! A person writes `{"when": {"all": ["lever_left"], "none": ["abort"]}, "goto":
//! "Hit"}`. The device is told `{"all": 16, "none": 4, "target": 3}`, because it
//! has 32 KB and cannot hold the word "lever_left" (docs/reference/protocol.md,
//! "Types and units"). Everything in between happens here.
//!
//! What comes out is a **plan**, not a conversation: an ordered list of messages
//! and the tables needed to read a result back. Nothing here touches a link,
//! which is what lets the whole translation be tested on a host, message by
//! message, against the protocol document — and what keeps the device layer from
//! ever needing to know what a name is.
//!
//! Three things are checked here that nowhere else can check them:
//!
//! * **The line names exist on this rig.** The graph is portable; the line map
//!   is not, and a paradigm moved between rigs finds out here rather than by
//!   driving the wrong valve.
//! * **The whole set fits this board.** The pools are shared, so it is the *sum*
//!   across every graph that matters, and it is checked against the `caps` the
//!   device declared in `hello_ack` rather than against a constant compiled in
//!   here. A twelve-graph board and a twenty-graph board are both answered
//!   correctly by the same code.
//! * **Nothing is uploaded that the device would refuse.** The device validates
//!   again regardless — it does not trust the host, and a daemon bug must not be
//!   able to commit a bad set — but a refusal that names `Foreperiod` before the
//!   first byte goes out is a better error than one naming state 2 after the
//!   last.
//!
//! **This is a port.** The wire bodies are checked, message by message, against
//! what `tools/graph_set_cases.py` records the Python daemon producing — see
//! `tests/compiler.rs`. A message this produces that the Python one would not is
//! a disagreement about the protocol, because the device validates both.

use indexmap::IndexMap;
use serde_json::{Map as JsonMap, Value as Json};

use crate::device::message_vocabulary::MsgType;
use crate::model::graph_definition::{
    DurationDistribution, GraphDefinition, OutputActionSpecification, StateDefinition,
};
use crate::model::line_map::LineMap;
use crate::model::trial_outcome::terminal_outcome_for_name;

/// `kNoTransition` on the wire, and what a `graph_state` with no timeout omits.
pub const NO_TRANSITION_FIRED: u8 = 255;

/// Why a set was refused, with a sentence a paradigm author can act on.
///
/// Every message names the graph, and where it can, the state and the field.
/// "Every refusal names what to change" is the rule the firmware's own errors
/// follow; this is the same rule one layer up, where the names still exist.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CompileError {
    /// A set that will not compile or will not fit — cut a state, drop a graph.
    Set(String),
    /// A set that compiled perfectly and has no graph by that name.
    ///
    /// **A different thing to fix.** A caller told `Set` would go looking for a
    /// board that was never full; this one is fixed by committing a set that
    /// does contain the graph. Its own variant so a caller can tell "this set
    /// cannot be uploaded" from "here is the graph you asked for, and it is not
    /// in the set you committed".
    NotInSet(String),
}

impl std::fmt::Display for CompileError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Set(sentence) | Self::NotInSet(sentence) => f.write_str(sentence),
        }
    }
}

impl std::error::Error for CompileError {}

fn refuse<T>(sentence: impl Into<String>) -> Result<T, CompileError> {
    Err(CompileError::Set(sentence.into()))
}

/// What one board can hold, as `hello_ack` declares it.
///
/// Read from the device, never assumed: the reference board ships more than one
/// image, and a Teensy is a different set of numbers again. Compiling against a
/// constant would mean a graph that fits in the tests and not on the bench.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DeviceCapabilities {
    pub max_line: i64,
    pub max_states: i64,
    pub max_transitions: i64,
    pub max_output_actions: i64,
    pub max_distributions: i64,
    pub max_choice_options: i64,
    pub max_path: i64,
    pub max_graphs: i64,
    pub max_timers: i64,
    /// Which input line the first global timer holds high. Not derivable from
    /// the line count: the timers are counted down from the top of the word, so
    /// that a graph means the same thing on a board with eight inputs and one
    /// with twenty. The device reports it because nothing else can know it.
    pub first_timer_line: i64,
    /// Not part of `caps` on the wire — they are top-level members of
    /// `hello_ack` — but they belong to the same question, so `from_hello_ack`
    /// folds them in here.
    pub input_line_count: i64,
    pub output_line_count: i64,
}

impl DeviceCapabilities {
    /// The capacities out of a greeting, wherever the wire happens to put them.
    pub fn from_hello_ack(hello_ack: &Json) -> DeviceCapabilities {
        let caps = hello_ack.get("caps").and_then(Json::as_object).cloned();
        let number = |key: &str, default: i64| -> i64 {
            caps.as_ref()
                .and_then(|caps| caps.get(key))
                .and_then(Json::as_i64)
                .unwrap_or(default)
        };
        // The `Field(gt=0)` / `ge=0` bounds in the pydantic model are a guard
        // against a greeting that lies, not a decision the host gets to make
        // here. A greeting is the device's word about itself; if it says the
        // board holds zero states then a set that needs one does not fit, which
        // is answered by the fit check rather than asserted here.
        DeviceCapabilities {
            max_line: number("max_line", 0),
            max_states: number("max_states", 1),
            max_transitions: number("max_transitions", 0),
            max_output_actions: number("max_output_actions", 0),
            max_distributions: number("max_distributions", 0),
            max_choice_options: number("max_choice_options", 0),
            max_path: number("max_path", 1),
            max_graphs: number("max_graphs", 1),
            max_timers: number("max_timers", 0),
            first_timer_line: number("first_timer_line", 24),
            input_line_count: hello_ack
                .get("n_input_lines")
                .and_then(Json::as_i64)
                .unwrap_or(32),
            output_line_count: hello_ack
                .get("n_output_lines")
                .and_then(Json::as_i64)
                .unwrap_or(32),
        }
    }
}

/// One line of docs/reference/protocol.md 3.2, before it is framed.
///
/// A message type and its body, in the order it must be sent. The CRC and the
/// rolling checksum belong to the wire and are added there, over the bytes
/// actually put on it — computing them here would mean computing them over
/// bytes nobody sent.
#[derive(Debug, Clone, PartialEq)]
pub struct UploadMessage {
    pub msg_type: MsgType,
    pub fields: JsonMap<String, Json>,
}

/// One graph, as the device now holds it and as a result can be read back.
///
/// The tables are what make a record legible months later: they are the graph
/// that actually ran, kept beside the trial rather than looked up in whatever
/// the store holds today.
#[derive(Debug, Clone, PartialEq)]
pub struct CompiledGraph {
    pub name: String,
    pub slot: usize,
    pub state_names_by_index: Vec<String>,
    /// For each state, where each of its transitions leads, in the order the
    /// graph declares them — so a visit's `transition_index` resolves to a
    /// state name without the host knowing the pool's layout.
    pub transition_target_names_by_state_index: Vec<Vec<String>>,
    /// Which entry of the set's shared distribution pool each of this graph's
    /// named distributions became. What a per-trial `patch` needs.
    pub distribution_pool_index_by_name: IndexMap<String, usize>,
}

impl CompiledGraph {
    pub fn state_index_for_name(&self, state_name: &str) -> Option<usize> {
        self.state_names_by_index
            .iter()
            .position(|name| name == state_name)
    }
}

/// Everything a session needs: what to send, and how to read the answers.
#[derive(Debug, Clone, PartialEq)]
pub struct CompiledGraphSet {
    pub set_version: i64,
    pub graphs_by_slot: Vec<CompiledGraph>,
    pub upload_messages: Vec<UploadMessage>,

    /// What this set costs, against what the board had. Reported rather than
    /// only checked, because "you have room for two more graphs" is the useful
    /// form of a capacity, and it is what `POST /api/session/graphs` should
    /// say. Ordered so the six pools print in the same order every time.
    pub pool_usage: IndexMap<String, i64>,
    pub pool_capacity: IndexMap<String, i64>,
}

impl CompiledGraphSet {
    pub fn graph_named(&self, graph_name: &str) -> Result<&CompiledGraph, CompileError> {
        self.graphs_by_slot
            .iter()
            .find(|graph| graph.name == graph_name)
            .ok_or_else(|| {
                let known = self
                    .graphs_by_slot
                    .iter()
                    .map(|graph| graph.name.as_str())
                    .collect::<Vec<_>>()
                    .join(", ");
                CompileError::NotInSet(format!(
                    "this set has no graph called {graph_name:?}. It has: {known}"
                ))
            })
    }

    /// What `configure`'s `graph_index` must be for this paradigm.
    ///
    /// The resolution triald never does: it names a graph, and the daemon knows
    /// which slot the set it built put it in.
    pub fn slot_for_graph_name(&self, graph_name: &str) -> Result<usize, CompileError> {
        Ok(self.graph_named(graph_name)?.slot)
    }
}

// --------------------------------------------------------------- private ---

/// The set-global pool of distributions, filled as graphs are compiled.
///
/// Identical distributions are shared, which is the point of the pool being
/// set-global rather than per graph: two paradigms that draw the same 300-700 ms
/// foreperiod should cost one of the thirty-two entries, not two. Distributions
/// that differ in any parameter are separate entries, so sharing can never
/// change what a graph draws.
struct SharedDistributionPool {
    entries: Vec<DurationDistribution>,
    messages: Vec<UploadMessage>,
    choice_options_used: usize,
}

impl SharedDistributionPool {
    fn new() -> Self {
        Self {
            entries: Vec::new(),
            messages: Vec::new(),
            choice_options_used: 0,
        }
    }

    fn index_for(&mut self, distribution: &DurationDistribution) -> usize {
        if let Some(existing_index) = self
            .entries
            .iter()
            .position(|existing| existing == distribution)
        {
            return existing_index;
        }
        let index = self.entries.len();
        self.entries.push(distribution.clone());
        self.messages.push(UploadMessage {
            msg_type: MsgType::GraphDist,
            fields: distribution_wire_fields(index, distribution),
        });
        if let DurationDistribution::Choice { options_ms, .. } = distribution {
            self.choice_options_used += options_ms.len();
        }
        index
    }
}

/// A `json!` object as the body of a message.
fn body(object: Json) -> JsonMap<String, Json> {
    match object {
        Json::Object(fields) => fields,
        _ => unreachable!("every body is written as an object literal"),
    }
}

/// A name the way Python's `!r` quotes it, so a refusal reads the same from
/// either daemon.
fn quoted(name: &str) -> String {
    format!("'{name}'")
}

/// One `graph_dist` body. The wire's `a`/`b`/`c` are positional by `kind`.
///
/// Terse on purpose down there — see PROTOCOL.md 3.2's table — and this is the
/// only place in the daemon that has to know which parameter is which.
fn distribution_wire_fields(
    index: usize,
    distribution: &DurationDistribution,
) -> JsonMap<String, Json> {
    use serde_json::json;
    match distribution {
        DurationDistribution::Fixed { duration_ms } => {
            body(json!({"i": index, "kind": "fixed", "a": duration_ms}))
        }
        DurationDistribution::Uniform {
            minimum_ms,
            maximum_ms,
        } => body(json!({"i": index, "kind": "uniform", "a": minimum_ms, "b": maximum_ms})),
        DurationDistribution::Exponential {
            minimum_ms,
            maximum_ms,
            mean_ms,
        } => body(json!({
            "i": index,
            "kind": "exponential",
            "a": minimum_ms,
            "b": maximum_ms,
            "c": mean_ms,
        })),
        DurationDistribution::Choice {
            options_ms,
            weights,
        } => {
            let mut fields = body(json!({"i": index, "kind": "choice", "opts": options_ms}));
            if let Some(weights) = weights {
                fields.insert("weights".into(), json!(weights));
            }
            fields
        }
    }
}

fn output_action_wire_fields(
    action: &OutputActionSpecification,
    when: &str,
    line_map: &LineMap,
    graph_name: &str,
    timer_index_by_name: &IndexMap<String, usize>,
) -> Result<JsonMap<String, Json>, CompileError> {
    use crate::model::graph_definition::ActionKind;
    use serde_json::json;
    let kind = match action.kind {
        ActionKind::High => "high",
        ActionKind::Low => "low",
        ActionKind::Toggle => "toggle",
        ActionKind::Pulse => "pulse",
        ActionKind::TimerStart => "timer_start",
        ActionKind::TimerCancel => "timer_cancel",
    };
    if matches!(
        action.kind,
        ActionKind::TimerStart | ActionKind::TimerCancel
    ) {
        // `timer`, not `line`: two index spaces of different sizes, and the
        // device bounds-checks them separately for that reason.
        let Some(timer_index) = timer_index_by_name.get(&action.timer) else {
            return refuse(format!(
                "graph {}: no global timer called {}",
                quoted(graph_name),
                quoted(&action.timer)
            ));
        };
        return Ok(body(
            json!({"on": when, "kind": kind, "timer": timer_index}),
        ));
    }
    let line_index = line_map
        .output_line_index_for_name(&action.line)
        .or_else(|problem| refuse(format!("graph {}: {problem}", quoted(graph_name))))?;
    let mut fields = body(json!({"on": when, "line": line_index, "kind": kind}));
    if action.kind == ActionKind::Pulse {
        fields.insert("ms".into(), json!(action.pulse_ms));
    }
    Ok(fields)
}

/// The mask a predicate over these names becomes, timers included.
///
/// A timer name and an input line name live in one namespace here because they
/// live in one word on the device — a running timer *is* a high input line.
/// Timers are looked up first so that a rig which happens to name a line after
/// a timer cannot silently shadow it; the compiler refuses that below.
///
/// The error is the line map's sentence alone; the caller says where it was.
fn predicate_mask(
    line_names: &[String],
    line_map: &LineMap,
    timer_index_by_name: &IndexMap<String, usize>,
    first_timer_line: i64,
) -> Result<u64, String> {
    let mut mask = 0u64;
    for name in line_names {
        let bit = match timer_index_by_name.get(name) {
            Some(timer_index) => first_timer_line + *timer_index as i64,
            None => line_map
                .input_line_index_for_name(name)
                .map_err(|problem| problem.to_string())?,
        };
        // Python's integers have no top; ours do. A bit past the word is a
        // board that declared more lines than it can mask, and says so.
        mask |= u32::try_from(bit)
            .ok()
            .and_then(|bit| 1u64.checked_shl(bit))
            .ok_or_else(|| format!("{} is line {bit}, which no mask can hold", quoted(name)))?;
    }
    Ok(mask)
}

/// The three terms of a predicate, as the wire's `all`/`any`/`none` masks.
///
/// An empty term is left out rather than sent as zero, so a message says only
/// what the graph said.
fn insert_predicate_masks(
    fields: &mut JsonMap<String, Json>,
    predicate: &crate::model::graph_definition::TransitionPredicate,
    line_map: &LineMap,
    timer_index_by_name: &IndexMap<String, usize>,
    first_timer_line: i64,
) -> Result<(), String> {
    for (predicate_key, names) in [
        ("all", &predicate.all),
        ("any", &predicate.any),
        ("none", &predicate.none),
    ] {
        if names.is_empty() {
            continue;
        }
        let mask = predicate_mask(names, line_map, timer_index_by_name, first_timer_line)?;
        fields.insert(predicate_key.into(), Json::from(mask));
    }
    Ok(())
}

/// Where a graph's own name for a distribution landed in the shared pool.
///
/// The document has already been validated, so a miss here is a graph that
/// names a distribution it never declared — refused, not panicked on, because a
/// graph is user data.
fn pool_index(
    distribution_pool_index_by_name: &IndexMap<String, usize>,
    distribution_name: &str,
    graph_name: &str,
) -> Result<usize, CompileError> {
    distribution_pool_index_by_name
        .get(distribution_name)
        .copied()
        .ok_or_else(|| {
            CompileError::Set(format!(
                "graph {}: no distribution called {}",
                quoted(graph_name),
                quoted(distribution_name)
            ))
        })
}

/// Where a state's own name for another state points, counted from zero.
fn state_index(
    state_names: &[&str],
    state_name: &str,
    graph_name: &str,
) -> Result<usize, CompileError> {
    state_names
        .iter()
        .position(|name| *name == state_name)
        .ok_or_else(|| {
            CompileError::Set(format!(
                "graph {}: no state called {}",
                quoted(graph_name),
                quoted(state_name)
            ))
        })
}

/// One `graph_state` body.
///
/// `terminal` and `timeout` are always present, as a code or as null: the
/// protocol distinguishes absent from null and the device refuses the former,
/// rather than guessing which a graph meant.
fn state_wire_fields(
    state: &StateDefinition,
    graph: &GraphDefinition,
    state_names: &[&str],
    distribution_pool_index_by_name: &IndexMap<String, usize>,
) -> Result<JsonMap<String, Json>, CompileError> {
    use serde_json::json;
    let terminal_code = match &state.outcome {
        None => Json::Null,
        Some(outcome) => match terminal_outcome_for_name(outcome) {
            Some(code) => json!(code as i32),
            None => {
                return refuse(format!(
                    "graph {}, state {}: {} is not an outcome a state may declare",
                    quoted(&graph.name),
                    quoted(&state.name),
                    quoted(outcome)
                ))
            }
        },
    };

    let timeout_fields = match &state.timeout {
        None => Json::Null,
        Some(timeout) => json!({
            "dist": pool_index(distribution_pool_index_by_name, &timeout.after, &graph.name)?,
            // Per graph, counted from zero: the device adds this graph's offset.
            "target": state_index(state_names, &timeout.goto, &graph.name)?,
        }),
    };

    let mut fields = body(json!({
        "i": state_index(state_names, &state.name, &graph.name)?,
        "terminal": terminal_code,
        "timeout": timeout_fields,
    }));
    // Omitted rather than sent as null when there is none, unlike the two above:
    // `relight` is optional on the wire precisely so that a graph_state written
    // before it existed still says, unambiguously, that this state does not
    // relight. Sending it always would cost a field on every state of every
    // upload for a thing almost no state uses.
    if let Some(relight_after) = &state.relight_after {
        fields.insert(
            "relight".into(),
            json!(pool_index(
                distribution_pool_index_by_name,
                relight_after,
                &graph.name
            )?),
        );
    }
    Ok(fields)
}

// ---------------------------------------------------------------- public ---

/// Turn the graphs a session will use into the upload
/// docs/reference/protocol.md 3.2 wants.
///
/// The order of `graphs` is the order of the slots, and a slot is what
/// `configure`'s `graph_index` names — so it is stable for the life of the set
/// and is the daemon's to assign, never triald's.
pub fn compile_graph_set_for_device(
    graphs: &[GraphDefinition],
    line_map: &LineMap,
    capabilities: &DeviceCapabilities,
    set_version: i64,
) -> Result<CompiledGraphSet, CompileError> {
    use serde_json::json;

    if graphs.is_empty() {
        return refuse("a set needs at least one graph");
    }
    if graphs.len() as i64 > capabilities.max_graphs {
        return refuse(format!(
            "this session names {} graphs and the board holds {}",
            graphs.len(),
            capabilities.max_graphs
        ));
    }

    let duplicate_names =
        names_appearing_more_than_once(graphs.iter().map(|graph| graph.name.as_str()));
    if !duplicate_names.is_empty() {
        return refuse(format!(
            "a set names the same graph twice: {}",
            duplicate_names.join(", ")
        ));
    }

    // Timer names are pooled across the set, in declaration order, because the
    // device's timers are. A timer outlives the run that started it, so it
    // cannot belong to one graph — the nesting in the source is about where its
    // distributions are declared, nothing more. See model/graph_definition.rs.
    let mut timer_index_by_name: IndexMap<String, usize> = IndexMap::new();
    for graph in graphs {
        for timer_name in graph.timers.keys() {
            if timer_index_by_name.contains_key(timer_name) {
                return refuse(format!(
                    "a set declares the global timer {} twice",
                    quoted(timer_name)
                ));
            }
            let index = timer_index_by_name.len();
            timer_index_by_name.insert(timer_name.clone(), index);
        }
    }
    if timer_index_by_name.len() as i64 > capabilities.max_timers {
        return refuse(format!(
            "this session declares {} global timers and the board holds {}",
            timer_index_by_name.len(),
            capabilities.max_timers
        ));
    }
    // One namespace, because the device has one word: a predicate naming a timer
    // and a predicate naming a lever compile to bits of the same mask. A rig
    // whose input line shares a timer's name would make every such predicate
    // ambiguous, so it is refused here rather than resolved by a rule nobody
    // would remember.
    let mut shadowed: Vec<&str> = line_map
        .input_lines
        .iter()
        .map(|line| line.name.as_str())
        .filter(|name| timer_index_by_name.contains_key(*name))
        .collect();
    shadowed.sort_unstable();
    shadowed.dedup();
    if !shadowed.is_empty() {
        return refuse(format!(
            "these names are both an input line and a global timer: {}",
            shadowed.join(", ")
        ));
    }

    let mut pool = SharedDistributionPool::new();
    let mut compiled_graphs: Vec<CompiledGraph> = Vec::new();
    let mut graph_upload_messages: Vec<UploadMessage> = Vec::new();
    let mut total_state_count = 0usize;
    let mut total_transition_count = 0usize;
    let mut total_output_action_count = 0usize;

    for (slot, graph) in graphs.iter().enumerate() {
        let distribution_pool_index_by_name: IndexMap<String, usize> = graph
            .distributions
            .iter()
            .map(|(name, distribution)| (name.clone(), pool.index_for(distribution)))
            .collect();
        let state_names = graph.state_names_in_declaration_order();
        let graph_quoted = quoted(&graph.name);

        graph_upload_messages.push(UploadMessage {
            msg_type: MsgType::GraphBegin,
            fields: body(json!({
                "slot": slot,
                "n_states": state_names.len(),
                "entry": state_index(&state_names, &graph.entry, &graph.name)?,
            })),
        });

        // Timers before states, because a state's actions may name one, and
        // after the distributions this graph declares, because a timer names
        // those. The device checks both, so the order here is what keeps a
        // legal set from being refused for the wrong reason.
        for (timer_name, timer) in &graph.timers {
            let where_ = || format!("graph {graph_quoted}, timer {}", quoted(timer_name));
            let mut fields = body(json!({
                "i": timer_index_by_name[timer_name],
                "width": pool_index(&distribution_pool_index_by_name, &timer.width, &graph.name)?,
            }));
            if let Some(delay) = &timer.delay {
                fields.insert(
                    "delay".into(),
                    json!(pool_index(
                        &distribution_pool_index_by_name,
                        delay,
                        &graph.name
                    )?),
                );
            }
            if let Some(gap) = &timer.gap {
                fields.insert(
                    "gap".into(),
                    json!(pool_index(
                        &distribution_pool_index_by_name,
                        gap,
                        &graph.name
                    )?),
                );
            }
            if !timer.line.is_empty() {
                let line_index = line_map
                    .output_line_index_for_name(&timer.line)
                    .or_else(|problem| refuse(format!("{}: {problem}", where_())))?;
                fields.insert("line".into(), json!(line_index));
            }
            if let Some(when) = &timer.when {
                insert_predicate_masks(
                    &mut fields,
                    when,
                    line_map,
                    &timer_index_by_name,
                    capabilities.first_timer_line,
                )
                .or_else(|problem| refuse(format!("{}: {problem}", where_())))?;
            }
            if timer.loops != 1 {
                fields.insert("loops".into(), json!(timer.loops));
            }
            if timer.active_low {
                fields.insert("active_low".into(), json!(true));
            }
            if timer.trial_bound {
                fields.insert("trial_bound".into(), json!(true));
            }
            graph_upload_messages.push(UploadMessage {
                msg_type: MsgType::GraphTimer,
                fields,
            });
        }

        let mut transition_targets_by_state_index: Vec<Vec<String>> = Vec::new();
        let mut graph_transition_count = 0usize;
        let mut graph_output_action_count = 0usize;

        for state in &graph.states {
            graph_upload_messages.push(UploadMessage {
                msg_type: MsgType::GraphState,
                fields: state_wire_fields(
                    state,
                    graph,
                    &state_names,
                    &distribution_pool_index_by_name,
                )?,
            });

            // Transitions before actions, and every one of a state's own before
            // the next state: the device stores them as a (first, count) slice
            // of a shared pool, so the ordering rule on the wire *is* its memory
            // invariant (PROTOCOL.md 3.2).
            transition_targets_by_state_index.push(
                state
                    .transitions
                    .iter()
                    .map(|transition| transition.goto.clone())
                    .collect(),
            );
            for transition in &state.transitions {
                let mut fields = body(json!({
                    "target": state_index(&state_names, &transition.goto, &graph.name)?,
                }));
                // Timers are in this word too, so "when the foreperiod timer
                // ends" is `none: [foreperiod]` and needs no vocabulary of its
                // own.
                insert_predicate_masks(
                    &mut fields,
                    &transition.when,
                    line_map,
                    &timer_index_by_name,
                    capabilities.first_timer_line,
                )
                .or_else(|problem| {
                    refuse(format!(
                        "graph {graph_quoted}, state {}: {problem}",
                        quoted(&state.name)
                    ))
                })?;
                if let Some(hold) = &transition.hold {
                    fields.insert(
                        "hold".into(),
                        json!(pool_index(
                            &distribution_pool_index_by_name,
                            hold,
                            &graph.name
                        )?),
                    );
                }
                if transition.fire_if_already_true_on_entry {
                    fields.insert("level".into(), json!(true));
                }
                graph_upload_messages.push(UploadMessage {
                    msg_type: MsgType::GraphTransition,
                    fields,
                });
                graph_transition_count += 1;
            }

            // Entry actions before exit actions, always: they are two slices of
            // one pool, and interleaving them would silently give one slice the
            // other's members.
            for (when, actions) in [("entry", &state.on_entry), ("exit", &state.on_exit)] {
                for action in actions {
                    graph_upload_messages.push(UploadMessage {
                        msg_type: MsgType::GraphAction,
                        fields: output_action_wire_fields(
                            action,
                            when,
                            line_map,
                            &graph.name,
                            &timer_index_by_name,
                        )?,
                    });
                    graph_output_action_count += 1;
                }
            }
        }

        graph_upload_messages.push(UploadMessage {
            msg_type: MsgType::GraphEnd,
            fields: body(json!({
                // This graph's own totals, not the set's: a host that miscounted
                // one graph should be told which graph.
                "n_transitions": graph_transition_count,
                "n_output_actions": graph_output_action_count,
            })),
        });

        total_state_count += state_names.len();
        total_transition_count += graph_transition_count;
        total_output_action_count += graph_output_action_count;
        compiled_graphs.push(CompiledGraph {
            name: graph.name.clone(),
            slot,
            state_names_by_index: state_names.iter().map(|name| name.to_string()).collect(),
            transition_target_names_by_state_index: transition_targets_by_state_index,
            distribution_pool_index_by_name,
        });
    }

    let pool_usage: IndexMap<String, i64> = [
        ("graphs", graphs.len()),
        ("states", total_state_count),
        ("transitions", total_transition_count),
        ("output_actions", total_output_action_count),
        ("distributions", pool.entries.len()),
        ("choice_options", pool.choice_options_used),
    ]
    .into_iter()
    .map(|(pool_name, used)| (pool_name.to_string(), used as i64))
    .collect();
    let pool_capacity: IndexMap<String, i64> = [
        ("graphs", capabilities.max_graphs),
        ("states", capabilities.max_states),
        ("transitions", capabilities.max_transitions),
        ("output_actions", capabilities.max_output_actions),
        ("distributions", capabilities.max_distributions),
        ("choice_options", capabilities.max_choice_options),
    ]
    .into_iter()
    .map(|(pool_name, capacity)| (pool_name.to_string(), capacity))
    .collect();
    refuse_a_set_that_does_not_fit(&pool_usage, &pool_capacity, graphs)?;

    let mut upload_messages = vec![UploadMessage {
        msg_type: MsgType::SetBegin,
        fields: body(json!({"set_version": set_version, "n_graphs": graphs.len()})),
    }];
    // The whole shared pool first, at set level. Legal either way — a
    // distribution may travel with the graph that introduces it — but a set
    // that shares entries between graphs has no single graph to put them with,
    // and "the pool belongs to the set" is the truth worth showing.
    upload_messages.extend(pool.messages);
    upload_messages.extend(graph_upload_messages);
    upload_messages.push(UploadMessage {
        msg_type: MsgType::SetEnd,
        fields: body(json!({
            "n_states": total_state_count,
            "n_transitions": total_transition_count,
            "n_output_actions": total_output_action_count,
            // `checksum` is added by the uploader, over the bytes it sends.
        })),
    });

    Ok(CompiledGraphSet {
        set_version,
        graphs_by_slot: compiled_graphs,
        upload_messages,
        pool_usage,
        pool_capacity,
    })
}

/// Every name that appears more than once, sorted, each once.
fn names_appearing_more_than_once<'a>(names: impl Iterator<Item = &'a str>) -> Vec<&'a str> {
    let mut seen = std::collections::HashSet::new();
    let mut repeated: Vec<&str> = names.filter(|name| !seen.insert(*name)).collect();
    repeated.sort_unstable();
    repeated.dedup();
    repeated
}

/// Name what overflowed, and by how much, and which graphs are in the set.
///
/// The failure this prevents is discovering at trial 40 that one trial type
/// names a graph with forty states on a thirty-two-state board, and losing the
/// session to it — so the message has to be actionable at the moment a session
/// is being set up, minutes before an animal is in the booth.
fn refuse_a_set_that_does_not_fit(
    pool_usage: &IndexMap<String, i64>,
    pool_capacity: &IndexMap<String, i64>,
    graphs: &[GraphDefinition],
) -> Result<(), CompileError> {
    let overflowing: Vec<String> = pool_usage
        .iter()
        .filter(|(pool_name, used)| **used > pool_capacity[*pool_name])
        .map(|(pool_name, used)| {
            format!(
                "{pool_name}: {used} needed, {} available",
                pool_capacity[pool_name]
            )
        })
        .collect();
    if overflowing.is_empty() {
        return Ok(());
    }
    let graph_names: Vec<&str> = graphs.iter().map(|graph| graph.name.as_str()).collect();
    refuse(format!(
        "this session's graphs do not fit the board. {}. The set is: {}",
        overflowing.join("; "),
        graph_names.join(", ")
    ))
}
