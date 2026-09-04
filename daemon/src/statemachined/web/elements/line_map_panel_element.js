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

import { BasePanelElement, defineElementOnce } from "./base_panel_element.js";

const LEVEL_POLL_SECONDS = 0.5;

export class LineMapPanelElement extends BasePanelElement {
  constructor() {
    super();
    //: The edited copy. Held apart from what the poll paints, so a level
    //: arriving mid-edit cannot overwrite a half-typed name.
    this.draft = null;
    this.hasUnsavedEdits = false;
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
      onClick: () => {
        this.hasUnsavedEdits = false;
        this.draft = null;
      },
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
      if (this.draft === null) {
        this.draft = {
          input_lines: lines.input_lines.map(withoutLiveLevel),
          output_lines: lines.output_lines.map(withoutLiveLevel),
        };
      }
      this.paint(lines);
    });
  }

  paint(live) {
    const levelByInputIndex = new Map(live.input_lines.map((l) => [l.line_index, l.is_high_now]));
    const levelByOutputIndex = new Map(live.output_lines.map((l) => [l.line_index, l.is_high_now]));

    this.saveButton.disabled = !this.hasUnsavedEdits;
    this.revertButton.disabled = !this.hasUnsavedEdits;

    this.body.replaceChildren(
      this.make("h3", { text: "Inputs" }),
      this.inputTable(levelByInputIndex),
      this.make("h3", { text: "Outputs" }),
      this.outputTable(levelByOutputIndex),
      this.make("p", {
        class: "muted",
        text:
          "Renaming is free: names never reach the wire. Everything else is the wiring, " +
          "is pushed to the device on save, and is refused while a trial is armed.",
      }),
    );
  }

  inputTable(levelByIndex) {
    const rows = this.draft.input_lines.map((line) =>
      this.make("tr", {}, [
        this.make("td", {}, [this.make("span", {
          class: levelByIndex.get(line.line_index) ? "level high" : "level",
        })]),
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

  outputTable(levelByIndex) {
    const rows = this.draft.output_lines.map((line) =>
      this.make("tr", {}, [
        this.make("td", {}, [this.make("span", {
          class: levelByIndex.get(line.line_index) ? "level high" : "level",
        })]),
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
      onChange: (event) => this.edit(line, key, event.target.value),
    });
  }

  numberField(line, key) {
    return this.make("input", {
      type: "number",
      min: "0",
      value: `${line[key] ?? 0}`,
      style: "width:5rem",
      onChange: (event) => this.edit(line, key, Number(event.target.value)),
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
    this.hasUnsavedEdits = false;
    // Re-read rather than trust the draft: the daemon is the authority on what
    // the map now is, and a save that was accepted with something normalised
    // should show the normalised version.
    this.draft = null;
    if (!saved.pushed_to_device) {
      this.showFailure(
        new Error(
          "saved, but no device is connected -- the wiring will be pushed on the next connect",
        ),
      );
    }
  }
}

function withoutLiveLevel(line) {
  const { is_high_now, ...rest } = line;
  return rest;
}

defineElementOnce("statemachined-lines", LineMapPanelElement);
