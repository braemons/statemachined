// SPDX-License-Identifier: LGPL-3.0-or-later
//
// `<statemachined-graph>` -- the store, the editor and the diagram.
//
// Two decisions here are worth stating.
//
// **The predicate editor is checkboxes over *named* lines.** A predicate is
// three masks on the wire, and a UI that showed three masks would push the one
// translation this system exists to do -- names to indices -- back onto the
// person. The names come from the rig's line map, so a graph authored here
// cannot name a line this box does not have.
//
// **The three columns are independent, and the checkboxes cannot say so.** A
// predicate is `all` AND `any` AND `none`, evaluated as three masks, so a line
// may be ticked in two columns -- and two of the three ways of doing that mean
// something nobody intends. "L, and either M or N", written as all:[L] with
// any:[L,M,N], is just "L": `all` already requires L high, so the `any` clause
// is satisfied whenever the predicate could fire, and M and N are ignored. No
// arrangement of checkboxes makes that visible. So the predicate is written out
// in a sentence underneath, and the traps are named where they are made --
// see transition_predicate.js, and model/graph_definition.py for the one the
// daemon refuses outright.
//
// **Every edit is re-rendered from the graph object**, not patched into the
// DOM, and the fields commit on `change` rather than `input` -- so a re-render
// never lands inside somebody's typing. It is the cheapest correct thing
// without a framework, and this form is nowhere near large enough for the cost
// to matter.
//
// The editor writes the *authored* form (`model/graph_definition.py`): no
// indices anywhere, because an index is a fact about one board's pools and a
// paradigm should outlive the board it was first run on.

import { BasePanelElement, defineElementOnce } from "./base_panel_element.js";
import { describePredicate, predicateProblems } from "./transition_predicate.js";
import {
  GRAPH_DIAGRAM_STYLE_TEXT,
  describeActions,
  renderGraphNodeDiagram,
} from "./graph_node_diagram.js";

/// The outcomes a terminal state may declare, and the empty string for a state
/// that is not terminal. These are `model/trial_outcome.py`'s
/// DECLARABLE_TERMINAL_OUTCOMES, spelled the same way -- a `.tdr` wire contract
/// that has been in every record the lab has written. The copy is checked
/// against the Python by tests/unit/test_web_user_interface_routes.py, because
/// the failure it prevents is a paradigm author picking a spelling from a menu
/// that the store then refuses.
const OUTCOME_NAMES = [
  "",
  "NOT_STARTED",
  "HIT",
  "WRONG_RESPONSE",
  "EARLY_HIT",
  "EARLY_WRONG_RESPONSE",
  "EARLY",
  "LATE",
  "EYE_ERROR",
  "UNEXPECTED_START_SIGNAL",
  "WRONG_START_SIGNAL",
  "CANCELLED",
];

const ACTION_KINDS = ["high", "low", "toggle", "pulse"];
const DISTRIBUTION_KINDS = ["fixed", "uniform", "exponential", "choice"];

export class GraphStorePanelElement extends BasePanelElement {
  static observedAttributes = ["base", "name"];

  constructor() {
    super();
    this.graph = null;
    this.storedGraphNames = [];
    this.inputLineNames = [];
    this.outputLineNames = [];
    this.hasUnsavedEdits = false;
    this.lastValidation = null;
  }

  renderShell() {
    const diagramStyle = document.createElement("style");
    diagramStyle.textContent = GRAPH_DIAGRAM_STYLE_TEXT;
    this.failureSlot = this.make("div", { class: "failure-slot" });
    this.chooser = this.make("div", { class: "row" });
    this.body = this.make("div", { text: "reading the store..." });
    this.root.append(
      diagramStyle,
      this.make("section", {}, [
        this.make("h2", {}, [this.make("span", { text: "Graphs" })]),
        this.chooser,
        this.make("p", {
          class: "muted",
          text:
            "A graph is one paradigm: the states a trial passes through, what moves it " +
            "between them, and which states end it as which outcome. They are written " +
            "against line names rather than pin numbers, so a graph runs on any rig wired " +
            "for it. Editing here changes the stored file; a session decides which of them " +
            "the board holds.",
        }),
        this.failureSlot,
        this.body,
      ]),
    );
  }

