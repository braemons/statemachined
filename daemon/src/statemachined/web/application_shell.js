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

const VIEWS = [
  { id: "device", label: "Device", tag: "statemachined-device" },
  { id: "lines", label: "Lines", tag: "statemachined-lines" },
  { id: "graphs", label: "Graphs", tag: "statemachined-graph" },
  { id: "session", label: "Session", tag: "statemachined-session" },
  { id: "trace", label: "Trace", tag: "statemachined-trace" },
  { id: "firmware", label: "Firmware", tag: "statemachined-firmware" },
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
  container.replaceChildren(panel);

  for (const button of navigation.children) {
    button.classList.toggle("current", button.dataset.viewId === view.id);
  }
  if (location.hash !== `#${view.id}`) history.replaceState(null, "", `#${view.id}`);
}

for (const view of VIEWS) {
  const button = document.createElement("button");
  button.textContent = view.label;
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
