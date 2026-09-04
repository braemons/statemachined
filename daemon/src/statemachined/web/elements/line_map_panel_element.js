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
// The **pin** column is two things side by side, and the difference is the one
// that used to be invisible: what the config calls this pin, and what the board
// itself answered when asked (dev/PROTOCOL.md 3.6). They agree or the daemon
// refused to connect, so what showing both is worth is that a person can see
// *which* is which -- and where the board could not answer, the panel says the
// labels are assumed rather than quietly showing them as fact.
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
    // Indexed by line number, which is how the board answers: the label of
    // line 4 is board_input_pins[4]. Held for the build below rather than
    // looked up per row, since a poll must not rebuild rows at all.
    this.boardInputPins = live.board_input_pins || [];
    this.boardOutputPins = live.board_output_pins || [];
    this.pinLabelSource = live.pin_labels_came_from || "unknown";

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
      this.pinProvenance(),
    );
  }

  /// Where the "on the board" column came from, said plainly.
  ///
  /// A label the board vouched for can be shown as a fact. One this daemon
  /// assumed -- from its own table, for firmware older than the `pins` command
  /// -- is a belief, and the failure it hides is a valve driven from a lever's
  /// line number. So it is labelled, rather than looking identical.
  pinProvenance() {
    if (this.pinLabelSource === "device") {
      return this.make("p", { class: "muted" }, [
        this.make("span", { class: "pill good", text: "pins from the board" }),
        this.make("span", {
          text:
            "  The board answered which pin each line is, out of the same table its firmware " +
            "drives the pins with. A pin column that disagreed would have stopped the daemon " +
            "connecting.",
        }),
      ]);
    }
    if (this.pinLabelSource === "assumed") {
      return this.make("p", { class: "muted" }, [
        this.make("span", { class: "pill warn", text: "pins assumed" }),
        this.make("span", {
          text:
            "  This board's firmware is older than the `pins` command, so the pin names come " +
            "from a table in the daemon rather than from the board. They are a belief. Flash " +
            "current firmware to have them checked.",
        }),
      ]);
    }
    return this.make("p", { class: "muted" }, [
      this.make("span", { class: "pill", text: "pins unknown" }),
      this.make("span", {
        text:
          "  Neither the board nor this daemon knows what this board's pins are called. Line " +
          "numbers are all there is; the levels beside them are the only check.",
      }),
    ]);
  }

  /// What the board calls this line, or nothing where nobody knows.
  boardPinCell(labels, lineIndex) {
    const label = lineIndex == null ? "" : labels[lineIndex] || "";
    return this.make("td", {
      class: this.pinLabelSource === "device" ? "mono" : "mono muted",
      text: label || "-",
      title:
        this.pinLabelSource === "device"
          ? "what the board answered for this line"
          : "assumed by the daemon; this board did not say",
    });
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
        this.boardPinCell(this.boardInputPins, line.line_index),
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
          this.make("th", { text: "pin", title: "what this config calls it -- editable" }),
          this.make("th", {
            text: "on the board",
            title: "what the board answered when asked (dev/PROTOCOL.md 3.6)",
          }),
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
        this.boardPinCell(this.boardOutputPins, line.line_index),
        this.make("td", {}, [this.checkBox(line, "safe_level_is_high")]),
      ]),
    );
    return this.make("table", {}, [
      this.make("thead", {}, [
        this.make("tr", {}, [
          this.make("th", { text: "" }),
          this.make("th", { text: "name" }),
          this.make("th", { text: "line" }),
          this.make("th", { text: "pin", title: "what this config calls it -- editable" }),
          this.make("th", {
            text: "on the board",
            title: "what the board answered when asked (dev/PROTOCOL.md 3.6)",
          }),
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
