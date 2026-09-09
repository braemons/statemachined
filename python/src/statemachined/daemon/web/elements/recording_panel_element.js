// SPDX-License-Identifier: LGPL-3.0-or-later
//
// `<statemachined-recording>` -- keeping a named piece of the trace.
//
// **The trace is always running. This panel does not switch it on.** What the
// four buttons here decide is what gets *kept under a name*, in a file that is
// only this run. That distinction is the whole panel: pausing does not blind
// the rig, it opens a gap, and the gap is stated below rather than closed over.
// A recording that hid its own gaps would be the one thing worse than not
// having recorded at all.
//
// It exists for the rig with no triald -- a bench, a pilot, a training box --
// where the daemon is the only thing that saw the session happen. On a rig with
// triald the `.tdr` is the record and this is the finer-grained thing beside
// it, joined on `trial_id`.

import { BasePanelElement, defineElementOnce } from "./base_panel_element.js";

const POLL_SECONDS = 1;

export class RecordingPanelElement extends BasePanelElement {
  constructor() {
    super();
    this.active = null;
    this.recordings = [];
    //: Held here rather than read off the field at start time, so a poll that
    //: repaints cannot take a half-typed name with it.
    this.wantedName = "";
    this.openedName = null;
    this.openedEntries = null;
  }

  renderShell() {
    this.failureSlot = this.make("div", { class: "failure-slot" });
    this.activeSlot = this.make("div", { text: "reading the recorder..." });
    this.listSlot = this.make("div");
    this.entriesSlot = this.make("div");
    this.root.replaceChildren(
      this.make("section", {}, [
        this.make("h2", {}, [this.make("span", { text: "Recording" })]),
        this.make("p", {
          class: "muted",
          text:
            "The trace runs whether or not anybody is recording -- it is a ring in memory and " +
            "a file per day, and it is always on. What these buttons decide is which part of " +
            "it is kept under a name, in a file that is only this run. Pausing does not stop " +
            "the rig or the trace: it opens a gap, and the gap is written down.",
        }),
        this.failureSlot,
        this.activeSlot,
        this.make("h3", { text: "Kept on this rig" }),
        this.listSlot,
        this.entriesSlot,
      ]),
    );
  }

  start() {
    this.pollEvery(POLL_SECONDS, async () => {
      const listed = await this.api.listRecordings();
      this.active = listed.active;
      this.recordings = listed.recordings;
      this.paint();
    });
  }

  paint() {
    this.paintActive();
    this.paintList();
  }

  // --------------------------------------------------------- the active one ---

  paintActive() {
    this.repaintPreservingFocus(() =>
      this.activeSlot.replaceChildren(
        this.active === null ? this.nothingRecording() : this.somethingRecording(),
      ),
    );
  }

  nothingRecording() {
    const nameField = this.make("input", {
      type: "text",
      value: this.wantedName,
      placeholder: "a name, or leave it blank for the time",
      onInput: (event) => {
        this.wantedName = event.target.value;
      },
    });
    return this.make("div", {}, [
      this.make("p", { class: "muted", text: "Nothing is being recorded." }),
      this.make("div", { class: "row" }, [
        nameField,
        this.make("button", {
          class: "primary",
          text: "start recording",
          onClick: () => this.startRecording(nameField.value.trim()),
        }),
      ]),
      this.make("p", {
        class: "muted",
        text:
          "A name is a file name, so it holds letters, digits, dot, dash and underscore. " +
          "Blank names it after the time it started, which still sorts.",
      }),
    ]);
  }

  somethingRecording() {
    const active = this.active;
    const isPaused = active.state === "paused";
    return this.make("div", {}, [
      this.make("p", {}, [
        this.make("span", {
          class: isPaused ? "pill" : "pill good",
          text: isPaused ? "paused" : "recording",
        }),
        this.make("strong", { text: `  ${active.name}` }),
      ]),
      this.fieldList([
        ["entries kept", active.entry_count],
        ["what is in it", describeKinds(active.kind_counts)],
        ["segments", describeSegments(active.segments)],
        ["config", active.state_machine_config ?? "(none loaded)"],
      ]),
      active.write_failure
        ? this.make("p", {
            class: "failure",
            text:
              `The last entry could not be written: ${active.write_failure}. The recording has ` +
              `a hole in it, and it says so rather than pretending otherwise.`,
          })
        : this.make("div"),
      this.make("div", { class: "row" }, [
        isPaused
          ? this.make("button", {
              class: "primary",
              text: "resume",
              onClick: () => this.act(() => this.api.resumeRecording()),
            })
          : this.make("button", {
              text: "pause",
              onClick: () => this.act(() => this.api.pauseRecording()),
            }),
        this.make("button", {
          text: "stop",
          onClick: () => this.act(() => this.api.stopRecording()),
        }),
        this.make("button", {
          text: "clear what is in it",
          title:
            "Throws away what has been captured and keeps recording. Not the same as stop, " +
            "which keeps what it has.",
          onClick: () => this.act(() => this.api.clearRecording()),
        }),
      ]),
      isPaused
        ? this.make("p", {
            class: "muted",
            text:
              "The rig is not paused -- only this recording is. Everything happening now is " +
              "still in the trace, and the gap will be visible in the segments above.",
          })
        : this.make("div"),
    ]);
  }

