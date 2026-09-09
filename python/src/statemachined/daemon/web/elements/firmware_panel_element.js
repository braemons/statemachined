// SPDX-License-Identifier: LGPL-3.0-or-later
//
// `<statemachined-firmware>` -- what is running against what is installed.
//
// The comparison is the whole panel. A board in a rack cannot be asked which
// commit it is running, so the package carries a manifest and the daemon says
// whether the two agree; a rig running last month's firmware against this
// month's daemon is a thing somebody should find out about from a page rather
// than from a paradigm that behaves oddly.
//
// **Flashing is deliberately not here.** It means dropping the port
// mid-session, which is a different risk from anything else this daemon does --
// docs/developer/daemon.md §6.3 and docs/reference/api.md §3.

import { BasePanelElement, defineElementOnce } from "./base_panel_element.js";

export class FirmwarePanelElement extends BasePanelElement {
  renderShell() {
    this.failureSlot = this.make("div", { class: "failure-slot" });
    this.body = this.make("div", { text: "asking the board..." });
    this.description = this.make("p", {
      class: "muted",
      text:
        "What is running on the board, against what this package ships. Flashing is not " +
        "offered here: it means dropping the port mid-session, which is a different risk " +
        "from anything else this daemon does.",
    });
    this.root.replaceChildren(
      this.make("section", {}, [
        this.make("h2", {}, [this.make("span", { text: "Firmware" })]),
        this.description,
        this.failureSlot,
        this.body,
      ]),
    );
  }

  start() {
    this.pollEvery(5, async () => {
      const versions = await this.api.readFirmwareVersions();
      this.paint(versions);
    });
  }

  paint(versions) {
    this.body.replaceChildren(
      this.fieldList([
        ["running on the board", this.make("span", { class: "mono", text: versions.running })],
        [
          "installed by the package",
          this.make("span", {
            class: "mono",
            text: versions.installed ?? "nothing installed here",
          }),
        ],
        ["agree", this.verdict(versions)],
      ]),
      this.make("p", {
        class: "muted",
        text:
          "Flashing is not offered here: it means dropping the port mid-session. " +
          "Use the package's own image and the board's bootloader.",
      }),
    );
  }

  verdict(versions) {
    if (versions.installed === null || versions.installed === undefined) {
      // Not an error. `make image` writes the manifest and the package installs
      // it, so its absence means "nobody installed a firmware image here",
      // which is the truth on a developer's machine.
      return this.make("span", {
        class: "pill",
        text: "no installed image to compare against",
      });
    }
    return versions.matches
      ? this.make("span", { class: "pill good", text: "yes" })
      : this.make("span", {
          class: "pill bad",
          text: "no -- the board is running something else",
        });
  }
}

defineElementOnce("statemachined-firmware", FirmwarePanelElement);
