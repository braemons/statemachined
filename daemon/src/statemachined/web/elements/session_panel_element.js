// SPDX-License-Identifier: LGPL-3.0-or-later
//
// `<statemachined-session>` -- the set a session will use, the trial running
// now, and what the last one did.
//
// The set upload is at the top because that is the order a session happens in:
// declare every graph the session will use, before an animal is in the booth,
// and find out *there* that one of them does not fit rather than at trial 40.
// The panel says what it cost, in milliseconds, for the same reason the API
// returns it -- tens of seconds on a UART rig is a fact somebody should see
// once rather than rediscover.
//
// The trial controls are here too, and they are honestly labelled: on a real
// rig **triald drives this loop**. What a person needs them for is the bench --
// arming one trial by hand to watch a valve open.
//
// **Three slots, painted by three different things.** The chooser is the
// person's -- a set of checkboxes -- and is repainted only when the person or
// the store changes it. The live state and the last trial are the rig's and are
// repainted from a stream and a poll, several times a second. They are separate
// subtrees because a stream frame that rebuilt the chooser would take the
// keyboard out of it: the element you had focused is removed from the document,
// and the focus goes with it.

import { BasePanelElement, defineElementOnce } from "./base_panel_element.js";
import { formatDeviceMicroseconds } from "./device_panel_element.js";

export class SessionPanelElement extends BasePanelElement {
  constructor() {
    super();
    this.chosenGraphNames = new Set();
    this.storedGraphs = [];
    this.state = null;
    this.lastResult = null;
    this.uploadNote = "";
    this.session = null;
    //: The next trial to arm by hand. A number a person can overwrite, because
    //: on a rig triald owns trial ids and on a bench nobody does -- and two
    //: trials sharing an id is how a trace stops being joinable.
    this.nextTrialId = 1;
    //: What the board would do with nobody attached. Read from the device
    //: rather than remembered here: it is stored on the board, it outlives this
    //: page, and it survives the greeting that took the rig back.
    this.autorun = null;
    this.saveNote = "";
  }

  renderShell() {
    this.failureSlot = this.make("div", { class: "failure-slot" });
    // Three slots, filled independently. See the note at the top of this file.
    this.chooserSlot = this.make("div", { text: "reading the store..." });
    this.manualSlot = this.make("div");
    this.standaloneSlot = this.make("div");
    this.liveSlot = this.make("div");
    this.lastTrialSlot = this.make("div");
    this.root.replaceChildren(
      this.make("section", {}, [
        this.make("h2", {}, [this.make("span", { text: "Session" })]),
        // What a "session" is, said once, where somebody who has never read
        // dev/DAEMON.md is looking. The panel is used inside a console with no
        // tabs and no shell, so it cannot rely on anything around it.
        this.make("p", {
          class: "muted",
          text:
            "A session is one run of an experiment: the set of paradigms this rig will use " +
            "for it, loaded onto the board once before an animal is in the booth. After that " +
            "a trial names one of them and starts in milliseconds, because the graphs are " +
            "already there -- which is the whole reason the upload happens here and not per " +
            "trial. Below: what is loaded, what the machine is doing now, and what the last " +
            "trial did.",
        }),
        this.failureSlot,
        this.chooserSlot,
        this.make("h3", { text: "Run a trial by hand" }),
        this.manualSlot,
        this.make("h3", { text: "Run without the daemon" }),
        this.standaloneSlot,
        this.make("h3", { text: "Now" }),
        this.liveSlot,
        this.make("h3", { text: "The last trial" }),
        this.lastTrialSlot,
      ]),
    );
  }

  async start() {
    await this.attempt(async () => {
      const listing = await this.api.listStoredGraphs();
      this.storedGraphs = listing.graphs.filter((graph) => graph.readable !== false);
      this.paintChooser();
    });
    // Both rig slots get their placeholder now rather than when the first frame
    // arrives, so the panel does not open as two empty headings.
    this.paintLiveState();
    this.paintLastTrial();
    this.paintManualControls();
    this.paintStandaloneControls();
    this.openStateStream();
    // The result is polled rather than streamed: it changes once per trial, and
    // a second socket per panel is a cost the rig pays for nothing.
    this.pollEvery(1, async () => {
      try {
        this.lastResult = await this.api.readLastTrialResult();
      } catch (error) {
        if (error.status !== 404) throw error;
        this.lastResult = null;
      }
      this.paintLastTrial();
    });
    // Separate from the result poll because it repaints a <select> and a number
    // field: the same discipline the chooser has, for the same reason. Two
    // seconds, because what it shows changes when a person or triald acts and
    // not several times a second.
    this.pollEvery(2, async () => {
      this.session = await this.api.readSession();
      this.paintManualControls();
      try {
        this.autorun = await this.api.readAutorun();
      } catch (error) {
        // No board, or one that does not know the command. Neither is a
        // failure worth a red box across the panel: the section says so itself.
        this.autorun = null;
      }
      this.paintStandaloneControls();
    });
  }