  // ------------------------------------------------------------- the store ---

  paintList() {
    if (this.recordings.length === 0) {
      this.listSlot.replaceChildren(
        this.make("p", { class: "muted", text: "No recordings are kept on this rig yet." }),
      );
      return;
    }
    this.listSlot.replaceChildren(
      this.make(
        "div",
        { class: "config-list" },
        this.recordings.map((recording) => this.rowFor(recording)),
      ),
    );
  }

  rowFor(recording) {
    if (recording.unreadable !== undefined) {
      return this.make("div", { class: "config-row" }, [
        this.make("span", {}, [this.make("strong", { text: recording.name })]),
        this.make("span", { class: "muted", text: `will not read: ${recording.unreadable}` }),
        this.make("button", {
          text: "delete",
          onClick: () => this.act(() => this.api.deleteRecording(recording.name)),
        }),
      ]);
    }
    const isActive = this.active !== null && this.active.name === recording.name;
    return this.make("div", { class: "config-row" }, [
      this.make("span", {}, [
        this.make("strong", { text: recording.name }),
        this.make("span", { class: "muted", text: isActive ? `  (${this.active.state})` : "" }),
      ]),
      this.make("span", {
        class: "muted",
        text:
          `${recording.entry_count ?? 0} entries` +
          `  ${describeSegments(recording.segments)}` +
          (recording.state_machine_config ? `  ${recording.state_machine_config}` : ""),
      }),
      this.make("button", {
        text: this.openedName === recording.name ? "hide" : "open",
        onClick: () => this.openRecording(recording.name),
      }),
      this.make("button", {
        text: "delete",
        disabled: isActive,
        title: isActive ? "Stop it first: deleting under a running recording is a hole." : "",
        onClick: () => this.act(() => this.api.deleteRecording(recording.name)),
      }),
    ]);
  }

  async openRecording(name) {
    if (this.openedName === name) {
      this.openedName = null;
      this.openedEntries = null;
      this.entriesSlot.replaceChildren();
      return;
    }
    const read = await this.attempt(() => this.api.readRecordingEntries(name, 0, 200));
    if (read === null) return;
    this.openedName = name;
    this.openedEntries = read;
    this.paintEntries();
  }

  paintEntries() {
    const read = this.openedEntries;
    if (read === null) return;
    const rows = read.entries.map((entry) =>
      this.make("tr", {}, [
        this.make("td", { text: String(entry.entry_number) }),
        this.make("td", { text: entry.recorded_host_time }),
        this.make("td", { text: entry.kind }),
        this.make("td", { text: entry.state_name ?? entry.graph ?? entry.recording ?? "" }),
        this.make("td", {
          text:
            entry.measured_duration_microseconds !== undefined &&
            entry.measured_duration_microseconds !== null
              ? `${(entry.measured_duration_microseconds / 1000).toFixed(1)} ms`
              : "",
        }),
      ]),
    );
    this.entriesSlot.replaceChildren(
      this.make("div", {}, [
        this.make("h3", { text: `${read.name}` }),
        this.make("p", {
          class: "muted",
          text:
            `Showing ${read.entries.length} of ${read.entry_count} entries. The entry numbers ` +
            `are the trace's, so they join back to it exactly -- and a jump in them is a pause, ` +
            `not a lost record.`,
        }),
        this.make("table", {}, [
          this.make("thead", {}, [
            this.make("tr", {}, [
              this.make("th", { text: "entry" }),
              this.make("th", { text: "recorded" }),
              this.make("th", { text: "kind" }),
              this.make("th", { text: "what" }),
              this.make("th", { text: "measured" }),
            ]),
          ]),
          this.make("tbody", {}, rows),
        ]),
      ]),
    );
  }

  async startRecording(name) {
    const started = await this.attempt(() => this.api.startRecording(name, ""));
    if (started === null) return;
    this.wantedName = "";
    await this.refresh();
  }

  async act(action) {
    const done = await this.attempt(action);
    if (done !== null) await this.refresh();
  }

  async refresh() {
    const listed = await this.api.listRecordings();
    this.active = listed.active;
    this.recordings = listed.recordings;
    this.paint();
    if (this.openedName !== null) {
      const still = this.recordings.some((each) => each.name === this.openedName);
      if (!still) {
        this.openedName = null;
        this.openedEntries = null;
        this.entriesSlot.replaceChildren();
      }
    }
  }
}

/// "12 visits, 3 trial results" rather than a JSON object: the question this
/// answers is whether the recording caught anything worth keeping.
function describeKinds(kindCounts) {
  const entries = Object.entries(kindCounts || {});
  if (entries.length === 0) return "nothing yet";
  return entries
    .sort((left, right) => right[1] - left[1])
    .map(([kind, count]) => `${count} ${kind}`)
    .join(", ");
}

/// Segments as gaps, because that is what a reader needs from them. One segment
/// is a recording with no pause in it and says so.
export function describeSegments(segments) {
  const kept = (segments || []).filter((each) => each.from_entry_number !== null);
  if (kept.length === 0) return "no entries yet";
  if (kept.length === 1) return "one stretch, no gaps";
  return `${kept.length} stretches, ${kept.length - 1} gap(s) where it was paused`;
}

defineElementOnce("statemachined-recording", RecordingPanelElement);
