// SPDX-License-Identifier: LGPL-3.0-or-later
//
// `<statemachined-configs>` -- what this rig is wired like, saved and loaded.
//
// **Two configurations, and this panel is one of them.** The rig config is the
// box -- the device, the directories, where triald is -- and it is a hand-edited
// conffile in `/etc/braemons` that the daemon never writes. A *state-machine
// config* is the line map and the graphs, it lives in
// `/var/lib/braemons/statemachined/configs/`, and it is this. The split is
// mechanical rather than aesthetic: a conffile a daemon rewrites is a file that
// fights the package manager on every upgrade, and the line map is exactly the
// thing somebody adjusts at the bench on a Tuesday.
//
// **Three verbs, deliberately not one button.** Saving writes a file and
// touches no hardware. Loading applies a config to the rig -- the map is
// resolved against the board, refused if this board does not have those pins,
// and pushed. Opening a session puts the graphs on the device. They are
// separate because they fail differently and at different moments: a save can
// happen mid-session, a load cannot happen mid-trial, and an upload is the
// slowest call in the API and the one where a session is allowed to fail.
//
// **Loaded is not open, and the panel says both.** A rig can be wired and
// holding no graphs -- which is how it comes up after a power cut, on purpose,
// because a daemon that armed itself on boot would be a rig ready to run trials
// nobody asked for. A board can also still hold a committed set from a session
// that ended, which is normal and is what makes a reconnect cheap.
//
// The line map is edited in the Lines panel rather than here. This panel is
// about *which* config; that one is about what is in it.

import { BasePanelElement, defineElementOnce } from "./base_panel_element.js";

const POLL_SECONDS = 2;

export class StateMachineConfigPanelElement extends BasePanelElement {
  constructor() {
    super();
    this.configs = [];
    this.loadedName = null;
    this.session = null;
    //: What the person typed into "save the running config as". Held here
    //: rather than read off the field at save time, so a poll that repaints the
    //: list cannot take a half-typed name with it.
    this.saveAsName = "";
  }

  renderShell() {
    this.failureSlot = this.make("div", { class: "failure-slot" });
    this.sessionSlot = this.make("div", { text: "reading the session..." });
    this.listSlot = this.make("div");
    this.saveSlot = this.make("div");
    this.root.replaceChildren(
      this.make("section", {}, [
        this.make("h2", {}, [this.make("span", { text: "State-machine configs" })]),
        this.make("p", {
          class: "muted",
          text:
            "What this rig is wired like and what it can run, in one saved file: the line " +
            "map and the graphs together. Loading one names the lines and pushes the wiring " +
            "to the board; opening a session puts its graphs on the device. The other half " +
            "of the configuration -- the device, the directories, where triald is -- is the " +
            "rig config, which is hand-edited in /etc/braemons and not written from here.",
        }),
        this.failureSlot,
        this.sessionSlot,
        this.listSlot,
        this.saveSlot,
      ]),
    );
  }

  start() {
    this.pollEvery(POLL_SECONDS, async () => {
      const [listed, session] = await Promise.all([
        this.api.listStateMachineConfigs(),
        this.api.readSession(),
      ]);
      this.configs = listed.configs;
      this.loadedName = listed.loaded;
      this.session = session;
      this.paint();
    });
  }

  // --------------------------------------------------------------- paint ---

  paint() {
    this.paintSession();
    this.paintList();
    this.paintSaveAs();
  }

  paintSession() {
    const session = this.session;
    if (session === null) return;
    const config = session.state_machine_config;

    const rows = [];
    if (config === null) {
      rows.push(
        this.make("p", {
          class: "muted",
          text:
            "Nothing is loaded, so this rig has no line names and no graphs. Load a config " +
            "below. A rig config may name one to load at startup, which is what lets a box " +
            "come back from a power cut already wired.",
        }),
      );
    } else {
      rows.push(
        this.make("p", {}, [
          this.make("strong", { text: config.name }),
          this.make("span", {
            class: "muted",
            text: config.description ? `  ${config.description}` : "",
          }),
        ]),
      );
      if (!config.is_still_in_the_store) {
        // Allowed, and worth saying out loud: deleting a file is not a request
        // to stop an experiment, so the rig goes on running what it was given.
        rows.push(
          this.make("p", {
            class: "muted",
            text:
              "This config has been deleted from the store. The rig is still running it, and " +
              "will lose it on the next restart -- save it under a name to keep it.",
          }),
        );
      }
      rows.push(
        this.make("p", {
          class: "muted",
          text: `Graphs in it: ${config.graph_names.join(", ") || "(none)"}`,
        }),
      );
    }

    const committed = session.committed_set;
    rows.push(
      this.make("p", {
        text: session.is_open
          ? `Session open for ${Math.round(session.open_seconds)} s.`
          : "No session is open.",
      }),
    );
    if (committed !== null) {
      rows.push(
        this.make("p", {
          class: "muted",
          text:
            `The board holds set ${committed.set_version}: ` +
            `${committed.graph_names.join(", ")}. ` +
            (session.is_open
              ? ""
              : "Left there on purpose -- a committed set surviving is what makes a " +
                "reconnect cheap."),
        }),
      );
    }

    rows.push(
      this.make("div", { class: "row" }, [
        this.make("button", {
          class: "primary",
          text: "open a session",
          disabled: config === null || session.is_open,
          onClick: () => this.openSession(),
        }),
        this.make("button", {
          text: "close it",
          disabled: !session.is_open,
          onClick: () => this.closeSession(),
        }),
      ]),
    );
    if (config !== null && !session.is_open) {
      rows.push(
        this.make("p", {
          class: "muted",
          text:
            "On a rig with triald, triald opens the session. This button is the same call, " +
            "for a bench where nobody is driving.",
        }),
      );
    }
    this.sessionSlot.replaceChildren(this.make("div", {}, rows));
  }

