// SPDX-License-Identifier: LGPL-3.0-or-later
//
// `<statemachined-device>` -- what board is attached, and how the link behaves.
//
// This is what `statemachined state` prints today, named and updating. The scan
// counters are on it deliberately: a board that quietly misses scans looks
// exactly like a board that is fine, so the number that says otherwise belongs
// where somebody is already looking.

import { BasePanelElement, defineElementOnce } from "./base_panel_element.js";

const POLL_SECONDS = 1;

export class DevicePanelElement extends BasePanelElement {
  renderShell() {
    this.failureSlot = this.make("div", { class: "failure-slot" });
    this.body = this.make("div", { text: "reading the device..." });
    this.connectButton = this.make("button", {
      text: "connect",
      onClick: () => this.attempt(() => this.api.connectToTheDevice()),
    });
    this.description = this.make("p", {
      class: "muted",
      text:
        "The board this daemon owns: what it is, what it can hold, and how well it is " +
        "keeping to its scan. A board that quietly misses scans looks exactly like one that " +
        "is fine, which is why the misses are counted here.",
    });
    this.root.replaceChildren(
      this.make("section", {}, [
        this.make("h2", {}, [
          this.make("span", { text: "Device" }),
          this.make("span", { class: "spacer" }),
          this.connectButton,
        ]),
        this.description,
        this.failureSlot,
        this.body,
      ]),
    );
  }

  start() {
    this.pollEvery(POLL_SECONDS, async () => {
      const [device, state] = await Promise.all([this.api.readDevice(), this.api.readState()]);
      this.paint(device, state);
    });
  }

  paint(device, state) {
    const capabilities = device.capabilities || {};
    const scan = device.scan || {};
    const link = device.link || {};
    const committed = device.committed_set;

    this.connectButton.disabled = device.connected;
    this.connectButton.textContent = device.connected ? "connected" : "connect";

    const inputCount = capabilities.input_line_count || 8;
    const outputCount = capabilities.output_line_count || 8;

    this.body.replaceChildren(
      this.fieldList([
        [
          "link",
          this.make("span", {
            class: device.connected ? "pill good" : "pill bad",
            text: device.connected ? "connected" : "no device",
          }),
        ],
        ["target", this.make("span", { class: "mono", text: device.target || "-" })],
        ["board", device.board ?? "-"],
        ["firmware", device.firmware_version ?? "-"],
        ["protocol", device.protocol_version ?? "-"],
        ["scan", this.scanSummary(device, scan)],
        [
          "graph set",
          committed
            ? `v${committed.set_version}: ${(committed.graph_names || []).join(", ")}`
            : "none uploaded",
        ],
        [
          "wiring",
          device.has_wiring
            ? this.make("span", { class: "pill good", text: "pushed" })
            : this.make("span", {
                class: "pill warn",
                title:
                  "The board is at its compile-time safe levels. Data flash is M7; " +
                  "until then the daemon pushes the wiring on every connect.",
                text: "compile-time defaults",
              }),
        ],
        [
          "lines",
          link.dropped_lines || link.bad_lines
            ? this.make("span", {
                class: "bad",
                text: `dropped ${link.dropped_lines}, bad ${link.bad_lines}`,
              })
            : `dropped ${link.dropped_lines ?? 0}, bad ${link.bad_lines ?? 0}`,
        ],
        ["connections", link.connection_count ?? 0],
        ["up", formatDeviceMicroseconds(device.uptime_device_microseconds)],
        ["in", this.levelDots(state.input_word || 0, inputCount)],
        ["out", this.levelDots(state.output_word || 0, outputCount)],
        [
          "capacity",
          this.make("span", {
            class: "mono muted",
            text: Object.entries(capabilities)
              .map(([name, value]) => `${name}=${value}`)
              .join("  "),
          }),
        ],
      ]),
    );
    if (link.last_error) {
      this.body.append(
        this.make("p", { class: "warn", text: `last refusal from the device: ${link.last_error}` }),
      );
    }
  }

  /// The honest half of the timing claim, so it is coloured rather than listed.
  scanSummary(device, scan) {
    const overruns = scan.overruns ?? 0;
    const stalls = scan.tx_stalls ?? 0;
    const text =
      `${scan.hz ?? device.measured_scan_hz ?? "?"} Hz` +
      `  overruns ${overruns}  worst gap ${scan.worst_gap ?? 0}  tx stalls ${stalls}`;
    return this.make("span", {
      class: overruns > 0 || stalls > 0 ? "bad" : "",
      title:
        overruns > 0
          ? "A scan was late. The device's timing is what this whole system is for, " +
            "so this is a finding rather than a statistic."
          : "",
      text,
    });
  }
}

export function formatDeviceMicroseconds(microseconds) {
  if (typeof microseconds !== "number") return "-";
  const seconds = microseconds / 1e6;
  if (seconds < 90) return `${seconds.toFixed(1)} s`;
  const minutes = seconds / 60;
  if (minutes < 90) return `${minutes.toFixed(1)} min`;
  return `${(minutes / 60).toFixed(1)} h`;
}

defineElementOnce("statemachined-device", DevicePanelElement);