  async start() {
    await this.attempt(async () => {
      const listing = await this.api.listStoredGraphs();
      this.storedGraphNames = listing.graphs.map((graph) => graph.name);
      const lines = await this.api.readDeviceLines().catch(() => ({
        input_lines: [],
        output_lines: [],
      }));
      this.inputLineNames = lines.input_lines.map((line) => line.name);
      this.outputLineNames = lines.output_lines.map((line) => line.name);
      const wanted = this.getAttribute("name") || this.storedGraphNames[0];
      if (wanted) await this.load(wanted);
      else this.paint();
    });
  }

  async load(name) {
    if (this.hasUnsavedEdits && !confirm(`Discard unsaved edits to ${this.graph?.name}?`)) return;
    const graph = await this.attempt(() => this.api.readStoredGraph(name));
    if (graph === null) return;
    this.graph = graph;
    this.hasUnsavedEdits = false;
    this.lastValidation = null;
    this.paint();
  }

  edited() {
    this.hasUnsavedEdits = true;
    // A validation is about a graph, and this is no longer that graph.
    this.lastValidation = null;
    // The whole editor is rebuilt, because a renamed state has to appear in
    // every transition that names it -- so the cursor has to be put back. The
    // edit that triggers this is a `change`, which fires on blur, so what is
    // usually restored is the field the person just tabbed *into*.
    this.repaintPreservingFocus(() => this.paint());
  }

  // --------------------------------------------------------------- paint ---

  paint() {
    this.paintChooser();
    if (this.graph === null) {
      this.body.replaceChildren(
        this.make("p", { class: "muted", text: "The store holds no graphs yet." }),
      );
      return;
    }
    this.body.replaceChildren(
      this.actionRow(),
      this.validationSummary(),
      this.make("div", { class: "scroller" }, [renderGraphNodeDiagram(this.graph)]),
      this.make("h3", { text: "Distributions" }),
      this.distributionTable(),
      this.make("h3", { text: "States" }),
      ...this.graph.states.map((state) => this.stateBlock(state)),
      this.make("div", { class: "row" }, [
        this.make("button", { text: "+ state", onClick: () => this.addState() }),
      ]),
    );
  }

  paintChooser() {
    const select = this.make("select", {
      onChange: (event) => this.load(event.target.value),
    });
    for (const name of this.storedGraphNames) {
      select.append(this.make("option", { value: name, text: name, selected: name === this.graph?.name }));
    }
    this.chooser.replaceChildren(
      select,
      this.make("button", { text: "+ new", onClick: () => this.newGraph() }),
      this.make("span", { class: "spacer" }),
      this.hasUnsavedEdits
        ? this.make("span", { class: "pill warn", text: "unsaved" })
        : this.make("span", { class: "pill", text: "saved" }),
    );
  }

  actionRow() {
    return this.make("div", { class: "row" }, [
      this.make("button", {
        class: "primary",
        text: "save to the store",
        disabled: !this.hasUnsavedEdits,
        onClick: () => this.save(),
      }),
      this.make("button", {
        text: "validate against this board",
        title:
          "Every rule, plus this device's caps. Changes nothing and uploads nothing -- " +
          "the capacity half is the useful half.",
        onClick: () => this.validate(),
      }),
      this.make("button", {
        text: "upload as a set of one",
        title:
          "The bench path: try this graph out. It replaces whatever set is committed, " +
          "so it is refused while a session's set is in place.",
        onClick: () => this.uploadAlone(),
      }),
      this.make("span", { class: "spacer" }),
      this.make("button", {
        class: "danger",
        text: "delete",
        onClick: () => this.deleteGraph(),
      }),
    ]);
  }

  validationSummary() {
    if (this.lastValidation === null) return this.make("div");
    const result = this.lastValidation;
    // A graph can be valid and still say something narrower than its author
    // meant, so these are shown either way -- next to "valid on this board",
    // which is otherwise the last word a person reads before running it.
    const warnings = (result.warnings || []).map((warning) =>
      this.make("p", {
        class: "warn",
        text: `${warning.state}, transition ${warning.transition}: ${warning.detail}`,
      }),
    );
    if (!result.valid) {
      return this.make("div", {}, [
        this.make("p", { class: "bad", text: result.detail }),
        ...warnings,
      ]);
    }
    const usage = result.pool_usage || {};
    const capacity = result.pool_capacity || {};
    const table = this.make("table", {}, [
      this.make("thead", {}, [
        this.make("tr", {}, [
          this.make("th", { text: "pool" }),
          this.make("th", { text: "this graph" }),
          this.make("th", { text: "the board holds" }),
        ]),
      ]),
      this.make(
        "tbody",
        {},
        Object.keys(usage).map((pool) =>
          this.make("tr", {}, [
            this.make("td", { text: pool }),
            this.make("td", { text: `${usage[pool]}` }),
            this.make("td", { text: `${capacity[pool] ?? "?"}` }),
          ]),
        ),
      ),
    ]);
    return this.make("div", {}, [
      this.make("p", { class: "good", text: "valid on this board" }),
      ...warnings,
      table,
    ]);
  }

