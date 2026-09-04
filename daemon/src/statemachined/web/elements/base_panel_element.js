// SPDX-License-Identifier: LGPL-3.0-or-later
//
// What every panel has in common: a shadow root, a `base` attribute, a way to
// poll without leaking a timer, and one honest place for a refusal to land.
//
// The polling is worth a word. Panels poll rather than each opening a
// WebSocket, except the two that are streams by nature (state and trace),
// because a browser tab with six panels open would otherwise hold six sockets
// on a daemon whose whole reason for existing is one serial port. Polling a
// snapshot at 1 Hz costs a rig nothing and cannot fall behind.

import { DaemonApiClient, DaemonRefusedTheRequest } from "./daemon_api_client.js";
import { adoptSharedStyles } from "./shared_panel_stylesheet.js";

export class BasePanelElement extends HTMLElement {
  static observedAttributes = ["base"];

  constructor() {
    super();
    this.root = this.attachShadow({ mode: "open" });
    adoptSharedStyles(this.root);
    this.pollTimers = [];
    this.openSockets = [];
    this.failure = null;
  }

  get api() {
    return new DaemonApiClient(this.getAttribute("base") || "");
  }

  connectedCallback() {
    this.renderShell();
    this.start();
  }

  disconnectedCallback() {
    // A panel removed from the DOM must stop talking to the rig. Not tidiness:
    // a console that swaps panels on every nav click would otherwise accumulate
    // pollers against a daemon holding one serial port.
    this.stop();
  }

  attributeChangedCallback(name, previous, current) {
    if (previous !== current && this.isConnected) {
      this.stop();
      this.start();
    }
  }

  /// Subclasses override these three.
  renderShell() {}
  start() {}
  stopped() {}

  stop() {
    for (const timer of this.pollTimers) clearInterval(timer);
    this.pollTimers = [];
    for (const socket of this.openSockets) {
      try {
        socket.close();
      } catch {
        /* already closing */
      }
    }
    this.openSockets = [];
    this.stopped();
  }

  /// Call `read` now and every `seconds`, and never let two overlap.
  ///
  /// The overlap guard matters on a device call: `GET /api/device` reads the
  /// board under the daemon's one device lock, and a panel that fired a second
  /// read before the first returned would queue work behind that lock in a
  /// trial's critical path.
  pollEvery(seconds, read) {
    let inFlight = false;
    const once = async () => {
      if (inFlight) return;
      inFlight = true;
      try {
        await read();
        this.clearFailure();
      } catch (error) {
        this.showFailure(error);
      } finally {
        inFlight = false;
      }
    };
    once();
    this.pollTimers.push(setInterval(once, seconds * 1000));
    return once;
  }

  /// Run one action, showing whatever it refuses with.
  async attempt(action) {
    try {
      const result = await action();
      this.clearFailure();
      return result;
    } catch (error) {
      this.showFailure(error);
      return null;
    }
  }

  trackSocket(socket) {
    this.openSockets.push(socket);
    return socket;
  }

  // ------------------------------------------------------------ failures ---

  showFailure(error) {
    this.failure = error;
    this.paintFailure();
  }

  clearFailure() {
    if (this.failure !== null) {
      this.failure = null;
      this.paintFailure();
    }
  }

  paintFailure() {
    const slot = this.root.querySelector(".failure-slot");
    if (slot === null) return;
    if (this.failure === null) {
      slot.replaceChildren();
      return;
    }
    const banner = document.createElement("div");
    banner.className = "failure";
    const error = this.failure;
    if (error instanceof DaemonRefusedTheRequest) {
      // Every refusal names what to change (dev/API.md §2), so show it: a UI
      // that renders only the status code throws away the useful half.
      banner.textContent = `${error.code}: ${error.detail}`;
      if (error.context) {
        const context = document.createElement("span");
        context.className = "context";
        context.textContent = `  (${error.context})`;
        banner.append(context);
      }
    } else {
      banner.textContent = `${error}`;
    }
    slot.replaceChildren(banner);
  }

  // ------------------------------------------------------------- markup ---
  //
  // Built rather than templated. There is no build step and no framework here,
  // so the alternative is string concatenation into innerHTML -- which is how a
  // graph called `<script>` becomes an execution.

