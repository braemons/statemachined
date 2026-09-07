// SPDX-License-Identifier: LGPL-3.0-or-later
//
// `<statemachined-monitor>` -- the wire itself, both directions, as it goes.
//
// The panel for the moment the layers stop agreeing. The Lines view says the
// valve is line 3; the valve is not opening; what actually went down the wire?
// Nothing here interprets anything: these are the lines the daemon wrote and
// the lines the board answered, in order, with the time they crossed.
//
// It reads what is already in the daemon's ring and then follows the stream, so
// opening it shows the greeting that happened before anybody clicked -- a
// monitor that started at "now" would miss every fault that had already
// happened, which is most of them.
//
// **Not the Trace.** The trace is the record: one row per state visit, kept in
// two clocks, written to disk and joined to triald's .tdr afterwards. This is a
// log: bytes, thrown away when the ring wraps, and interesting for about as
// long as somebody is watching it. See dev/DAEMON.md §5.
//
// It sends nothing. A serial terminal that could type at the board would be a
// second host on a link whose whole design is one command in flight (see
// dev/PROTOCOL.md §1.2), and the interesting commands all have API routes that
// keep the daemon's idea of the device true. Every one of them shows up here.

import { BasePanelElement, defineElementOnce } from "./base_panel_element.js";

const BACKFILL_LINES = 400;
//: What the browser holds. The daemon's ring is larger; this is what a table
//: can be scrolled through without the tab becoming the slow part of the rig.
const MOST_LINES_ON_SCREEN = 2000;

export class SerialMonitorPanelElement extends BasePanelElement {
  constructor() {
    super();
    this.lines = [];
    this.lostRange = null;
    this.isFollowing = true;
    this.textFilter = "";
    this.showsHeartbeats = false;
  }

  renderShell() {
    this.failureSlot = this.make("div", { class: "failure-slot" });
    this.rows = this.make("tbody");
    this.status = this.make("span", { class: "muted" });
    this.lostSlot = this.make("div");
    this.scroller = this.make("div", { class: "scroller", style: "max-height:60vh" }, [
      this.make("table", {}, [
        this.make("thead", {}, [
          this.make("tr", {}, [
            this.make("th", { text: "#" }),
            this.make("th", { text: "host time" }),
            this.make("th", { text: "" }),
            this.make("th", { text: "line" }),
          ]),
        ]),
        this.rows,
      ]),
    ]);

    // Closed until somebody opens it, and **not connected while closed**. This
    // is a debugging view: the question it answers is what crossed the wire,
    // which nobody asks until something has already gone wrong. Left open by
    // default it is the loudest thing on the page for the people who need it
    // least -- and, worse, a WebSocket per open tab against a daemon whose
    // whole reason for existing is one serial port. So the stream starts on the
    // first open and stops on close; the daemon's ring keeps the history
    // meanwhile, which is what makes closing it free rather than a decision to
    // stop watching.
    this.details = this.make("details", {}, [
      this.make("summary", {}, [
        this.make("span", { text: "Serial monitor" }),
        this.status,
        this.make("span", {
          class: "muted",
          text: "  every line in and out of the port. For debugging; open it when something " +
            "does not add up.",
        }),
      ]),
      this.make("div", {}, [
        this.make("p", {
          class: "muted",
          text:
            "The daemon's commands and the board's answers, in order, including the ones " +
            "nothing understood. This is the wire, not the record: it is a few thousand lines " +
            "deep and then the oldest go. For what a trial did, keep, and join to a .tdr " +
            "afterwards, use the trace above.",
        }),
        this.make("div", { class: "row" }, [
          this.make("input", {
            type: "text",
            placeholder: "filter, e.g. wiring",
            style: "width:9rem",
            onInput: (event) => {
              this.textFilter = event.target.value.trim().toLowerCase();
              this.repaintRows();
            },
          }),
          this.make("label", { class: "row", style: "gap:0.25rem" }, [
            this.make("input", {
              type: "checkbox",
              checked: this.showsHeartbeats,
              onChange: (event) => {
                this.showsHeartbeats = event.target.checked;
                this.repaintRows();
              },
            }),
            this.make("span", {
              text: "heartbeats",
              title:
                "The daemon pings twice a second to arm the board's link-loss watchdog, and " +
                "takes a clock reading off the answer. Hidden by default because they would " +
                "otherwise be most of this table.",
            }),
          ]),
          this.make("label", { class: "row", style: "gap:0.25rem" }, [
            this.make("input", {
              type: "checkbox",
              checked: true,
              onChange: (event) => {
                this.isFollowing = event.target.checked;
                if (this.isFollowing) this.scrollToTheEnd();
              },
            }),
            this.make("span", { text: "follow" }),
          ]),
        ]),
        this.failureSlot,
        this.lostSlot,
        this.scroller,
      ]),
    ]);
    this.details.open = false;
    this.details.addEventListener("toggle", () => this.theDisclosureWasToggled());

    this.root.replaceChildren(
      this.make("section", {}, [
        this.make("h2", {}, [this.make("span", { text: "Serial monitor" })]),
        this.details,
      ]),
    );
  }