  // ------------------------------------------------------- distributions ---

  distributionTable() {
    const rows = Object.entries(this.graph.distributions || {}).map(([name, distribution]) =>
      this.make("tr", {}, [
        this.make("td", {}, [
          this.make("input", {
            type: "text",
            value: name,
            onChange: (event) => this.renameDistribution(name, event.target.value),
          }),
        ]),
        this.make("td", {}, [
          this.selectField(DISTRIBUTION_KINDS, distribution.kind, (kind) => {
            this.graph.distributions[name] = blankDistribution(kind);
            this.edited();
          }),
        ]),
        this.make("td", {}, this.distributionFields(distribution)),
        this.make("td", {}, [
          this.make("button", {
            class: "danger",
            text: "remove",
            onClick: () => {
              delete this.graph.distributions[name];
              this.edited();
            },
          }),
        ]),
      ]),
    );
    return this.make("div", {}, [
      this.make("table", {}, [
        this.make("thead", {}, [
          this.make("tr", {}, [
            this.make("th", { text: "name" }),
            this.make("th", { text: "kind" }),
            this.make("th", { text: "parameters, in milliseconds" }),
            this.make("th", { text: "" }),
          ]),
        ]),
        this.make("tbody", {}, rows),
      ]),
      this.make("div", { class: "row" }, [
        this.make("button", {
          text: "+ distribution",
          onClick: () => {
            this.graph.distributions = this.graph.distributions || {};
            this.graph.distributions[uniqueName("duration", this.graph.distributions)] =
              blankDistribution("fixed");
            this.edited();
          },
        }),
        this.make("span", {
          class: "muted",
          text: "Drawn on the device, per visit -- which is why a duration is named here and not a number in a state.",
        }),
      ]),
    ]);
  }

  distributionFields(distribution) {
    const numberFor = (key) =>
      this.make("label", { class: "row" }, [
        this.make("span", { class: "muted", text: key.replace(/_ms$/, "") }),
        this.make("input", {
          type: "number",
          min: "0",
          value: `${distribution[key] ?? 0}`,
          onChange: (event) => {
            distribution[key] = Number(event.target.value);
            this.edited();
          },
        }),
      ]);
    if (distribution.kind === "fixed") return [numberFor("duration_ms")];
    if (distribution.kind === "uniform") return [numberFor("minimum_ms"), numberFor("maximum_ms")];
    if (distribution.kind === "exponential") {
      return [numberFor("minimum_ms"), numberFor("maximum_ms"), numberFor("mean_ms")];
    }
    return [
      this.make("input", {
        type: "text",
        value: (distribution.options_ms || []).join(", "),
        title: "milliseconds, comma separated",
        onChange: (event) => {
          distribution.options_ms = event.target.value
            .split(",")
            .map((piece) => Number(piece.trim()))
            .filter((value) => Number.isFinite(value));
          this.edited();
        },
      }),
    ];
  }

  renameDistribution(oldName, newName) {
    if (!newName || newName === oldName) return;
    // Rename every reference too. A distribution is named from timeouts and
    // holds, and leaving those pointing at a name that no longer exists would
    // turn a rename into a graph the compiler refuses.
    const distributions = {};
    for (const [name, value] of Object.entries(this.graph.distributions)) {
      distributions[name === oldName ? newName : name] = value;
    }
    this.graph.distributions = distributions;
    for (const state of this.graph.states) {
      if (state.timeout?.after === oldName) state.timeout.after = newName;
      for (const transition of state.transitions || []) {
        if (transition.hold === oldName) transition.hold = newName;
      }
    }
    this.edited();
  }

