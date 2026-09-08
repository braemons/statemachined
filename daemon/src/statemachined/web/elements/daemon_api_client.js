// SPDX-License-Identifier: LGPL-3.0-or-later
//
// The one place in the UI that knows a network exists.
//
// Every element takes a `base` attribute rather than assuming same-origin,
// because the point of dev/DAEMON.md §5 is that a console served from somewhere
// else can drop `<statemachined-lines>` into its own page. An element that
// called `fetch("/api/state")` would work perfectly on the rig's own page and
// silently talk to the console's host everywhere else.

/// A refusal from the daemon, with the three fields dev/API.md §2 promises.
///
/// `context` is the useful one and it is why this is a class rather than a
/// thrown string: every refusal names what to change, and a UI that shows only
/// "409" throws that away.
export class DaemonRefusedTheRequest extends Error {
  constructor(status, body) {
    const detail = (body && body.detail) || `HTTP ${status}`;
    super(detail);
    this.name = "DaemonRefusedTheRequest";
    this.status = status;
    this.code = (body && body.error) || "http_error";
    this.detail = detail;
    this.context = (body && body.context) || "";
  }
}

export class DaemonApiClient {
  constructor(baseUrl) {
    this.baseUrl = (baseUrl || "").replace(/\/+$/, "");
  }

  urlFor(path) {
    return `${this.baseUrl}${path}`;
  }

  /// The WebSocket origin, derived from `base` and falling back to this page's.
  ///
  /// A relative `base` is the normal case on the rig's own page, and
  /// `new URL(path, location.href)` is what turns it into something `new
  /// WebSocket()` will accept -- it refuses a relative URL outright.
  webSocketUrlFor(path) {
    const absolute = new URL(this.urlFor(path), globalThis.location?.href ?? "http://localhost/");
    absolute.protocol = absolute.protocol === "https:" ? "wss:" : "ws:";
    return absolute.toString();
  }

