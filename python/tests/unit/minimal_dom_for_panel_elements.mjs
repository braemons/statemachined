// SPDX-License-Identifier: GPL-3.0-or-later
//
// A DOM small enough to hold a panel, and no smaller.
//
// The web UI has no build step and no framework, which is a deliberate choice
// (dev/DAEMON.md §5) and costs it a test runner: there is no jsdom here, and
// pulling one in would put a node toolchain between a rig and its own UI.
//
// What the panels actually use is a dozen DOM methods -- createElement,
// append, replaceChildren, className, textContent, a few properties -- so this
// implements those against plain objects. It is enough to answer the one
// question no amount of reading the source answers reliably: **after a poll,
// is the field still the same element?** A rebuilt field is a field somebody's
// cursor has just been thrown out of.
//
// It is not a browser and must not grow into one. Nothing here lays anything
// out, nothing computes a style, and no test should ask it to.

class MinimalNode {
  constructor(tagName) {
    this.tagName = tagName;
    this.children = [];
    this.parentNode = null;
    this.className = "";
    this.textContent = "";
    this.attributes = {};
    this.listeners = {};
    this.style = "";
    // Declared so that `make()` assigns them as properties, which is what it
    // does on a real element and what the panels then read back. A real <div>
    // has no `value`; nothing here cares, and a test that did would be testing
    // this file rather than a panel.
    this.type = "";
    this.value = "";
    this.checked = false;
    this.selected = false;
    this.disabled = false;
    this.title = "";
    // What a real shadow root answers when nothing in it has the focus, which
    // is always true here: nothing in this DOM can be focused. Declared
    // because a panel repainting itself asks for it, and `undefined` is not
    // the answer a browser gives.
    this.activeElement = null;
  }

  append(...nodes) {
    for (const node of nodes) {
      if (node === null || node === undefined) continue;
      node.parentNode = this;
      this.children.push(node);
    }
  }

  replaceChildren(...nodes) {
    for (const child of this.children) child.parentNode = null;
    this.children = [];
    this.append(...nodes);
  }

  addEventListener(name, handler) {
    (this.listeners[name] ||= []).push(handler);
  }

  /// Deliver one event to whatever is listening, the way a click or a keystroke
  /// would. Enough for a test to be a person.
  dispatch(name, event = {}) {
    for (const handler of this.listeners[name] || []) handler({ target: this, ...event });
  }

  setAttribute(name, value) {
    this.attributes[name] = `${value}`;
  }

  getAttribute(name) {
    return name in this.attributes ? this.attributes[name] : null;
  }

  /// Every node under this one, in document order.
  descendants() {
    return this.children.flatMap((child) => [child, ...child.descendants()]);
  }

  /// The first descendant a predicate accepts, or null.
  find(accepts) {
    return this.descendants().find(accepts) ?? null;
  }
}

export function installMinimalDom() {
  globalThis.document = { createElement: (tagName) => new MinimalNode(tagName) };
  // No adoptedStyleSheets on it, so adoptSharedStyles takes its <style>
  // fallback -- which is the path a browser without constructed stylesheets
  // takes too, and the one worth exercising here.
  globalThis.Document = class {};
  globalThis.HTMLElement = class {
    attachShadow() {
      this.shadowRoot = new MinimalNode("#shadow-root");
      return this.shadowRoot;
    }
  };
  globalThis.customElements = { get: () => undefined, define: () => {} };
}

export { MinimalNode };