  // -------------------------------------------------------------- states ---

  stateBlock(state) {
    const stateNames = this.graph.states.map((each) => each.name);
    const distributionNames = Object.keys(this.graph.distributions || {});
    const isTerminal = Boolean(state.outcome);

    const heading = this.make("div", { class: "row" }, [
      this.make("input", {
        type: "text",
        value: state.name,
        onChange: (event) => this.renameState(state, event.target.value),
      }),
      state.name === this.graph.entry
        ? this.make("span", { class: "pill good", text: "entry" })
        : this.make("button", {
            text: "make entry",
            onClick: () => {
              this.graph.entry = state.name;
              this.edited();
            },
          }),
      this.make("label", { class: "row" }, [
        this.make("span", { class: "muted", text: "outcome" }),
        this.selectField(OUTCOME_NAMES, state.outcome || "", (outcome) => {
          state.outcome = outcome || null;
          if (state.outcome) {
            // Nothing exits a terminal state: it is where the trial ends and
            // what it ended as. Clearing these here is what stops the store
            // refusing a graph whose editor let it be written.
            state.timeout = null;
            state.transitions = [];
          }
          this.edited();
        }),
      ]),
      this.make("span", { class: "spacer" }),
      this.make("button", {
        class: "danger",
        text: "remove",
        onClick: () => {
          this.graph.states = this.graph.states.filter((each) => each !== state);
          this.edited();
        },
      }),
    ]);

    const children = [heading, this.actionEditor(state, "on_entry"), this.actionEditor(state, "on_exit")];
    if (!isTerminal) {
      children.push(this.timeoutEditor(state, stateNames, distributionNames));
      children.push(this.transitionEditor(state, stateNames, distributionNames));
    } else {
      children.push(
        this.make("p", {
          class: "muted",
          text: "Terminal. Nothing leaves it -- the trial ends here, as this outcome.",
        }),
      );
    }
    return this.make("div", { style: "border-top:1px solid var(--panel-border);padding:0.5rem 0" }, children);
  }

  renameState(state, newName) {
    if (!newName || newName === state.name) return;
    const oldName = state.name;
    state.name = newName;
    if (this.graph.entry === oldName) this.graph.entry = newName;
    for (const each of this.graph.states) {
      if (each.timeout?.goto === oldName) each.timeout.goto = newName;
      for (const transition of each.transitions || []) {
        if (transition.goto === oldName) transition.goto = newName;
      }
    }
    this.edited();
  }

  actionEditor(state, key) {
    const actions = state[key] || [];
    const rows = actions.map((action) =>
      this.make("div", { class: "row" }, [
        this.selectField(this.outputLineNames, action.line, (line) => {
          action.line = line;
          this.edited();
        }),
        this.selectField(ACTION_KINDS, action.kind, (kind) => {
          action.kind = kind;
          action.pulse_ms = kind === "pulse" ? action.pulse_ms || 40 : null;
          this.edited();
        }),
        action.kind === "pulse"
          ? this.make("input", {
              type: "number",
              min: "1",
              value: `${action.pulse_ms ?? 40}`,
              title: "milliseconds, served by the device's own clock",
              onChange: (event) => {
                action.pulse_ms = Number(event.target.value);
                this.edited();
              },
            })
          : null,
        this.make("button", {
          class: "danger",
          text: "-",
          onClick: () => {
            state[key] = actions.filter((each) => each !== action);
            this.edited();
          },
        }),
      ]),
    );
    return this.make("div", {}, [
      this.make("div", { class: "row" }, [
        this.make("span", { class: "muted", text: key === "on_entry" ? "on entry" : "on exit" }),
        this.make("button", {
          text: "+ action",
          onClick: () => {
            state[key] = [
              ...(state[key] || []),
              { line: this.outputLineNames[0] || "", kind: "high", pulse_ms: null },
            ];
            this.edited();
          },
        }),
        actions.length === 0
          ? this.make("span", { class: "muted", text: describeActions(actions) || "nothing" })
          : null,
      ]),
      ...rows,
    ]);
  }

