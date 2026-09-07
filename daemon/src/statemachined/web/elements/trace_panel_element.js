// SPDX-License-Identifier: LGPL-3.0-or-later
//
// `<statemachined-trace>` -- every state the machine entered, as it happens.
//
// The one view that is useful with nobody in the room, because it is still
// there in the morning.
//
// Two rules are visible in this panel and both are dev/API.md §7's:
//
// **The stream is not coalesced.** A trace that dropped frames to keep up would
// be a trace of the frames the browser felt like drawing. So the socket sends
// every entry, and when a client is slow enough to fall out of the daemon's
// ring it is *told*, with the range that is gone, rather than handed a shorter
// answer that looks complete. This panel shows that message where the rows
// would be, because a gap you are not told about is the failure the whole
// design is arranged around.
//
// **The cursor is `entry_number`, not the device's `seq`.** `seq` restarts at
// zero every trial, so it cannot address a position in a session-long log.
// Both are shown: `seq` is what a gap within a trial is *detected* with.

import { BasePanelElement, defineElementOnce } from "./base_panel_element.js";

const BACKFILL_ENTRIES = 200;

export class TracePanelElement extends BasePanelElement {
  static observedAttributes = ["base", "trial"];

  constructor() {
    super();
    this.entries = [];
    this.lostRange = null;
    this.isFollowing = true;
    this.trialFilter = "";
  }

  renderShell() {
    this.failureSlot = this.make("div", { class: "failure-slot" });
    this.rows = this.make("tbody");
    this.status = this.make("span", { class: "muted" });
    this.filterField = this.make("input", {
      type: "text",
      value: this.getAttribute("trial") || "",
      placeholder: "trial id",
      style: "width:6rem",
      onChange: (event) => {
        this.trialFilter = event.target.value.trim();
        this.repaintRows();
      },
    });
    this.root.replaceChildren(
      this.make("section", {}, [
        this.make("h2", {}, [
          this.make("span", { text: "Trace" }),
          this.status,
          this.make("span", { class: "spacer" }),
          this.filterField,
          this.make("label", { class: "row", style: "gap:0.25rem" }, [
            this.make("input", {
              type: "checkbox",
              checked: true,
              onChange: (event) => {
                this.isFollowing = event.target.checked;
              },
            }),
            this.make("span", { class: "muted", text: "follow" }),
          ]),
          this.make("button", {
            text: "clear view",
            title: "Clears what this page is showing. The daemon's ring and its NDJSON tail are untouched.",
            onClick: () => {
              this.entries = [];
              this.repaintRows();
            },
          }),
        ]),
        this.make("p", {
          class: "muted",
          text:
            "The trace is this daemon's own record of what the machine did: one row every " +
            "time it entered a state, with the device's clock, an estimate in host time, and " +
            "the trial it belonged to. It is finer grained than the trial record triald " +
            "keeps -- one row per state rather than one per trial -- and the two join on the " +
            "trial id. It is written to disk as it arrives, so it is still there in the " +
            "morning. For the bytes on the wire rather than the states, use the serial " +
            "monitor.",
        }),
        this.failureSlot,
        this.make("div", { class: "scroller" }, [
          this.make("table", {}, [
            this.make("thead", {}, [
              this.make("tr", {}, [
                this.make("th", { text: "#", title: "the daemon's entry_number -- what a cursor is" }),
                this.make("th", { text: "kind" }),
                this.make("th", { text: "trial" }),
                this.make("th", { text: "seq", title: "the device's own count, which restarts every trial" }),
                this.make("th", { text: "state" }),
                this.make("th", { text: "left by" }),
                this.make("th", { text: "device µs" }),
                this.make("th", { text: "host time (estimated)" }),
              ]),
            ]),
            this.rows,
          ]),
        ]),
      ]),
    );
    this.trialFilter = this.getAttribute("trial") || "";
  }

  async start() {
    await this.attempt(async () => {
      // Backfill first, so a page opened mid-session is not blank until the
      // next state visit -- which on a long foreperiod is a very long time.
      const held = await this.api.readTrace(0, BACKFILL_ENTRIES);
      this.entries = held.entries || [];
      this.repaintRows();
    });
    this.openTraceStream();
  }

