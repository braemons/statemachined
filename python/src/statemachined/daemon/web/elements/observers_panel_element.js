// SPDX-License-Identifier: LGPL-3.0-or-later
//
// `<statemachined-observers>` -- who is watching this rig, right now.
//
// **This daemon reports to nobody.** It publishes what it did to its trace and
// assumes nobody read it; the trace and the recording exist on the rig whether
// anything is listening or not. So there is nothing to configure here, no list
// to edit, and no observer this rig is waiting for. Opening a stream is the
// whole of subscribing and closing it is the whole of leaving.
//
// **The panel is a debugging aid, and that is the whole of its job.** When
// trials stop reaching triald there are two very different faults -- nothing is
// connected, or something is connected and receiving nothing -- and without
// this the way to tell them apart is a packet capture. That is why `delivered`
// is a column: a connection that is up with a counter that never moves is the
// interesting one.
//
// Names are self-declared, from `?observer=` on the stream URL. Nothing is
// granted by one and there is no registration to forge, so a wrong name is a
// wrong label on this screen and nothing else. Shown as given.

import { BasePanelElement, defineElementOnce } from "./base_panel_element.js";

const POLL_SECONDS = 2;

export class ObserversPanelElement extends BasePanelElement {
  constructor() {
    super();
    this.observers = [];
    this.everSaw = false;
  }

  renderShell() {
    this.failureSlot = this.make("div", { class: "failure-slot" });
    this.listSlot = this.make("div", { text: "asking..." });
    this.root.replaceChildren(
      this.make("section", {}, [
        this.make("h2", {}, [this.make("span", { text: "Observers" })]),
        this.make("p", {
          class: "muted",
          text:
            "Everything watching this rig at the moment. The daemon reports to nobody and " +
            "waits for nobody -- it writes to its trace, and whoever wants it opens a stream. " +
            "This list is a debugging aid: it is how you tell 'nothing is connected' from " +
            "'something is connected and getting nothing'.",
        }),
        this.failureSlot,
        this.listSlot,
      ]),
    );
  }

  start() {
    this.pollEvery(POLL_SECONDS, async () => {
      const listed = await this.api.listObservers();
      this.observers = listed.observers ?? [];
      if (this.observers.length > 0) this.everSaw = true;
      this.paint();
    });
  }

  paint() {
    this.repaintPreservingFocus(() =>
      this.listSlot.replaceChildren(
        this.observers.length === 0 ? this.nobodyIsWatching() : this.theWatchers(),
      ),
    );
  }

  nobodyIsWatching() {
    return this.make("div", {}, [
      this.make("p", { class: "muted", text: "Nothing is watching." }),
      this.make("p", {
        class: "muted",
        text:
          this.everSaw
            ? "Something was, and has gone. A trial still runs and is still written to the trace."
            : "Not a fault. A rig with nothing watching runs trials and records them exactly " +
              "the same; this is what a bench box looks like all day.",
      }),
    ]);
  }

  theWatchers() {
    const rows = this.observers.map((observer) => this.oneWatcher(observer));
    return this.make("div", {}, rows);
  }

  oneWatcher(observer) {
    const stalled = observer.delivered === 0;
    return this.make("div", { class: "row-block" }, [
      this.make("p", {}, [
        this.make("span", {
          class: observer.fell_behind ? "pill bad" : stalled ? "pill" : "pill good",
          text: observer.fell_behind ? "fell behind" : stalled ? "nothing yet" : "receiving",
        }),
        this.make("strong", { text: `  ${observer.name}` }),
      ]),
      this.fieldList([
        ["stream", describeStream(observer.stream)],
        ["from", observer.address ?? "(no address)"],
        ["watching for", describeDuration(observer.connected_seconds)],
        ["messages sent", `${observer.delivered}`],
      ]),
      observer.fell_behind
        ? this.make("p", {
            class: "failure",
            text:
              "This one was too slow for the trace ring and genuinely lost entries. It was " +
              "told which ones and disconnected, rather than handed a shorter answer that " +
              "looked complete. It can fetch the trials it missed by id.",
          })
        : null,
    ].filter(Boolean));
  }
}

/// The two streams differ in the one way that matters to somebody debugging.
export function describeStream(stream) {
  if (stream === "trace") return "trace -- every entry, in order, none skipped";
  if (stream === "state") return "state -- the latest snapshot, coalesced";
  return stream ?? "(unknown)";
}

export function describeDuration(seconds) {
  const whole = Math.max(0, Math.floor(seconds ?? 0));
  if (whole < 60) return `${whole}s`;
  if (whole < 3600) return `${Math.floor(whole / 60)}m ${whole % 60}s`;
  return `${Math.floor(whole / 3600)}h ${Math.floor((whole % 3600) / 60)}m`;
}

defineElementOnce("statemachined-observers", ObserversPanelElement);
