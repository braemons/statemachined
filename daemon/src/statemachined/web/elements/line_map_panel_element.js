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
// The **pin** column is a chooser rather than a text box, wherever the board
// answered `pins` (dev/PROTOCOL.md 3.6). The board owns the labels: which pin
// line 4 is was decided when the firmware was compiled, and typing a different
// string here cannot move a wire. What a person legitimately decides is the
// *assignment* -- "the lever is on D6" -- and that is a choice among this
// board's pins, so it is offered as one. Choosing a pin sets the line number
// with it, because they are the same fact said twice.
//
// A free-text box could only be wrong: the daemon would refuse the save, which
// is the right answer to a question the UI should not have asked. Where the
// board could not answer -- firmware older than the command -- the box comes
// back, because then there is nothing to choose from and a typed label is all
// anybody has.
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
    //: The live dots, paired with the line they belong to rather than keyed by
    //: line number: choosing a different pin changes the number, and a dot
    //: keyed by the old one would go on reporting the old line.
    this.inputDots = [];
    this.outputDots = [];
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
    this.inputDots = [];
    this.outputDots = [];
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
            "drives the pins with, so the pin column offers those and nothing else. Which " +
            "pin a line is cannot be edited here -- it is compiled into the firmware.",
        }),
      ]);
    }
    if (this.pinLabelSource === "assumed") {
      return this.make("p", { class: "muted" }, [
        this.make("span", { class: "pill warn", text: "pins assumed" }),
        this.make("span", {
          text:
            "  This board's firmware is older than the `pins` command, so the pin names come " +
            "from a table in the daemon rather than from the board, and the pin column is a " +
            "text box because there is no authoritative list to choose from. They are a " +
            "belief. Flash current firmware to have them checked.",
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

  /// The pin: a choice among the board's own, or a text box where there is no
  /// list to choose from.
  ///
  /// `lineNumberCell` is the cell showing the line number, updated in place
  /// when a pin is chosen. Rebuilding the row instead would take the focus out
  /// of whatever the person is holding -- the rule at the top of this file --
  /// and the two values are one fact anyway.
  pinField(line, boardPins, lineNumberCell) {
    if (this.pinLabelSource !== "device") {
      return this.textField(line, "pin_label", "5rem");
    }

    const chooser = this.make("select", {
      title: "the pins this board answered with. Which line each one is, is the firmware's",
      onChange: (event) => {
        const pinLabel = event.target.value;
        const lineIndex = boardPins.indexOf(pinLabel);
        this.edit(line, "pin_label", pinLabel);
        // Both, together. A label without its line number is the disagreement
        // the daemon would refuse the save over, and it would be this panel's
        // fault rather than the person's.
        this.edit(line, "line_index", lineIndex < 0 ? null : lineIndex);
        lineNumberCell.textContent = lineIndex < 0 ? "-" : `${lineIndex}`;
      },
    });
    // A line whose pin this board does not have cannot happen through the API
    // -- the daemon refuses it -- but it can be looked at here after a board
    // was swapped for one with fewer pins, and dropping the row would hide it.
    if (!boardPins.includes(line.pin_label)) {
      chooser.append(
        this.make("option", {
          value: line.pin_label || "",
          text: line.pin_label ? `${line.pin_label} (not on this board)` : "(unassigned)",
          selected: true,
        }),
      );
    }
    for (const pinLabel of boardPins) {
      chooser.append(
        this.make("option", {
          value: pinLabel,
          text: pinLabel,
          selected: pinLabel === line.pin_label,
        }),
      );
    }
    return chooser;
  }

  /// The one thing a poll may touch: a class on a dot that is already there.
  ///
  /// The level is looked up by the line number the *draft* row now has, so a
  /// pin chosen but not yet saved lights the dot of the line it was moved to.
  /// That is the point of the panel: "I think the lever is on D7" is a question
  /// somebody answers by pressing the lever and watching, before saving
  /// anything.
  showLiveLevels(live) {
    const byLineIndex = (lines) => new Map(lines.map((l) => [l.line_index, l.is_high_now]));
    const inputs = byLineIndex(live.input_lines);
    const outputs = byLineIndex(live.output_lines);
    for (const { line, dot } of this.inputDots) setLevel(dot, inputs.get(line.line_index));
    for (const { line, dot } of this.outputDots) setLevel(dot, outputs.get(line.line_index));
  }

  /// A dot, remembered with its row so the poll can find it again.
  levelDot(dots, line) {
    const dot = this.make("span", { class: "level" });
    dots.push({ line, dot });
    return dot;
  }

  inputTable() {
    const rows = this.draft.input_lines.map((line) => {
      const lineNumberCell = this.make("td", {
        class: "mono",
        text: line.line_index == null ? "-" : `${line.line_index}`,
      });
      return this.make("tr", {}, [
        this.make("td", {}, [this.levelDot(this.inputDots, line)]),
        this.make("td", {}, [this.textField(line, "name")]),
        lineNumberCell,
        this.make("td", {}, [this.pinField(line, this.boardInputPins, lineNumberCell)]),
        this.make("td", {}, [this.checkBox(line, "reads_active_low")]),
        this.make("td", {}, [this.checkBox(line, "is_enabled")]),
        this.make("td", {}, [this.numberField(line, "debounce_milliseconds")]),
      ]);
    });
    return this.make("table", {}, [
      this.make("thead", {}, [
        this.make("tr", {}, [
          this.make("th", { text: "" }),
          this.make("th", { text: "name" }),
          this.make("th", { text: "line" }),
          this.make("th", {
            text: "pin",
            title: "this board's own pins. Which line each one is, is the firmware's",
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
    const rows = this.draft.output_lines.map((line) => {
      const lineNumberCell = this.make("td", {
        class: "mono",
        text: line.line_index == null ? "-" : `${line.line_index}`,
      });
      return this.make("tr", {}, [
        this.make("td", {}, [this.levelDot(this.outputDots, line)]),
        this.make("td", {}, [this.textField(line, "name")]),
        lineNumberCell,
        this.make("td", {}, [this.pinField(line, this.boardOutputPins, lineNumberCell)]),
        this.make("td", {}, [this.checkBox(line, "safe_level_is_high")]),
      ]);
    });
    return this.make("table", {}, [
      this.make("thead", {}, [
        this.make("tr", {}, [
          this.make("th", { text: "" }),
          this.make("th", { text: "name" }),
          this.make("th", { text: "line" }),
          this.make("th", {
            text: "pin",
            title: "this board's own pins. Which line each one is, is the firmware's",
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