  start() {
    // Nothing until it is opened. `connectedCallback` calls this, so a panel
    // that backfilled here would pay for a view nobody has looked at.
    if (this.details.open) return this.beginWatching();
    return undefined;
  }

  /// Returns the promise rather than dropping it: the backfill is async, and a
  /// caller that cannot wait for it cannot tell "not connected yet" from "not
  /// connecting at all" -- which is the one property this disclosure has to have.
  theDisclosureWasToggled() {
    if (!this.details.open) return this.stopWatching();
    return this.beginWatching();
  }

  /// The ring first, then the stream from where it ended.
  ///
  /// The backfill is what makes opening this late still useful: the greeting,
  /// and the fault that prompted somebody to open it, both happened before the
  /// click. A monitor that started at "now" would miss every fault that had
  /// already happened, which is most of them.
  async beginWatching() {
    if (this.openSockets.length > 0) return;
    await this.attempt(async () => {
      const backfill = await this.api.readDeviceMonitor(0, BACKFILL_LINES);
      this.lines = backfill.lines;
      this.ringCapacity = backfill.ring_capacity;
      this.repaintRows();
    });
    // Checked again: the disclosure may have been closed while the backfill was
    // in flight, and opening a socket then would leave one running behind a
    // closed panel -- exactly what this is meant to avoid.
    if (this.details.open) this.openStream();
  }

  stopWatching() {
    for (const socket of this.openSockets) {
      try {
        socket.close();
      } catch {
        /* already closing */
      }
    }
    this.openSockets = [];
  }

  openStream() {
    const socket = this.trackSocket(this.api.openDeviceMonitorStream());
    socket.addEventListener("message", (event) => {
      const message = JSON.parse(event.data);
      if (message.error === "fell_out_of_the_ring") {
        // Said where the rows are, because a gap nobody is told about is the
        // one failure a monitor must not have.
        this.lostRange = message;
        this.repaintRows();
        return;
      }
      this.lines.push(message);
      if (this.lines.length > MOST_LINES_ON_SCREEN) {
        this.lines = this.lines.slice(-MOST_LINES_ON_SCREEN);
      }
      this.repaintRows();
    });
    socket.addEventListener("close", () => {
      if (this.isConnected && this.details.open && this.openSockets.includes(socket)) {
        this.openSockets = this.openSockets.filter((each) => each !== socket);
        setTimeout(() => {
          if (this.isConnected) this.openStream();
        }, 1000);
      }
    });
  }

  shown() {
    return this.lines.filter((line) => {
      if (!this.showsHeartbeats && isHeartbeat(line.line)) return false;
      if (this.textFilter && !line.line.toLowerCase().includes(this.textFilter)) return false;
      return true;
    });
  }

  repaintRows() {
    const shown = this.shown();
    this.rows.replaceChildren(...shown.map((line) => this.rowFor(line)));
    this.status.textContent =
      ` ${shown.length} of ${this.lines.length} lines` +
      (this.ringCapacity ? `, the daemon keeps ${this.ringCapacity}` : "");
    this.lostSlot.replaceChildren(
      this.lostRange === null
        ? this.make("div")
        : this.make("div", {
            class: "failure",
            text:
              `lines ${this.lostRange.lost_from_entry_number} to ` +
              `${this.lostRange.lost_to_entry_number} went past while this page was not ` +
              `keeping up, and the daemon's ring no longer holds them.`,
          }),
    );
    if (this.isFollowing) this.scrollToTheEnd();
  }

  rowFor(line) {
    const toDevice = line.direction === "to_device";
    return this.make("tr", {}, [
      this.make("td", { class: "muted mono", text: `${line.entry_number}` }),
      this.make("td", { class: "muted mono", text: timeOnly(line.recorded_host_time) }),
      this.make("td", {
        class: toDevice ? "mono" : "mono muted",
        // Which way it went, in one character a person can scan a column of.
        text: toDevice ? "-->" : "<--",
        title: toDevice ? "the daemon said this" : "the board said this",
      }),
      this.make("td", { class: "mono", style: "white-space:pre-wrap", text: line.line }),
    ]);
  }

  scrollToTheEnd() {
    if (this.scroller) this.scroller.scrollTop = this.scroller.scrollHeight;
  }
}

/// The heartbeat and its answer: two lines a second that are not what anybody
/// opened this panel to read.
export function isHeartbeat(line) {
  return line.includes('"msg_type":"ping"') || line.includes('"msg_type":"pong"');
}

/// `10:55:02.186` -- the date is on every row and is never the question.
export function timeOnly(isoTimestamp) {
  const time = `${isoTimestamp}`.split("T")[1] || `${isoTimestamp}`;
  return time.replace("Z", "").slice(0, 12);
}

defineElementOnce("statemachined-monitor", SerialMonitorPanelElement);
