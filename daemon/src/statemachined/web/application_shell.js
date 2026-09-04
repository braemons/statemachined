// SPDX-License-Identifier: LGPL-3.0-or-later
//
// The rig's own page: navigation, and nothing else.
//
// Deliberately thin, and it is the same thinness the console repo is specified
// to have (dev/DAEMON.md §5). Every view here is one custom element with a
// shadow root; this file chooses which one is on screen and hands it the same
// `base` a console would. If this shell grew domain logic, the console would
// either duplicate it or do without -- so it has none, and the panels are the
// only place anything is decided.

import "/elements/statemachined.js";
import { DaemonApiClient } from "/elements/daemon_api_client.js";

//: Empty, meaning same-origin: this page is served by the daemon it is about.
//: A console passes its own value, which is why every panel takes it as an
//: attribute rather than assuming.
const BASE_URL = "";

// Each view says what it is, in a sentence, in the navigation and again at the
// top of the panel. "Session" and "Trace" are words this system uses in a
// particular way -- a session is the set of paradigms loaded on the board, a
// trace is the daemon's own record of what the machine did -- and a person
// opening this page for the first time has no way to know that from a tab
// label. The panels are used inside a console too, where there are no tabs at
// all, so each one carries its own description rather than relying on this.
const VIEWS = [
  {
    id: "device",
    label: "Device",
    tag: "statemachined-device",
    description: "The board on the other end of the cable: what it is, and whether it is well.",
  },
  {
    id: "lines",
    label: "Lines & wiring",
    tag: "statemachined-lines",
    description:
      "Which pin is the left lever, and what the rig does to each signal. Watch a line's " +
      "level here while pressing the thing wired to it -- that is the only check there is.",
  },
  {
    id: "graphs",
    label: "Paradigms",
    tag: "statemachined-graph",
    description:
      "The graphs this rig can run: states, timeouts, what ends a trial and as which outcome. " +
      "Authored against line names, so a graph outlives the box it was written on.",
  },
  {
    id: "session",
    label: "Session",
    tag: "statemachined-session",
    description:
      "The graphs loaded onto the board for this run, and the trial happening now. Every " +
      "paradigm a session will use is uploaded once, before an animal is in the booth; after " +
      "that a trial names one and starts in milliseconds.",
  },
  {
    id: "trace",
    label: "Trace",
    tag: "statemachined-trace",
    description:
      "The daemon's own record: one row per state the machine entered, timestamped, kept in " +
      "memory and written to disk. It is what tells you what a trial actually did, and it is " +
      "still there in the morning.",
  },
  {
    id: "monitor",
    label: "Serial monitor",
    tag: "statemachined-monitor",
    description:
      "Every line in and out of the serial port, as it went. For the moment the layers stop " +
      "agreeing and the question is what actually crossed the wire.",
  },
  {
    id: "firmware",
    label: "Firmware",
    tag: "statemachined-firmware",
    description: "What is running on the board against what this package ships.",
  },
];

const navigation = document.getElementById("view-navigation");
const container = document.getElementById("view-container");
const summary = document.getElementById("rig-summary");

function showView(viewId) {
  const view = VIEWS.find((each) => each.id === viewId) || VIEWS[0];
  // Replaced rather than hidden, so the panel that leaves the screen stops
  // polling the device: `disconnectedCallback` is where a panel gives back the
  // rig's attention, and a hidden-but-connected panel would keep it.
  const panel = document.createElement(view.tag);
  panel.setAttribute("base", BASE_URL);
  const description = document.createElement("p");
  description.className = "view-description";
  description.textContent = view.description;
  container.replaceChildren(description, panel);

  for (const button of navigation.children) {
    button.classList.toggle("current", button.dataset.viewId === view.id);
  }
  if (location.hash !== `#${view.id}`) history.replaceState(null, "", `#${view.id}`);
}

for (const view of VIEWS) {
  const button = document.createElement("button");
  button.textContent = view.label;
  button.title = view.description;
  button.dataset.viewId = view.id;
  button.addEventListener("click", () => showView(view.id));
  navigation.append(button);
}

window.addEventListener("hashchange", () => showView(location.hash.slice(1)));
showView(location.hash.slice(1) || "device");

// The header is the one thing on this page that is not a panel: which rig this
// is, and whether its board is there. It is what makes two browser tabs open on
// two rigs tellable apart, which on a bench with three of them is the whole
// difference between a useful page and a confusing one.
const api = new DaemonApiClient(BASE_URL);

async function refreshSummary() {
  try {
    const device = await api.readDevice();
    summary.textContent =
      `${location.host}  --  ${device.board ?? "no board"} on ${device.target}` +
      (device.connected ? "" : "  (not connected)");
    summary.className = device.connected ? "summary good" : "summary bad";
  } catch (error) {
    summary.textContent = `the daemon did not answer: ${error}`;
    summary.className = "summary bad";
  }
}

refreshSummary();
setInterval(refreshSummary, 5000);
