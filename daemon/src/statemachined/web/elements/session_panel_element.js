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
  }

  renderShell() {
    this.failureSlot = this.make("div", { class: "failure-slot" });
    // Three slots, filled independently. See the note at the top of this file.
    this.chooserSlot = this.make("div", { text: "reading the store..." });
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