  paintList() {
    if (this.configs.length === 0) {
      this.listSlot.replaceChildren(
        this.make("p", { class: "muted", text: "No configs are saved on this rig yet." }),
      );
      return;
    }
    const rows = this.configs.map((config) => this.rowFor(config));
    this.listSlot.replaceChildren(this.make("div", { class: "config-list" }, rows));
  }

  rowFor(config) {
    // A config that would not parse is listed with its reason rather than
    // omitted: a file you cannot see is a file you cannot fix.
    if (config.unreadable !== undefined) {
      return this.make("div", { class: "config-row" }, [
        this.make("span", {}, [this.make("strong", { text: config.name })]),
        this.make("span", { class: "muted", text: `will not load: ${config.unreadable}` }),
        this.make("button", {
          text: "delete",
          onClick: () => this.deleteConfig(config.name),
        }),
      ]);
    }
    const isLoaded = config.name === this.loadedName;
    return this.make("div", { class: "config-row" }, [
      this.make("span", {}, [
        this.make("strong", { text: config.name }),
        this.make("span", { class: "muted", text: isLoaded ? "  (loaded)" : "" }),
      ]),
      this.make("span", {
        class: "muted",
        text:
          `${config.input_line_count} in, ${config.output_line_count} out` +
          (config.board ? `  ${config.board}` : "") +
          (config.graph_names.length ? `  ${config.graph_names.join(", ")}` : "  no graphs"),
      }),
      this.make("button", {
        class: isLoaded ? "" : "primary",
        text: isLoaded ? "reload" : "load",
        onClick: () => this.loadConfig(config.name),
      }),
      this.make("button", {
        text: "delete",
        onClick: () => this.deleteConfig(config.name),
      }),
    ]);
  }

  paintSaveAs() {
    const nameField = this.make("input", {
      type: "text",
      value: this.saveAsName,
      placeholder: this.loadedName || "a name",
      onInput: (event) => {
        this.saveAsName = event.target.value;
      },
    });
    this.saveSlot.replaceChildren(
      this.make("div", {}, [
        this.make("h3", { text: "Save what is running" }),
        this.make("p", {
          class: "muted",
          text:
            "The line map on the board right now, with the loaded config's graphs, written " +
            "to a file. This is where an edit made in the Lines panel becomes permanent -- " +
            "until it is saved, it is on the board and in memory, and a restart loses it. " +
            "Saving over the loaded config does not re-apply it; nothing is pushed here.",
        }),
        this.make("div", { class: "row" }, [
          nameField,
          this.make("button", {
            class: "primary",
            text: "save",
            disabled: this.loadedName === null,
            onClick: () => this.saveRunningConfigAs(nameField.value.trim()),
          }),
        ]),
      ]),
    );
  }

  // -------------------------------------------------------------- actions ---

  async loadConfig(name) {
    const loaded = await this.attempt(() => this.api.loadStateMachineConfig(name));
    if (loaded !== null) await this.refresh();
  }

  async deleteConfig(name) {
    const deleted = await this.attempt(() => this.api.deleteStateMachineConfig(name));
    if (deleted !== null) await this.refresh();
  }

  async openSession() {
    const opened = await this.attempt(() => this.api.openSession());
    if (opened !== null) await this.refresh();
  }

  async closeSession() {
    const closed = await this.attempt(() => this.api.closeSession());
    if (closed !== null) await this.refresh();
  }

  async saveRunningConfigAs(name) {
    const wanted = name || this.loadedName;
    if (!wanted || this.loadedName === null) return;
    // Assembled from what the rig has *now* rather than from the stored file:
    // the whole point is to capture an edit that has reached the board and not
    // yet the disk. The lines come from the device route because that is the
    // one that knows what was pushed; the graphs and the description come from
    // the stored config, because nothing has been editing those here.
    const saved = await this.attempt(async () => {
      const [lines, stored] = await Promise.all([
        this.api.readDeviceLines(),
        this.api.readStateMachineConfig(this.loadedName),
      ]);
      return this.api.writeStateMachineConfig(wanted, {
        ...stored,
        name: wanted,
        line_map: {
          // `is_high_now` is added to a line by the device route and is not
          // part of a line map -- LineMap forbids members it does not declare,
          // so it has to come off before this goes back.
          input_lines: lines.input_lines.map(withoutTheLiveLevel),
          output_lines: lines.output_lines.map(withoutTheLiveLevel),
        },
      });
    });
    if (saved !== null) {
      this.saveAsName = "";
      await this.refresh();
    }
  }

  async refresh() {
    const [listed, session] = await Promise.all([
      this.api.listStateMachineConfigs(),
      this.api.readSession(),
    ]);
    this.configs = listed.configs;
    this.loadedName = listed.loaded;
    this.session = session;
    this.paint();
  }
}

function withoutTheLiveLevel(line) {
  const { is_high_now, ...definition } = line;
  return definition;
}

defineElementOnce("statemachined-configs", StateMachineConfigPanelElement);
