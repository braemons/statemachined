// SPDX-License-Identifier: LGPL-3.0-or-later
//
// `<statemachined-lines>` -- the map, and the wiring behind it.
//
// The two halves of `model/line_map.py` are visibly different here, because the
// difference is the one a person setting up a rig has to hold:
//
//   * a **name** is the daemon's alone, never reaches the wire, and renaming is
//     free -- no graph changes, nothing is uploaded;
//   * the **wiring** (invert, enable, debounce, safe level) is pushed to the
//     device, is read by the scan, and is refused while a trial is armed.
//
// So the table says which edits are which, and the save button says what it
// will do rather than "save".
//
// `is_high_now` keeps updating while you edit, and that is the point of the
// panel: there is no read-back path from a pin, so watching a lamp move when
// somebody presses a lever is the only way to confirm a graph's line numbers
// reach the wire that was actually soldered.
//
// Which is why **the poll paints the dots and nothing else**. The tables are
// built once and rebuilt only when the draft is replaced -- by a revert, by a
// save, or by the first read. A panel that rebuilt its fields on every poll
// would take the focus out of a field twice a second: the element you were
// typing into is removed from the document, and with it the cursor, the
// selection and any keystrokes not yet committed. Holding the draft apart from
// the live levels is not enough on its own; the DOM has to be held apart too.

import { BasePanelElement, defineElementOnce } from "./base_panel_element.js";

const LEVEL_POLL_SECONDS = 0.5;

export class LineMapPanelElement extends BasePanelElement {
  constructor() {
    super();
    //: The edited copy. Held apart from what the poll paints, so a level
    //: arriving mid-edit cannot overwrite a half-typed name.
    this.draft = null;
    this.hasUnsavedEdits = false;
    //: Counted rather than compared. A revert re-reads a map that is usually
    //: identical to what is on screen, so "has the shape changed" would answer
    //: no and leave the reverted fields showing the edits. The identity of the
    //: draft is the question, and a counter is the cheapest way to ask it.
    this.draftGeneration = 0;
    this.builtGeneration = -1;
    //: The live dots, by line index, so a poll can touch them and nothing else.
    this.inputDotByLineIndex = new Map();
    this.outputDotByLineIndex = new Map();
  }

  renderShell() {
    this.failureSlot = this.make("div", { class: "failure-slot" });
    this.body = this.make("div", { text: "reading the line map..." });
    this.saveButton = this.make("button", {
      class: "primary",
      text: "save and push the wiring",
      disabled: true,
      onClick: () => this.save(),
    });
    this.revertButton = this.make("button", {
      text: "revert",
      disabled: true,
      onClick: () => this.discardTheDraft(),
    });
    this.root.replaceChildren(
      this.make("section", {}, [
        this.make("h2", {}, [
          this.make("span", { text: "Lines" }),
          this.make("span", { class: "spacer" }),
          this.revertButton,
          this.saveButton,
        ]),
        this.failureSlot,
        this.body,
      ]),
    );
  }

  start() {
    this.pollEvery(LEVEL_POLL_SECONDS, async () => {
      const lines = await this.api.readDeviceLines();
      if (this.draft === null) this.adoptDraft(lines);
      this.paint(lines);
    });
  }

  /// Take the daemon's map as the thing being edited.
  adoptDraft(lines) {
    this.draft = draftOf(lines);
    this.hasUnsavedEdits = false;
    this.draftGeneration += 1;
  }

  /// Throw the edits away. The next poll adopts the daemon's map again.
  discardTheDraft() {
    this.draft = null;
    this.hasUnsavedEdits = false;
  }

  paint(live) {
    // The tables, only when the thing they are editing is a different object.
    // Everything else a poll knows -- the levels -- is painted below without
    // touching a field.
    if (this.builtGeneration !== this.draftGeneration) {
      this.buildTables();
      this.builtGeneration = this.draftGeneration;
    }
    this.showLiveLevels(live);

    // Disabling the button the pointer is on is fine; disabling the field
    // somebody is typing in is not, and these are buttons.
    this.saveButton.disabled = !this.hasUnsavedEdits;
    this.revertButton.disabled = !this.hasUnsavedEdits;
  }

  buildTables() {
    this.inputDotByLineIndex = new Map();
    this.outputDotByLineIndex = new Map();
    this.body.replaceChildren(
      this.make("h3", { text: "Inputs" }),
      this.inputTable(),
      this.make("h3", { text: "Outputs" }),
      this.outputTable(),
      this.make("p", {
        class: "muted",
        text:
          "Renaming is free: names never reach the wire. Everything else is the wiring, " +
          "is pushed to the device on save, and is refused while a trial is armed.",
      }),
    );
  }

  /// The one thing a poll may touch: a class on a dot that is already there.
  showLiveLevels(live) {
    for (const line of live.input_lines) {
      setLevel(this.inputDotByLineIndex.get(line.line_index), line.is_high_now);
    }
    for (const line of live.output_lines) {
      setLevel(this.outputDotByLineIndex.get(line.line_index), line.is_high_now);
    }
  }