  openStateStream() {
    const socket = this.trackSocket(this.api.openStateStream());
    socket.addEventListener("message", (event) => {
      this.state = JSON.parse(event.data);
      this.paintLiveState();
    });
    socket.addEventListener("close", () => {
      // Reopen unless this panel is going away. A stream that dies quietly when
      // the daemon restarts leaves a page that looks live and is not.
      if (this.isConnected && this.openSockets.includes(socket)) {
        this.openSockets = this.openSockets.filter((each) => each !== socket);
        setTimeout(() => {
          if (this.isConnected) this.openStateStream();
        }, 1000);
      }
    });
  }

  /// The person's half. Repainted when the person or the store changes it, and
  /// never by a poll -- it holds the checkboxes.
  paintChooser() {
    this.repaintPreservingFocus(() =>
      this.chooserSlot.replaceChildren(
        this.make("h3", { text: "The set this session will use" }),
        this.graphChooser(),
        this.uploadNote
          ? this.make("p", { class: "good", text: this.uploadNote })
          : this.make("div"),
      ),
    );
  }

  /// Arming one trial without triald. See the note at the top: on a rig this
  /// loop is driven, and what a person needs these for is watching a valve open.
  ///
  /// The graph is chosen once and kept -- `PUT /api/session/active-graph` -- so
  /// that pressing "arm and start" is one act rather than two. It is a
  /// **default and not a mode**: triald names a graph on every trial and that
  /// always wins, so a person switching graphs here cannot change what a driven
  /// rig is running.
  paintManualControls() {
    this.repaintPreservingFocus(() => this.manualSlot.replaceChildren(this.manualControls()));
  }

  manualControls() {
    const session = this.session;
    if (session === null) return this.make("p", { class: "muted", text: "reading the session..." });
    const committed = session.committed_set;
    if (committed === null) {
      return this.make("p", {
        class: "muted",
        text:
          "The board is holding no graphs, so there is nothing to arm. Upload a set above, or " +
          "load a config and open a session.",
      });
    }
    const chooser = this.make(
      "select",
      {
        onChange: (event) => this.chooseActiveGraph(event.target.value),
      },
      [
        this.make("option", { value: "", text: "(choose a graph)" }),
        ...committed.graph_names.map((name) =>
          this.make("option", { value: name, text: name, selected: name === session.active_graph }),
        ),
      ],
    );
    const trialField = this.make("input", {
      type: "number",
      min: "0",
      value: String(this.nextTrialId),
      style: "width:6rem",
      onInput: (event) => {
        this.nextTrialId = Number(event.target.value);
      },
    });
    const armed = this.state !== null && this.state.running;
    return this.make("div", {}, [
      this.make("div", { class: "row" }, [
        this.make("span", { class: "muted", text: "graph" }),
        chooser,
        this.make("span", { class: "muted", text: "trial" }),
        trialField,
        this.make("button", {
          class: "primary",
          text: "arm and start",
          disabled: !session.active_graph,
          onClick: () => this.armAndStartOneTrial(),
        }),
        this.make("button", {
          text: "cancel",
          disabled: !armed,
          onClick: () => this.cancelTheTrial(),
        }),
      ]),
      this.make("p", {
        class: "muted",
        text:
          "The chosen graph is remembered by the daemon as the active one, so a trial that " +
          "names none gets it. triald names a graph on every trial and is unaffected by what " +
          "is chosen here.",
      }),
    ]);
  }

  async chooseActiveGraph(name) {
    await this.attempt(() =>
      name ? this.api.selectActiveGraph(name) : this.api.clearActiveGraph(),
    );
    this.session = await this.api.readSession();
    this.paintManualControls();
  }

  async armAndStartOneTrial() {
    // Armed and started as two calls because they are two calls on the wire:
    // `configure` is the slow one and `start` is the one whose timestamp
    // matters, and collapsing them here would hide which of the two refused.
    const trialId = this.nextTrialId;
    const armed = await this.attempt(() => this.api.configureTrial({ trial_id: trialId }));
    if (armed === null) return;
    const started = await this.attempt(() => this.api.startTrial(trialId));
    if (started === null) return;
    this.nextTrialId = trialId + 1;
    this.paintManualControls();
  }