  openTraceStream() {
    const socket = this.trackSocket(this.api.openTraceStream());
    socket.addEventListener("message", (event) => {
      const message = JSON.parse(event.data);
      if (message.error === "fell_out_of_the_ring") {
        this.lostRange = message;
        this.repaintRows();
        return;
      }
      this.entries.push(message);
      // Bounded here as well as in the daemon: a browser tab left open for a
      // week must not be the thing that runs a rig out of memory.
      if (this.entries.length > 5000) this.entries = this.entries.slice(-5000);
      this.repaintRows();
    });
    socket.addEventListener("close", () => {
      if (this.isConnected && this.openSockets.includes(socket)) {
        this.openSockets = this.openSockets.filter((each) => each !== socket);
        setTimeout(() => {
          if (this.isConnected) this.openTraceStream();
        }, 1000);
      }
    });
  }

  visibleEntries() {
    if (!this.trialFilter) return this.entries;
    return this.entries.filter((entry) => `${entry.trial_id}` === this.trialFilter);
  }

  repaintRows() {
    const visible = this.visibleEntries();
    this.status.textContent = `${visible.length} entries`;
    const rows = visible.map((entry) =>
      this.make("tr", {}, [
        this.make("td", { class: "muted", text: `${entry.entry_number}` }),
        this.make("td", {}, [
          this.make("span", {
            class: entry.kind === "visit" ? "pill" : "pill warn",
            text: `${entry.kind}`,
          }),
        ]),
        this.make("td", { text: entry.trial_id == null ? "-" : `${entry.trial_id}` }),
        this.make("td", {
          class: "muted",
          text: entry.device_sequence_number == null ? "-" : `${entry.device_sequence_number}`,
        }),
        this.make("td", { text: summarise(entry) }),
        this.make("td", { text: entry.exit_cause ?? "-" }),
        this.make("td", {
          class: "mono",
          text:
            entry.entered_device_microseconds == null
              ? "-"
              : `${entry.entered_device_microseconds}`,
          title:
            entry.unwrapped_device_microseconds == null
              ? ""
              : `unwrapped across the ~71-minute wrap: ${entry.unwrapped_device_microseconds}`,
        }),
        this.make("td", {}, [this.hostTimeCell(entry)]),
      ]),
    );
    if (this.lostRange !== null) {
      rows.push(
        this.make("tr", {}, [
          this.make("td", { colSpan: "8" }, [
            this.make("span", {
              class: "bad",
              text:
                `entries ${this.lostRange.lost_from_entry_number} to ` +
                `${this.lostRange.lost_to_entry_number} are gone: this page fell out of the ` +
                `daemon's ring. They are still in the NDJSON tail on the rig.`,
            }),
          ]),
        ]),
      );
    }
    this.rows.replaceChildren(...rows);
    if (this.isFollowing) {
      const scroller = this.root.querySelector(".scroller");
      if (scroller) scroller.scrollTop = scroller.scrollHeight;
    }
  }

  /// An estimate, shown as one. Null until a `ping` has been answered, and
  /// carrying its uncertainty -- a made-up offset would be indistinguishable in
  /// the record from a measured one, so it is not made up here either.
  hostTimeCell(entry) {
    if (!entry.entered_host_time) {
      return this.make("span", {
        class: "muted",
        title: "no clock correlation yet -- it appears after the first answered ping",
        text: "not yet estimated",
      });
    }
    const uncertainty = entry.host_time_uncertainty_microseconds;
    return this.make("span", {
      class: "mono",
      title:
        uncertainty == null
          ? ""
          : `±${uncertainty} µs, which is half the round trip this was estimated from`,
      text:
        `${entry.entered_host_time}` +
        (uncertainty == null ? "" : ` ±${(uncertainty / 1000).toFixed(1)} ms`),
    });
  }
}

/// One column for entries of every kind.
///
/// A `visit` has a state name; the daemon's own entries -- an arm, a start, a
/// link loss, an upload -- do not, and dropping them from the view would undo
/// the reason they are in the same ring. So each says the one thing it is
/// about, in the column a reader is already scanning.
export function summarise(entry) {
  if (entry.state_name) return entry.state_name;
  if (entry.kind === "trial_result") return `${entry.outcome} (${entry.total_visit_count} visits)`;
  if (entry.kind === "graph_set_uploaded") {
    return `set v${entry.set_version}: ${(entry.graph_names || []).join(", ")}`;
  }
  if (entry.kind === "link_connected") return `${entry.board ?? ""} ${entry.firmware_version ?? ""}`.trim();
  if (entry.kind === "sequence_gap" && entry.missing_from_sequence_number != null) {
    return `missing seq ${entry.missing_from_sequence_number}-${entry.missing_to_sequence_number}`;
  }
  return entry.detail ?? entry.graph ?? "-";
}

defineElementOnce("statemachined-trace", TracePanelElement);