  timeoutEditor(state, stateNames, distributionNames) {
    if (!state.timeout) {
      return this.make("div", { class: "row" }, [
        this.make("span", { class: "muted", text: "timeout" }),
        this.make("button", {
          text: "+ timeout",
          disabled: distributionNames.length === 0,
          title:
            distributionNames.length === 0
              ? "A timeout leaves after a drawn duration, so it needs a distribution first."
              : "",
          onClick: () => {
            state.timeout = { after: distributionNames[0], goto: stateNames[0] };
            this.edited();
          },
        }),
      ]);
    }
    return this.make("div", { class: "row" }, [
      this.make("span", { class: "muted", text: "after" }),
      this.selectField(distributionNames, state.timeout.after, (after) => {
        state.timeout.after = after;
        this.edited();
      }),
      this.make("span", { class: "muted", text: "go to" }),
      this.selectField(stateNames, state.timeout.goto, (goto) => {
        state.timeout.goto = goto;
        this.edited();
      }),
      this.make("button", {
        class: "danger",
        text: "-",
        onClick: () => {
          state.timeout = null;
          this.edited();
        },
      }),
    ]);
  }

  transitionEditor(state, stateNames, distributionNames) {
    const transitions = state.transitions || [];
    const rows = transitions.map((transition) =>
      this.make("div", { style: "padding:0.25rem 0 0.25rem 0.75rem" }, [
        this.make("div", { class: "row" }, [
          this.make("span", { class: "muted", text: "go to" }),
          this.selectField(stateNames, transition.goto, (goto) => {
            transition.goto = goto;
            this.edited();
          }),
          this.make("label", { class: "row", title: "The predicate must hold continuously for a drawn duration -- 'the lever is held down', not 'the lever was touched'." }, [
            this.make("span", { class: "muted", text: "held for" }),
            this.selectField(["", ...distributionNames], transition.hold || "", (hold) => {
              transition.hold = hold || null;
              this.edited();
            }),
          ]),
          this.make("label", { class: "row", title: "Off by default, so a switch already held when the state is entered does not carry the trial straight through it." }, [
            this.make("input", {
              type: "checkbox",
              checked: Boolean(transition.fire_if_already_true_on_entry),
              onChange: (event) => {
                transition.fire_if_already_true_on_entry = event.target.checked;
                this.edited();
              },
            }),
            this.make("span", { class: "muted", text: "fire if already true on entry" }),
          ]),
          this.make("span", { class: "spacer" }),
          this.make("button", {
            class: "danger",
            text: "-",
            onClick: () => {
              state.transitions = transitions.filter((each) => each !== transition);
              this.edited();
            },
          }),
        ]),
        this.predicateEditor(transition.when || (transition.when = { all: [], any: [], none: [] })),
      ]),
    );
    return this.make("div", {}, [
      this.make("div", { class: "row" }, [
        this.make("span", { class: "muted", text: "transitions" }),
        this.make("button", {
          text: "+ transition",
          onClick: () => {
            state.transitions = [
              ...transitions,
              {
                when: { all: [], any: [], none: [] },
                goto: stateNames[0],
                hold: null,
                fire_if_already_true_on_entry: false,
              },
            ];
            this.edited();
          },
        }),
      ]),
      ...rows,
    ]);
  }

  /// Three masks as three rows of checkboxes over the rig's named input lines.
  predicateEditor(predicate) {
    const rowFor = (key, label, title) =>
      this.make("div", { class: "row", title }, [
        this.make("span", { class: "muted", style: "width:3rem", text: label }),
        ...this.inputLineNames.map((name) =>
          this.make("label", { class: "row", style: "gap:0.2rem" }, [
            this.make("input", {
              type: "checkbox",
              checked: (predicate[key] || []).includes(name),
              onChange: (event) => {
                const chosen = new Set(predicate[key] || []);
                if (event.target.checked) chosen.add(name);
                else chosen.delete(name);
                predicate[key] = [...chosen];
                this.edited();
              },
            }),
            this.make("span", { text: name }),
          ]),
        ),
        this.inputLineNames.length === 0
          ? this.make("span", {
              class: "warn",
              text: "no line map -- connect a device or set one in the config",
            })
          : null,
      ]);
    return this.make("div", {}, [
      rowFor("all", "all", "every one of these lines is high"),
      rowFor("any", "any", "at least one of these lines is high; ticking none of them means 'don't care'"),
      rowFor("none", "none", "none of these lines is high"),
      // The three columns are independent masks and a line may be ticked in
      // two of them, so what the boxes add up to is said in words underneath.
      // "L, and either M or N" written as all:[L] any:[L,M,N] is just "L", and
      // no arrangement of checkboxes makes that visible on its own.
      this.make("div", { class: "muted", style: "margin-top:0.25rem" }, [
        this.make("span", { text: describePredicate(predicate) || "names no lines, so it would always fire" }),
      ]),
      ...predicateProblems(predicate).map((problem) =>
        this.make("div", {
          class: problem.severity === "error" ? "failure" : "warn",
          text: `${problem.severity === "error" ? "cannot fire" : "no effect"}: ${problem.detail}`,
        }),
      ),
    ]);
  }