  async cancelTheTrial() {
    const trialId = this.state && this.state.trial_id != null ? this.state.trial_id : this.nextTrialId;
    await this.attempt(() => this.api.cancelTrial(trialId));
  }

  /// The board on its own: dev/PROTOCOL.md 3.7 and 3.8, as two controls.
  ///
  /// Deliberately below the manual controls and deliberately wordy. Everything
  /// else on this page is a rig this daemon is driving; this is the switch that
  /// makes the daemon optional, and somebody turning it on should know that the
  /// board will keep going when they close the laptop -- and that saving is
  /// what makes it survive the power cut as well.
  paintStandaloneControls() {
    this.repaintPreservingFocus(() =>
      this.standaloneSlot.replaceChildren(this.standaloneControls()),
    );
  }

  standaloneControls() {
    const autorun = this.autorun;
    if (autorun === null) {
      return this.make("p", {
        class: "muted",
        text: "No board is attached, so there is nothing to hand the job to.",
      });
    }
    const active = Boolean(autorun.active);
    const session = this.session;
    const committed = session && session.committed_set;
    return this.make("div", {}, [
      this.make("p", {
        class: "muted",
        text:
          "The board can arm its own trials, taking the interval between them from the dwell " +
          "each terminal state declares. It keeps going when this daemon disconnects, which " +
          "is the point -- so a rig can run a shaping session with nothing plugged into it. " +
          "A terminal state that declares no dwell is where it stops.",
      }),
      this.make("div", { class: "row" }, [
        this.make("button", {
          class: active ? "" : "primary",
          text: active ? "take the rig back" : "let the board run itself",
          disabled: !committed || (!active && !(session && session.active_graph)),
          onClick: () => this.setAutorun(!active),
        }),
        this.make("span", {
          class: active ? "good" : "muted",
          text: active
            ? `running ${autorun.graph_index != null ? `graph ${autorun.graph_index}` : ""} on its own; next trial ${autorun.next_trial_id}`
            : autorun.enabled
              ? "stored as on, but this daemon has the rig: greeting a board takes it back"
              : "off; this daemon arms every trial",
        }),
      ]),
      this.make("div", { class: "row" }, [
        this.make("button", {
          text: "save to the board",
          onClick: () => this.saveSettingsToTheBoard(),
        }),
        this.make("span", {
          class: "muted",
          text:
            "Writes the wiring, the graph set and this setting to the board's own storage, so " +
            "it comes back from a power cut still doing it. Refused while a trial is running.",
        }),
      ]),
      this.saveNote ? this.make("p", { class: "good", text: this.saveNote }) : this.make("div"),
    ]);
  }

  async setAutorun(enabled) {
    const reply = await this.attempt(() =>
      this.api.setAutorun({
        enabled,
        graph_name: enabled && this.session ? this.session.active_graph : null,
      }),
    );
    if (reply === null) return;
    this.autorun = reply;
    this.paintStandaloneControls();
  }

  async saveSettingsToTheBoard() {
    this.saveNote = "";
    const saved = await this.attempt(() => this.api.saveDeviceSettings());
    if (saved === null) return;
    // The write count is flash wear made visible: about 100,000 erase cycles is
    // the budget on the reference board, and a rig should be able to say where
    // it is in it rather than failing one day.
    this.saveNote =
      `saved -- set v${saved.set_version}, autorun ${saved.autorun ? "on" : "off"}, ` +
      `write ${saved.write_count} of this board's storage`;
    this.paintStandaloneControls();
  }

  /// The rig's half. Several times a second, and nothing here is editable.
  paintLiveState() {
    this.liveSlot.replaceChildren(this.liveState());
  }

  paintLastTrial() {
    this.lastTrialSlot.replaceChildren(this.lastTrial());
  }

  graphChooser() {
    const boxes = this.storedGraphs.map((graph) =>
      this.make("label", { class: "row", style: "gap:0.25rem" }, [
        this.make("input", {
          type: "checkbox",
          checked: this.chosenGraphNames.has(graph.name),
          onChange: (event) => {
            if (event.target.checked) this.chosenGraphNames.add(graph.name);
            else this.chosenGraphNames.delete(graph.name);
            this.paintChooser();
          },
        }),
        this.make("span", { text: graph.name }),
        this.make("span", { class: "muted", text: `${graph.state_count} states` }),
      ]),
    );
    return this.make("div", {}, [
      this.make("div", { class: "row" }, boxes),
      this.make("div", { class: "row" }, [
        this.make("button", {
          class: "primary",
          text: `upload ${this.chosenGraphNames.size} graph(s) as the session's set`,
          disabled: this.chosenGraphNames.size === 0,
          onClick: () => this.uploadTheSet(),
        }),
        this.make("span", {
          class: "muted",
          text:
            "The slowest call there is, and the only place a graph is uploaded during a " +
            "session. A failed upload leaves the board holding nothing.",
        }),
      ]),
    ]);
  }