  /// A dot, remembered by line index so the poll can find it again.
  levelDot(dotsByLineIndex, lineIndex) {
    const dot = this.make("span", { class: "level" });
    dotsByLineIndex.set(lineIndex, dot);
    return dot;
  }

  inputTable() {
    const rows = this.draft.input_lines.map((line) =>
      this.make("tr", {}, [
        this.make("td", {}, [this.levelDot(this.inputDotByLineIndex, line.line_index)]),
        this.make("td", {}, [this.textField(line, "name")]),
        this.make("td", { class: "mono", text: `${line.line_index}` }),
        this.make("td", {}, [this.textField(line, "pin_label", "5rem")]),
        this.make("td", {}, [this.checkBox(line, "reads_active_low")]),
        this.make("td", {}, [this.checkBox(line, "is_enabled")]),
        this.make("td", {}, [this.numberField(line, "debounce_milliseconds")]),
      ]),
    );
    return this.make("table", {}, [
      this.make("thead", {}, [
        this.make("tr", {}, [
          this.make("th", { text: "" }),
          this.make("th", { text: "name" }),
          this.make("th", { text: "line" }),
          this.make("th", { text: "pin" }),
          this.make("th", { text: "active low", title: "reads inverted -- opto-isolated inputs routinely do" }),
          this.make("th", { text: "enabled", title: "a disabled line reads zero however the pin is driven" }),
          this.make("th", { text: "debounce ms" }),
        ]),
      ]),
      this.make("tbody", {}, rows),
    ]);
  }

  outputTable() {
    const rows = this.draft.output_lines.map((line) =>
      this.make("tr", {}, [
        this.make("td", {}, [this.levelDot(this.outputDotByLineIndex, line.line_index)]),
        this.make("td", {}, [this.textField(line, "name")]),
        this.make("td", { class: "mono", text: `${line.line_index}` }),
        this.make("td", {}, [this.textField(line, "pin_label", "5rem")]),
        this.make("td", {}, [this.checkBox(line, "safe_level_is_high")]),
      ]),
    );
    return this.make("table", {}, [
      this.make("thead", {}, [
        this.make("tr", {}, [
          this.make("th", { text: "" }),
          this.make("th", { text: "name" }),
          this.make("th", { text: "line" }),
          this.make("th", { text: "pin" }),
          this.make("th", {
            text: "safe level high",
            title:
              "What this line is driven to on reset, link loss or a refused graph. " +
              "Per line, because an active-low valve driver is opened by a low.",
          }),
        ]),
      ]),
      this.make("tbody", {}, rows),
    ]);
  }

  // -------------------------------------------------------------- fields ---

  textField(line, key, width = "8rem") {
    return this.make("input", {
      type: "text",
      value: line[key] ?? "",
      style: `width:${width}`,
      // On `input` rather than `change`: nothing rebuilds this field any more,
      // so there is no re-render to land inside -- and a draft that lagged a
      // blur behind the screen is a save that quietly posts the old name.
      onInput: (event) => this.edit(line, key, event.target.value),
    });
  }

  numberField(line, key) {
    return this.make("input", {
      type: "number",
      min: "0",
      value: `${line[key] ?? 0}`,
      style: "width:5rem",
      onInput: (event) => this.edit(line, key, Number(event.target.value)),
    });
  }

  checkBox(line, key) {
    return this.make("input", {
      type: "checkbox",
      checked: Boolean(line[key]),
      onChange: (event) => this.edit(line, key, event.target.checked),
    });
  }

  edit(line, key, value) {
    line[key] = value;
    this.hasUnsavedEdits = true;
    this.saveButton.disabled = false;
    this.revertButton.disabled = false;
  }

  async save() {
    const saved = await this.attempt(() => this.api.replaceLineMap(this.draft));
    if (saved === null) return;
    // Re-read rather than trust the draft: the daemon is the authority on what
    // the map now is, and a save that was accepted with something normalised
    // should show the normalised version.
    this.discardTheDraft();
    if (!saved.pushed_to_device) {
      this.showFailure(
        new Error(
          "saved, but no device is connected -- the wiring will be pushed on the next connect",
        ),
      );
    }
  }
}

/// The map as something to edit: every line, without the level it happened to
/// be at when it was read.
///
/// `is_high_now` is the device's reading and is not part of the map. The daemon
/// refuses a line map carrying fields the model does not declare, so posting a
/// draft with it still on would be refused -- and rightly: a level is not
/// configuration and a rig cannot be told to be high.
export function draftOf(lines) {
  return {
    input_lines: lines.input_lines.map(withoutLiveLevel),
    output_lines: lines.output_lines.map(withoutLiveLevel),
  };
}

function withoutLiveLevel(line) {
  const { is_high_now, ...rest } = line;
  return rest;
}

function setLevel(dot, isHigh) {
  if (dot === undefined) return;
  dot.className = isHigh ? "level high" : "level";
}

defineElementOnce("statemachined-lines", LineMapPanelElement);