  async request(method, path, body) {
    const response = await fetch(this.urlFor(path), {
      method,
      headers: body === undefined ? {} : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const text = await response.text();
    let parsed = null;
    try {
      parsed = text ? JSON.parse(text) : null;
    } catch {
      parsed = null;
    }
    if (!response.ok) {
      // FastAPI's own 422 shape names the field in `detail`, which is an array
      // rather than a sentence. Flatten it here so a panel has one thing to
      // show whichever half of dev/API.md §2 refused it.
      if (parsed && Array.isArray(parsed.detail)) {
        const first = parsed.detail[0] || {};
        parsed = {
          error: "validation_failed",
          detail: first.msg || "the request did not validate",
          context: (first.loc || []).join("."),
        };
      }
      throw new DaemonRefusedTheRequest(response.status, parsed);
    }
    return parsed;
  }

  get(path) {
    return this.request("GET", path);
  }

  post(path, body) {
    return this.request("POST", path, body === undefined ? {} : body);
  }

  put(path, body) {
    return this.request("PUT", path, body);
  }

  patch(path, body) {
    return this.request("PATCH", path, body);
  }

  delete(path) {
    return this.request("DELETE", path);
  }

  // ------------------------------------------------------------- the API ---
  //
  // Named after what they ask for rather than after their URLs, so that a
  // panel reads as what it wants and the route lives in exactly one place.

  readDevice() {
    return this.get("/api/device");
  }

  readDeviceLines() {
    return this.get("/api/device/lines");
  }

  replaceLineMap(lineMap) {
    return this.patch("/api/device/lines", lineMap);
  }

  connectToTheDevice() {
    return this.post("/api/device/connect");
  }

  readFirmwareVersions() {
    return this.get("/api/device/firmware");
  }

  // The board on its own. `enabled` is the stored setting and `active` is
  // whether it is driving trials right now -- a board that was greeted has the
  // first without the second, which is the rig being taken back.

  readAutorun() {
    return this.get("/api/device/autorun");
  }

  setAutorun(wanted) {
    return this.put("/api/device/autorun", wanted);
  }

  saveDeviceSettings() {
    return this.post("/api/device/save");
  }

  readState() {
    return this.get("/api/state");
  }

  // The state-machine configs: what this rig is wired like and what it can
  // run, saved under /var/lib and owned by the person rather than the package.
  // `/api/config` is the other half -- the box -- and this UI does not write it.

  listStateMachineConfigs() {
    return this.get("/api/state-machine-configs");
  }

  readStateMachineConfig(name) {
    return this.get(`/api/state-machine-configs/${encodeURIComponent(name)}`);
  }

  writeStateMachineConfig(name, config) {
    return this.put(`/api/state-machine-configs/${encodeURIComponent(name)}`, config);
  }

  deleteStateMachineConfig(name) {
    return this.delete(`/api/state-machine-configs/${encodeURIComponent(name)}`);
  }

  loadStateMachineConfig(name) {
    return this.post(`/api/state-machine-configs/${encodeURIComponent(name)}/load`);
  }

  readSession() {
    return this.get("/api/session");
  }

  openSession() {
    return this.post("/api/session/open");
  }

  closeSession() {
    return this.post("/api/session/close");
  }

  selectActiveGraph(name) {
    return this.put("/api/session/active-graph", { graph: name });
  }

  clearActiveGraph() {
    return this.delete("/api/session/active-graph");
  }

  listObservers() {
    return this.get("/api/observers");
  }

  listRecordings() {
    return this.get("/api/recordings");
  }

  startRecording(name, description) {
    return this.post("/api/recordings/start", { name: name || "", description: description || "" });
  }

  pauseRecording() {
    return this.post("/api/recordings/pause");
  }

  resumeRecording() {
    return this.post("/api/recordings/resume");
  }

  stopRecording() {
    return this.post("/api/recordings/stop");
  }

  clearRecording() {
    return this.post("/api/recordings/clear");
  }

  readRecordingEntries(name, offset, limit) {
    const query = new URLSearchParams({ offset: offset ?? 0, limit: limit ?? 500 });
    return this.get(`/api/recordings/${encodeURIComponent(name)}/entries?${query}`);
  }

  deleteRecording(name) {
    return this.delete(`/api/recordings/${encodeURIComponent(name)}`);
  }

  listStoredGraphs() {
    return this.get("/api/graphs");
  }

  readStoredGraph(name) {
    return this.get(`/api/graphs/${encodeURIComponent(name)}`);
  }

  writeStoredGraph(name, graph) {
    return this.put(`/api/graphs/${encodeURIComponent(name)}`, graph);
  }

  deleteStoredGraph(name) {
    return this.delete(`/api/graphs/${encodeURIComponent(name)}`);
  }

  validateStoredGraph(name) {
    return this.post(`/api/graphs/${encodeURIComponent(name)}/validate`);
  }

  uploadOneGraph(name) {
    return this.post(`/api/graphs/${encodeURIComponent(name)}/upload`);
  }

  uploadSessionGraphSet(graphNames) {
    return this.post("/api/session/graphs", { graph_names: graphNames });
  }

  configureTrial(request) {
    return this.post("/api/trial/configure", request);
  }

  startTrial(trialId) {
    return this.post("/api/trial/start", { trial_id: trialId });
  }

  cancelTrial(trialId) {
    return this.post("/api/trial/cancel", { trial_id: trialId });
  }

  readLastTrialResult() {
    return this.get("/api/trial/result");
  }

  readTrace(sinceEntryNumber, limit) {
    return this.get(
      `/api/trace?since_entry_number=${sinceEntryNumber | 0}&limit=${limit | 0}`,
    );
  }

  readTraceForTrial(trialId) {
    return this.get(`/api/trace/trial/${trialId | 0}`);
  }

  readConfiguration() {
    return this.get("/api/config");
  }

  replaceConfiguration(configuration) {
    return this.patch("/api/config", configuration);
  }

  openStateStream() {
    return new WebSocket(this.webSocketUrlFor("/api/stream"));
  }

  openTraceStream() {
    return new WebSocket(this.webSocketUrlFor("/api/trace/stream"));
  }

  /// The wire itself: every line in and out of the port, as it went.
  readDeviceMonitor(sinceEntryNumber, limit) {
    return this.get(
      `/api/device/monitor?since_entry_number=${sinceEntryNumber | 0}&limit=${limit | 0}`,
    );
  }

  openDeviceMonitorStream() {
    return new WebSocket(this.webSocketUrlFor("/api/device/monitor/stream"));
  }
}
