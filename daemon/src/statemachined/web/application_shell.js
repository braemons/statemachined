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
// top of every panel in it. "Session", "trace" and "recording" are words this
// system uses in a particular way -- a session is the set of paradigms loaded on
// the board, the trace is the daemon's always-on record of what the machine did,
// a recording is a named piece of that trace kept in its own file -- and a
// person opening this page for the first time has no way to know that from a tab
// label. The panels are used inside a console too, where there are no tabs at
// all, so each one carries its own description rather than relying on this.
//
// **A view is a question, not a panel.** There were eight tabs and they cut the
// same material twice: a config *is* a line map plus paradigms, so editing one
// in "Lines" and then loading the file in "Configs" was two tabs for one
// thought. And "Session", "Trace" and "Serial monitor" are three views of the
// single question *what is this rig doing right now* -- what it is running, what
// it recorded, and what actually crossed the wire when those two disagree.
//
// So there are three views, and each is a question somebody actually arrives
// with: what is on the end of the cable, what is this rig set up to do, and
// what is it doing. Nothing was removed -- every panel below is the same custom
// element, unchanged, and a console that embeds one of them individually is
// unaffected. What changed is only how this page groups them.
const VIEWS = [
  {
    id: "device",
    label: "Device",
    tags: ["statemachined-device", "statemachined-firmware"],
    description:
      "The board on the other end of the cable: what it is, whether it is well, and what " +
      "firmware it is running against what this package ships.",
  },
  {
    id: "setup",
    label: "Setup",
    tags: ["statemachined-configs", "statemachined-lines", "statemachined-graph"],
    description:
      "What this rig is set up to do, in one place: the saved config that binds a line map " +
      "and a set of paradigms together, the wiring that config names, and the paradigms " +
      "themselves. The config on top is the file; the two panels under it are what is in it. " +
      "Edit either one and save it back from the config panel -- until you do, the change is " +
      "on the board and in memory and a restart loses it.",
  },
  {
    id: "run",
    label: "Run",
    tags: [
      "statemachined-session",
      "statemachined-recording",
      "statemachined-trace",
      "statemachined-monitor",
    ],
    description:
      "What this rig is doing, and what it did. Open a session to put the paradigms on the " +
      "board, arm a trial by hand or let triald drive it, keep a named recording of what " +
      "happens, and read back the trace underneath. The serial monitor at the bottom is for " +
      "the moment the layers stop agreeing and the question is what actually crossed the wire.",
  },
];

const navigation = document.getElementById("view-navigation");
const container = document.getElementById("view-container");
const summary = document.getElementById("rig-summary");

function showView(viewId) {
  const view = VIEWS.find((each) => each.id === viewId) || VIEWS[0];
  // Replaced rather than hidden, so the panels that leave the screen stop
  // polling the device: `disconnectedCallback` is where a panel gives back the
  // rig's attention, and a hidden-but-connected one would keep it. That matters
  // more now that a view holds several: four panels left connected behind a
  // tab would be four pollers against a daemon holding one serial port.
  const panels = view.tags.map((tag) => {
    const panel = document.createElement(tag);
    panel.setAttribute("base", BASE_URL);
    return panel;
  });
  const description = document.createElement("p");
  description.className = "view-description";
  description.textContent = view.description;
  container.replaceChildren(description, ...panels);

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