  // ------------------------------------------------------------- helpers ---

  selectField(options, chosen, onChoose) {
    const select = this.make("select", {
      onChange: (event) => onChoose(event.target.value),
    });
    for (const option of options) {
      select.append(
        this.make("option", { value: option, text: option || "-", selected: option === chosen }),
      );
    }
    return select;
  }

  addState() {
    const name = uniqueName("State", Object.fromEntries(this.graph.states.map((s) => [s.name, s])));
    this.graph.states.push({
      name,
      outcome: null,
      on_entry: [],
      on_exit: [],
      timeout: null,
      transitions: [],
    });
    if (!this.graph.entry) this.graph.entry = name;
    this.edited();
  }

  newGraph() {
    const name = prompt("A name for the new graph");
    if (!name) return;
    this.graph = {
      name,
      entry: "Start",
      distributions: { hold_ms: { kind: "fixed", duration_ms: 500 } },
      states: [
        {
          name: "Start",
          outcome: null,
          on_entry: [],
          on_exit: [],
          timeout: { after: "hold_ms", goto: "Done" },
          transitions: [],
        },
        { name: "Done", outcome: "HIT", on_entry: [], on_exit: [], timeout: null, transitions: [] },
      ],
    };
    this.hasUnsavedEdits = true;
    this.paint();
  }

  async save() {
    const saved = await this.attempt(() => this.api.writeStoredGraph(this.graph.name, this.graph));
    if (saved === null) return;
    this.hasUnsavedEdits = false;
    if (!this.storedGraphNames.includes(this.graph.name)) {
      this.storedGraphNames = [...this.storedGraphNames, this.graph.name].sort();
    }
    this.paint();
  }

  async validate() {
    if (this.hasUnsavedEdits) {
      // The daemon validates what is *stored*, so validating an edited graph
      // would answer about the wrong one. Say that rather than answer.
      this.showFailure(new Error("save first: this checks the stored graph, not the edited one"));
      return;
    }
    this.lastValidation = await this.attempt(() => this.api.validateStoredGraph(this.graph.name));
    this.paint();
  }

  async uploadAlone() {
    const uploaded = await this.attempt(() => this.api.uploadOneGraph(this.graph.name));
    if (uploaded === null) return;
    this.showUploadResult(uploaded);
  }

  showUploadResult(uploaded) {
    this.body.prepend(
      this.make("p", {
        class: "good",
        text:
          `uploaded as set v${uploaded.set_version} in ${uploaded.elapsed_milliseconds} ms; ` +
          `it is now the only graph on the board`,
      }),
    );
  }

  async deleteGraph() {
    if (!confirm(`Delete ${this.graph.name} from the store?`)) return;
    const deleted = await this.attempt(() => this.api.deleteStoredGraph(this.graph.name));
    if (deleted === null) return;
    this.storedGraphNames = this.storedGraphNames.filter((name) => name !== this.graph.name);
    this.graph = null;
    this.hasUnsavedEdits = false;
    if (this.storedGraphNames.length > 0) await this.load(this.storedGraphNames[0]);
    else this.paint();
  }
}

function blankDistribution(kind) {
  if (kind === "fixed") return { kind, duration_ms: 500 };
  if (kind === "uniform") return { kind, minimum_ms: 250, maximum_ms: 750 };
  if (kind === "exponential") return { kind, minimum_ms: 250, maximum_ms: 1500, mean_ms: 500 };
  return { kind: "choice", options_ms: [250, 500, 1000], weights: null };
}

function uniqueName(stem, taken) {
  let candidate = stem;
  let suffix = 2;
  while (candidate in taken) candidate = `${stem}${suffix++}`;
  return candidate;
}

defineElementOnce("statemachined-graph", GraphStorePanelElement);