  make(tag, properties = {}, children = []) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(properties)) {
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key.startsWith("on") && typeof value === "function") {
        node.addEventListener(key.slice(2).toLowerCase(), value);
      } else if (key === "dataset") Object.assign(node.dataset, value);
      else if (key in node) node[key] = value;
      else node.setAttribute(key, value);
    }
    node.append(...children.filter((child) => child !== null && child !== undefined));
    return node;
  }

  /// Repaint, and put the cursor back where the person left it.
  ///
  /// There is no framework here, so a repaint means rebuilding a subtree -- and
  /// the rebuilt subtree does not contain the element that had focus, so the
  /// cursor lands on the floor mid-word. Two different fixes, and the
  /// difference matters:
  ///
  ///   * a repaint driven by a **poll** should not happen at all. A device
  ///     reading itself twice a second must not touch a field somebody is
  ///     typing in, and preserving focus afterwards is a worse version of not
  ///     disturbing it. See line_map_panel_element.js, which rebuilds only when
  ///     the shape of the thing changed.
  ///   * a repaint driven by **the person's own edit** has to happen -- a
  ///     renamed state has to appear in the transitions that name it -- and the
  ///     structure it rebuilds is the same structure. That is this.
  repaintPreservingFocus(repaint) {
    const active = this.root.activeElement;
    const path = active === null ? null : this.pathToDescendant(active);
    // Number inputs throw on selectionStart in some browsers, so ask carefully.
    let selectionStart = null;
    let selectionEnd = null;
    try {
      selectionStart = active?.selectionStart ?? null;
      selectionEnd = active?.selectionEnd ?? null;
    } catch {
      /* a field with no text selection. Focus is still worth restoring. */
    }

    repaint();

    if (path === null) return;
    const restored = this.descendantAtPath(path);
    if (restored === null || typeof restored.focus !== "function") return;
    restored.focus();
    if (selectionStart === null || typeof restored.setSelectionRange !== "function") return;
    try {
      restored.setSelectionRange(selectionStart, selectionEnd);
    } catch {
      /* not a field that carries a selection */
    }
  }

  /// Where a node is, as the chain of child indices from this panel's root.
  ///
  /// An index chain rather than an id: nothing in this UI has ids, the fields
  /// are built in loops over the thing being edited, and the rebuild that is
  /// about to happen produces the same tree with one value different.
  pathToDescendant(node) {
    const path = [];
    let current = node;
    while (current !== null && current !== this.root) {
      const parent = current.parentNode;
      if (parent === null || parent === undefined) return null;
      path.unshift(Array.prototype.indexOf.call(parent.children, current));
      current = parent;
    }
    return current === this.root ? path : null;
  }

  descendantAtPath(path) {
    let node = this.root;
    for (const index of path) {
      const children = node.children;
      if (!children || index < 0 || index >= children.length) return null;
      node = children[index];
    }
    return node === this.root ? null : node;
  }

  /// A definition list of label/value pairs -- the shape most of this UI is.
  fieldList(pairs) {
    const list = this.make("dl", { class: "fields" });
    for (const [label, value] of pairs) {
      list.append(
        this.make("dt", { text: label }),
        value instanceof Node ? this.make("dd", {}, [value]) : this.make("dd", { text: `${value}` }),
      );
    }
    return list;
  }

  /// A word as lit and unlit dots, low bit on the left -- the same order
  /// `statemachined state` prints, so a person can compare the two.
  levelDots(word, count) {
    const row = this.make("span", { class: "row", style: "gap:0.25rem" });
    for (let index = 0; index < count; index += 1) {
      const high = ((word >> index) & 1) === 1;
      row.append(
        this.make("span", {
          class: high ? "level high" : "level",
          title: `line ${index}: ${high ? "high" : "low"}`,
        }),
      );
    }
    return row;
  }
}

/// Registering twice is not an error worth throwing over: a console may load
/// this module and one of its panels' modules, and the second registration
/// would take down the page it was meant to draw.
export function defineElementOnce(tagName, elementClass) {
  if (!customElements.get(tagName)) customElements.define(tagName, elementClass);
}

export { DaemonRefusedTheRequest };