  async uploadTheSet() {
    this.uploadNote = "";
    const uploaded = await this.attempt(() =>
      this.api.uploadSessionGraphSet([...this.chosenGraphNames]),
    );
    if (uploaded === null) return;
    const slots = Object.entries(uploaded.slots)
      .map(([name, slot]) => `${slot}:${name}`)
      .join("  ");
    this.uploadNote =
      `set v${uploaded.set_version} committed in ${uploaded.elapsed_milliseconds} ms  --  ${slots}`;
    this.paintChooser();
  }

  liveState() {
    const state = this.state;
    if (state === null) return this.make("p", { class: "muted", text: "waiting for a frame..." });
    return this.fieldList([
      [
        "device",
        this.make("span", {
          class: state.connected ? "pill good" : "pill bad",
          text: state.connected ? "connected" : "no device",
        }),
      ],
      ["trial", state.trial_id ?? "-"],
      [
        "running",
        state.running
          ? this.make("span", { class: "pill good", text: "running" })
          : this.make("span", { class: "pill", text: "idle" }),
      ],
      ["graph", state.graph ?? "-"],
      ["active graph", (this.session && this.session.active_graph) ?? "-"],
      // By name where the daemon can say it: an index means nothing to
      // somebody watching a rig.
      ["state", state.state_name ?? (state.state_index ?? "-")],
      ["in", this.levelDots(state.input_word || 0, 8)],
      ["out", this.levelDots(state.output_word || 0, 8)],
    ]);
  }

  lastTrial() {
    const result = this.lastResult;
    if (result === null) {
      return this.make("p", { class: "muted", text: "no trial has completed on this connection" });
    }
    const rows = (result.visits || []).map((visit, position) =>
      this.make("tr", {}, [
        this.make("td", { class: "muted", text: `${position}` }),
        this.make("td", { text: visit.state_name }),
        this.make("td", { text: visit.exit_cause ?? "-" }),
        this.make("td", { text: visit.fired_transition_target_state_name ?? "-" }),
        this.make("td", { text: visit.drawn_duration_ms == null ? "-" : `${visit.drawn_duration_ms} ms` }),
        this.make("td", {
          text: formatMeasuredMicroseconds(visit.measured_duration_microseconds),
        }),
      ]),
    );
    return this.make("div", {}, [
      this.fieldList([
        ["trial", result.trial_id],
        [
          "outcome",
          this.make("span", {
            class: result.outcome === "HIT" ? "pill good" : "pill",
            text: `${result.outcome}`,
          }),
        ],
        ["cancelled because", result.cancel_reason],
        ["total", formatDeviceMicroseconds(result.total_duration_microseconds)],
        [
          "path",
          result.path_was_truncated
            ? this.make("span", {
                class: "warn",
                title:
                  "The device's path ring holds a bounded number of visits; this trial " +
                  "visited more. The oldest are gone and the count is still exact.",
                text: `${result.total_visit_count} visits, truncated`,
              })
            : `${result.total_visit_count} visits`,
        ],
      ]),
      this.make("table", {}, [
        this.make("thead", {}, [
          this.make("tr", {}, [
            this.make("th", { text: "#" }),
            this.make("th", { text: "state" }),
            this.make("th", { text: "left by" }),
            this.make("th", { text: "to" }),
            this.make("th", { text: "drawn" }),
            this.make("th", { text: "measured" }),
          ]),
        ]),
        this.make("tbody", {}, rows),
      ]),
      this.make("p", {
        class: "muted",
        text:
          "Named against the graph that actually ran, not against whatever the store holds " +
          "today -- which is what keeps a renamed state from mislabelling last week's data.",
      }),
    ]);
  }
}

/// Microseconds as a person reads a trial: milliseconds, to the microsecond.
///
/// Not rounded to whole milliseconds. The device measures in microseconds and
/// this is the number the record keeps; a UI that rounded it would be the only
/// place in the system that quietly lost the precision everything else is
/// built to preserve.
export function formatMeasuredMicroseconds(microseconds) {
  if (typeof microseconds !== "number") return "-";
  return `${(microseconds / 1000).toFixed(3)} ms`;
}

defineElementOnce("statemachined-session", SessionPanelElement);
