// SPDX-License-Identifier: LGPL-3.0-or-later
//
// The `/elements/` contract. dev/DAEMON.md §5.
//
//     <script type="module" src="http://rig.local:8081/elements/statemachined.js"></script>
//     <statemachined-device  base="http://rig.local:8081"></statemachined-device>
//     <statemachined-lines   base="http://rig.local:8081"></statemachined-lines>
//     <statemachined-graph   base="http://rig.local:8081" name="go-nogo"></statemachined-graph>
//     <statemachined-session base="http://rig.local:8081"></statemachined-session>
//     <statemachined-trace   base="http://rig.local:8081" trial="193"></statemachined-trace>
//     <statemachined-firmware base="http://rig.local:8081"></statemachined-firmware>
//     <statemachined-monitor base="http://rig.local:8081"></statemachined-monitor>
//
// **This URL and these seven tag names are what the console repo depends on.**
// The console (`braemons-console`) is a static shell with no domain logic:
// every panel it shows is one of these, served by the daemon that owns the
// device it is about, which is what keeps a console from bundling a copy of
// this UI and drifting the first time a field changes.
//
// Three properties this shape buys, each of them the point:
//
//   * Every panel has a **shadow root**, so triald's ~600 lines of global
//     selectors cannot reach into one dropped onto its page -- natively, with
//     no tooling.
//   * Every attribute is a **string**, so React 18's attribute-only custom
//     element support is enough. vstimd is React + Vite; nobody has to change
//     build systems for a shared framework nobody would agree on.
//   * `base` is an attribute rather than an assumption, because a console is
//     not served from the rig.
//
// Importing this module registers all seven. Importing one panel's module
// directly registers only that one, and is also supported -- a console that
// wants the trace and nothing else should not pay for the graph editor.

export { DaemonApiClient, DaemonRefusedTheRequest } from "./daemon_api_client.js";
export { BasePanelElement } from "./base_panel_element.js";
export { DevicePanelElement } from "./device_panel_element.js";
export { LineMapPanelElement } from "./line_map_panel_element.js";
export { GraphStorePanelElement } from "./graph_store_panel_element.js";
export { SessionPanelElement } from "./session_panel_element.js";
export { TracePanelElement } from "./trace_panel_element.js";
export { FirmwarePanelElement } from "./firmware_panel_element.js";
export { SerialMonitorPanelElement } from "./serial_monitor_panel_element.js";
export { renderGraphNodeDiagram } from "./graph_node_diagram.js";
// Pure, and exported because a console showing a graph should be able to say
// what a predicate means in the same words this UI does -- and to spot the two
// ways a predicate says something other than what its checkboxes look like.
export { describePredicate, predicateProblems } from "./transition_predicate.js";

/// The tag names, so a console can iterate them rather than hard-code a list
/// that goes stale when a panel is added.
export const STATEMACHINED_ELEMENT_NAMES = [
  "statemachined-device",
  "statemachined-lines",
  "statemachined-graph",
  "statemachined-session",
  "statemachined-trace",
  "statemachined-firmware",
  "statemachined-monitor",
];
