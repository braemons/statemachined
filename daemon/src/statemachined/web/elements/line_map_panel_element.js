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
// **Adding a line does not create one.** Every line the board has exists
// whether anybody has named it or not -- which pin each one is was compiled
// into the firmware -- so what is added here is a *name* for one, and a new row
// therefore arrives already sitting on the free pin with the lowest line
// number. There is nothing else it could sensibly be on, and a row with no pin
// is a row the daemon refuses. When every pin has a name the button is off, and
// says so: a board has a fixed number of lines and no amount of clicking makes
// a ninth input.
//
// **Removing is that fact backwards**: the line stays, the name stops. The one
// thing that makes it dangerous is a graph that names it, so the panel reads
// the loaded config's graphs once and holds the save if a name they use has
// gone -- with the graph named. Otherwise the discovery happens at the next
// upload, which is the start of a session, with an animal in the booth.
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
// **Two lines may not be the same pin**, and the daemon refuses that too --
// "two names for one line is not a harmless alias: a graph naming both would
// raise one line and believe it had raised two, and the mistake is invisible in
// the record" (model/line_map.py). The chooser does not *prevent* it, though,
// because preventing it makes swapping two pins impossible -- moving the lever
// from D6 to D7 while the pedal is on D7 has to pass through a state where both
// are on D7. So a taken pin is offered, marked with what has it, and the panel
// refuses to save until it is resolved. Loud and reversible beats forbidden.
//
// **Where a save lands.** On the board, immediately, and in the loaded
// state-machine config *in memory* -- not on disk. That is deliberate on both
// halves: a wiring change has to reach the board at once or the live dots
// cannot check it against the wire, and an edit that reached the disk on every
// save would make `revert` a lie and would rewrite a file somebody may be
// running a session from. The Configs panel is where a map becomes permanent,
// under whatever name the person chooses, which is also how a rig's wiring is
// saved as a *new* config rather than over the one it came from.
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
    //: Every pin chooser on screen, so the annotations that say which pins are
    //: taken can be refreshed without rebuilding a row.
    this.pinChoosers = [];
    //: The live dots, paired with the line they belong to rather than keyed by
    //: line number: choosing a different pin changes the number, and a dot
    //: keyed by the old one would go on reporting the old line.
    this.inputDots = [];
    this.outputDots = [];
    //: Which line names the loaded config's graphs actually name, so that
    //: removing one can say what it would break. Read once when the panel
    //: opens and again after a save, not on the poll: a graph set does not
    //: change twice a second, and this panel's rule is that the poll paints
    //: dots and nothing else.
    this.namesUsedByGraphs = { in: new Map(), out: new Map() };
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
        this.make("p", {
          class: "muted",
          text:
            "What each line of this rig is called, which pin it is, and what the box does to " +
            "the signal before the graph sees it. Names are this daemon's alone and never " +
            "reach the board, so renaming is free; everything else is pushed to the board on " +
            "save. The dot beside each line is its level right now -- pressing the thing you " +
            "wired and watching the dot is the only way to confirm a wire from outside. " +
            "Saving here reaches the board and the loaded config in memory, not the disk: " +
            "keep it past a restart with \u201csave what is running\u201d in Configs.",
        }),
        this.failureSlot,
        this.body,
      ]),
    );
  }

  start() {
    this.readWhichLinesTheGraphsName();
    this.pollEvery(LEVEL_POLL_SECONDS, async () => {
      const lines = await this.api.readDeviceLines();
      if (this.draft === null) this.adoptDraft(lines);
      this.paint(lines);
    });
  }

  /// Which line names the loaded config's graphs use, and which graph uses each.
  ///
  /// A line map and the graphs that name it live in one state-machine config,
  /// so this panel can answer the question a delete button otherwise cannot:
  /// *what breaks*. Removing `reward_valve` while go-nogo pulses it does not
  /// fail here -- it fails at the next upload, with an animal in the booth --
  /// so the panel refuses the save and says which graph is the reason.
  ///
  /// Never fatal. A rig with no config loaded has no graphs to check against,
  /// and the panel is still the place somebody wires a board.
  async readWhichLinesTheGraphsName() {
    this.namesUsedByGraphs = { in: new Map(), out: new Map() };
    try {
      const session = await this.api.readSession();
      const loaded = session.state_machine_config;
      if (loaded === null) return;
      const config = await this.api.readStateMachineConfig(loaded.name);
      for (const graph of config.graphs || []) {
        for (const [direction, name] of lineNamesUsedBy(graph)) {
          const used = this.namesUsedByGraphs[direction];
          used.set(name, [...new Set([...(used.get(name) || []), graph.name])]);
        }
      }
    } catch {
      // An older daemon, or a rig with nothing loaded. The map is still
      // editable; only the warning is missing, and a warning that guessed
      // would be worse than none.
    }
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
    this.showPinConflicts();
    this.revertButton.disabled = !this.hasUnsavedEdits;
  }

  buildTables() {
    this.inputDots = [];
    this.outputDots = [];
    this.pinChoosers = [];
    this.conflictSlot = this.make("div");
    this.body.replaceChildren(
      this.conflictSlot,
      this.make("h3", { text: "Inputs" }),
      this.inputTable(),
      this.addControls("in"),
      this.make("h3", { text: "Outputs" }),
      this.outputTable(),
      this.addControls("out"),
      this.make("p", {
        class: "muted",
        text:
          "Renaming is free: names never reach the wire. Everything else is the wiring, " +
          "is pushed to the device on save, and is refused while a trial is armed. " +
          "Adding a line does not create one -- every line the board has exists whether it " +
          "is named or not -- it gives one a name a graph can use.",
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
  pinField(line, boardPins, lineNumberCell, direction) {
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
        // Another row may now be sharing this pin, or may have stopped sharing
        // one. Both are facts about the whole table rather than this row.
        this.showPinConflicts();
      },
    });
    this.pinChoosers.push({ chooser, line, direction, boardPins });
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

  /// Which lines two rows are both claiming, in each direction.
  ///
  /// By line number rather than by label, because the number is what the wire
  /// carries and what the daemon refuses duplicates of. A pin chosen twice is
  /// the same line twice; a *name* used twice is refused for its own reasons
  /// and is checked here too, since both come back as one refusal at save and
  /// a person should see them in the same place.
  draftConflicts() {
    const conflicts = [];
    for (const [direction, lines] of [
      ["in", this.draft.input_lines],
      ["out", this.draft.output_lines],
    ]) {
      const where = direction === "in" ? "input" : "output";
      conflicts.push(
        ...sharedValues(lines, "line_index").map(([lineIndex, names]) => ({
          direction,
          lineIndex,
          detail:
            `${names.join(" and ")} are both ${where} line ${lineIndex}. Two names for one ` +
            `line is not an alias: a graph naming both would raise one line and believe it ` +
            `had raised two.`,
        })),
        ...sharedValues(lines, "name").map(([name, names]) => ({
          direction,
          lineIndex: null,
          detail: `two ${where} lines are called "${name}".`,
        })),
      );

      // A row naming neither a pin nor a line number. Only reachable where the
      // board could not say what its pins are called, since a new row is put
      // on a free pin wherever there is a list of them -- and the daemon
      // refuses it with the same words, one round trip later.
      conflicts.push(
        ...lines
          .filter((line) => line.line_index == null && !line.pin_label)
          .map((line) => ({
            direction,
            lineIndex: null,
            detail:
              `the ${where} line "${line.name}" says neither which pin it is nor which ` +
              `line. Give it a pin -- a bit position is written nowhere on the hardware.`,
          })),
      );

      // A name a graph uses, removed or renamed out from under it. The upload
      // is where this would otherwise be found -- "no output line is called
      // 'reward_valve'" -- and by then a session is starting.
      const stillNamed = new Set(lines.map((line) => line.name));
      for (const [name, graphNames] of this.namesUsedByGraphs[direction]) {
        if (stillNamed.has(name)) continue;
        conflicts.push({
          direction,
          lineIndex: null,
          detail:
            `no ${where} line is called "${name}" any more, and ${graphNames.join(" and ")} ` +
            `name${graphNames.length === 1 ? "s" : ""} it. Put the name back, or edit the ` +
            `graph first -- an upload would be refused at the start of a session.`,
        });
      }
    }
    return conflicts;
  }

  /// Mark the taken pins, say what is wrong, and hold the save button.
  ///
  /// Not a rebuild: the option labels and one banner are updated in place, so
  /// this can run on every change without touching whatever has focus.
  showPinConflicts() {
    const conflicts = this.draftConflicts();
    const takenBy = new Map();
    for (const { line, direction } of this.pinChoosers) {
      if (line.line_index == null) continue;
      const key = `${direction}:${line.line_index}`;
      takenBy.set(key, [...(takenBy.get(key) || []), line.name]);
    }

    for (const { chooser, line, direction, boardPins } of this.pinChoosers) {
      for (const option of [...chooser.children]) {
        // By label rather than by position: an option's pin is what it says,
        // and the row may carry an extra option for a pin this board does not
        // have (which is line -1, and held by nothing).
        const lineIndex = boardPins.indexOf(option.value);
        // The row's own "(unassigned)" or "not on this board" option stands for
        // no line, and its text says so already.
        if (lineIndex < 0) continue;
        const holders = (takenBy.get(`${direction}:${lineIndex}`) || []).filter(
          (name) => name !== line.name,
        );
        // The bare label where nothing else has it, and who has it where
        // something does -- so a pin already spoken for is visible *before* it
        // is chosen rather than after the save is refused.
        option.textContent =
          holders.length === 0 ? option.value : `${option.value} — ${holders.join(", ")}`;
      }
    }

    this.conflictSlot.replaceChildren(
      ...conflicts.map((conflict) => this.make("div", { class: "failure", text: conflict.detail })),
    );
    // The daemon would refuse this map, so the panel does not offer to send it.
    // Disabling a button is safe where disabling a field is not: nobody is
    // typing into a button.
    this.saveButton.disabled = !this.hasUnsavedEdits || conflicts.length > 0;
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
        this.make("td", {}, [this.pinField(line, this.boardInputPins, lineNumberCell, "in")]),
        this.make("td", {}, [this.checkBox(line, "reads_active_low")]),
        this.make("td", {}, [this.checkBox(line, "is_enabled")]),
        this.make("td", {}, [this.numberField(line, "debounce_milliseconds")]),
        this.make("td", {}, [this.removeButton("in", line)]),
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
          this.make("th", { text: "" }),
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
        this.make("td", {}, [this.pinField(line, this.boardOutputPins, lineNumberCell, "out")]),
        this.make("td", {}, [this.checkBox(line, "safe_level_is_high")]),
        this.make("td", {}, [this.removeButton("out", line)]),
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
          this.make("th", { text: "" }),
        ]),
      ]),
      this.make("tbody", {}, rows),
    ]);
  }

  // ------------------------------------------------------ adding a line ---
  //
  // What a person adds is not a *line* -- the board's lines exist whether
  // anybody names them or not, and which pin each is was compiled into the
  // firmware. What is added is a **name for one**, which is why a new row
  // arrives already on a pin: the free one with the lowest line number, since
  // an unnamed pin is exactly what there is to claim. A row with no pin would
  // be a row the daemon refuses.
  //
  // Removing is the same fact backwards. The line stays; the name stops. A
  // graph naming it goes on naming something that is no longer there, which is
  // why `draftConflicts` checks the loaded config's graphs and holds the save.

  /// The pins of one direction that no row in the draft has claimed.
  unclaimedPins(direction) {
    const boardPins = direction === "in" ? this.boardInputPins : this.boardOutputPins;
    const lines = direction === "in" ? this.draft.input_lines : this.draft.output_lines;
    const claimed = new Set(lines.map((line) => line.pin_label));
    return boardPins.filter((pinLabel) => !claimed.has(pinLabel));
  }

  addLine(direction) {
    const free = this.unclaimedPins(direction);
    const boardPins = direction === "in" ? this.boardInputPins : this.boardOutputPins;
    // Where the board did not answer, there is no list to take a free pin from
    // -- so the row arrives blank and the person types the label their own
    // notes use. It is the same fallback the pin column already makes.
    const pinLabel = free.length > 0 ? free[0] : "";
    const lineIndex = boardPins.indexOf(pinLabel);
    const where = direction === "in" ? "input" : "output";
    const line =
      direction === "in"
        ? {
            name: this.aFreeNameFor(where, lineIndex),
            line_index: lineIndex < 0 ? null : lineIndex,
            pin_label: pinLabel,
            reads_active_low: false,
            is_enabled: true,
            debounce_milliseconds: 0,
          }
        : {
            name: this.aFreeNameFor(where, lineIndex),
            line_index: lineIndex < 0 ? null : lineIndex,
            pin_label: pinLabel,
            safe_level_is_high: false,
          };
    (direction === "in" ? this.draft.input_lines : this.draft.output_lines).push(line);
    this.theShapeOfTheDraftChanged();
  }

  addEveryUnclaimedPin(direction) {
    for (let remaining = this.unclaimedPins(direction).length; remaining > 0; remaining -= 1) {
      this.addLine(direction);
    }
  }

  removeLine(direction, line) {
    const lines = direction === "in" ? this.draft.input_lines : this.draft.output_lines;
    const at = lines.indexOf(line);
    if (at >= 0) lines.splice(at, 1);
    this.theShapeOfTheDraftChanged();
  }

  /// A placeholder nothing else is called. Named after the line it is on,
  /// because that is the one thing about a new row that is already true.
  aFreeNameFor(where, lineIndex) {
    const taken = new Set(
      [...this.draft.input_lines, ...this.draft.output_lines].map((line) => line.name),
    );
    const stem = lineIndex < 0 ? `${where}_line` : `${where}_${lineIndex}`;
    if (!taken.has(stem)) return stem;
    for (let suffix = 2; ; suffix += 1) {
      if (!taken.has(`${stem}_${suffix}`)) return `${stem}_${suffix}`;
    }
  }

  /// A row appeared or went away, so the tables have to be rebuilt.
  ///
  /// The generation counter is what the poll consults, but waiting for the next
  /// poll would leave a click looking ignored for half a second -- so this
  /// rebuilds now and tells the poll it has. The dots come back on the next
  /// read; `setLevel` is written to survive a dot whose line it has no reading
  /// for yet.
  theShapeOfTheDraftChanged() {
    this.hasUnsavedEdits = true;
    this.draftGeneration += 1;
    this.buildTables();
    this.builtGeneration = this.draftGeneration;
    this.showPinConflicts();
    this.revertButton.disabled = false;
  }

  /// "add a line", and what is left to add.
  ///
  /// The count is the useful half: a board has a fixed number of lines, and the
  /// question somebody actually has is "is there another input free" -- which
  /// no list of the rows already named can answer.
  addControls(direction) {
    const free = this.unclaimedPins(direction);
    const where = direction === "in" ? "input" : "output";
    const knowsThePins = this.pinLabelSource === "device";
    return this.make("div", { class: "row" }, [
      this.make("button", {
        text: `add an ${where} line`,
        disabled: knowsThePins && free.length === 0,
        onClick: () => this.addLine(direction),
      }),
      free.length > 1 && knowsThePins
        ? this.make("button", {
            text: `name all ${free.length} remaining`,
            onClick: () => this.addEveryUnclaimedPin(direction),
          })
        : null,
      this.make("span", {
        class: "muted",
        text: !knowsThePins
          ? "  This board did not say what its pins are called, so a new row arrives blank."
          : free.length === 0
            ? `  Every ${where} pin on this board has a name.`
            : `  Free: ${free.join(", ")}`,
      }),
    ]);
  }

  /// Remove the name, not the line. Says what it would break, where it would.
  removeButton(direction, line) {
    const usedBy = this.namesUsedByGraphs[direction].get(line.name) || [];
    return this.make("button", {
      text: "remove",
      title:
        usedBy.length > 0
          ? `${line.name} is named by ${usedBy.join(", ")}; removing it holds the save`
          : `stop naming ${line.pin_label || "this line"}`,
      onClick: () => this.removeLine(direction, line),
    });
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
    this.revertButton.disabled = false;
    // Renaming can create or clear a duplicate name, and this is also what
    // enables the save button -- through the check rather than around it, so a
    // map the daemon would refuse can never be offered.
    if (key === "name") this.showPinConflicts();
    else this.saveButton.disabled = false;
  }

  async save() {
    const saved = await this.attempt(() => this.api.replaceLineMap(this.draft));
    if (saved === null) return;
    // Re-read rather than trust the draft: the daemon is the authority on what
    // the map now is, and a save that was accepted with something normalised
    // should show the normalised version.
    this.discardTheDraft();
    // A rename that was saved is now what the graphs have to agree with, and
    // the config on disk may have moved under this panel besides.
    this.readWhichLinesTheGraphsName();
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

/// Every line name a graph mentions, as [direction, name].
///
/// Two places name a line and they are different directions: a transition's
/// predicate watches **inputs** (`all`/`any`/`none`), and a state's entry and
/// exit actions drive **outputs**. Collapsing them would be the same mistake
/// the line map itself refuses -- input line 3 and output line 3 are different
/// pins -- so an input called `lever` does not protect an output called
/// `lever` from being removed.
export function lineNamesUsedBy(graph) {
  const used = [];
  for (const state of graph.states || []) {
    for (const action of [...(state.on_entry || []), ...(state.on_exit || [])]) {
      if (action.line) used.push(["out", action.line]);
    }
    for (const transition of state.transitions || []) {
      const predicate = transition.when || {};
      for (const key of ["all", "any", "none"]) {
        for (const name of predicate[key] || []) used.push(["in", name]);
      }
    }
  }
  return used;
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

/// Values held by more than one line, with the names holding them.
function sharedValues(lines, key) {
  const namesByValue = new Map();
  for (const line of lines) {
    const value = line[key];
    if (value === null || value === undefined || value === "") continue;
    namesByValue.set(value, [...(namesByValue.get(value) || []), line.name || "(unnamed)"]);
  }
  return [...namesByValue].filter(([, names]) => names.length > 1);
}
