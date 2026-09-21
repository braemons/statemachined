// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The one place in this UI that knows a network exists.
//
// It speaks the **Connect protocol** to the eight services in
// `proto/statemachined/v1/`, over the same port the panels themselves are
// served from — `daemon/src/statemachined/daemon/api/web_edge.py` answers it
// in the daemon, so there is no proxy to deploy and nothing to configure.
//
// Connect rather than gRPC-Web, which is what mousewheeld uses: that daemon is
// Rust and `tonic-web` translates in process, while this one is Python and the
// edge is written against the protocol specification. Connect's unary call is
// a plain HTTP POST with the bare message in the body, which is a great deal
// less to own — and the browser cannot tell the difference, because both are
// `createClient` over a transport.
//
// Every element takes a `base` attribute rather than assuming same-origin,
// because the point of the `/elements/` contract is that a console served from
// somewhere else drops `<statemachined-device>` into its own page. An element
// that called the daemon at its own origin would work perfectly on the rig's
// own page and silently talk to the console's host everywhere else.
//
// **A panel never sees a protobuf message.** This is the browser's half of the
// convert seam, and it is the same rule the daemon keeps in `api/convert/`:
// the generated types stop here. What crosses is protobuf's JSON mapping —
// `fromJson` on the way out, `toJson` on the way back — which is a plain object
// with the field names the proto spells, and which refuses an unknown field in
// a request by name, in the browser, before it reaches the wire.
//
// Those names are the proto's own: every field pins `json_name` to its
// snake_case spelling, so `newest_trace_entry_number` is that in the proto, on
// the wire, in the file on disk and here. That is the family's rule about a
// name travelling unchanged, applied to JSON.
//
// Methods are named after what they ask for rather than after their rpcs, so a
// panel reads as what it wants.

import { createClient, ConnectError, Code } from "@connectrpc/connect";
import { createConnectTransport } from "@connectrpc/connect-web";
import { fromJson, toJson } from "@bufbuild/protobuf";

import {
  Configuration as ConfigurationService,
  Device as DeviceService,
  GraphStore as GraphStoreService,
  Recording as RecordingService,
  Session as SessionService,
  State as StateService,
  StateMachineConfigStore as StateMachineConfigStoreService,
  Trial as TrialService,
} from "../gen/statemachined/v1/service_pb.js";
import { ErrorSchema } from "../gen/statemachined/v1/common_pb.js";

export { TrialOutcomeSchema } from "../gen/braemons/v1/trial_outcome_pb.js";

/// One enum value, as a word a person reads.
///
/// `TRIAL_CANCEL_REASON_LINK_LOST` is the right name on the wire and the wrong
/// thing to show somebody at a rig. protobuf's style prefixes every value with
/// its enum's name so that two enums can both have a `NONE`; the descriptor
/// knows that prefix, so stripping it is a fact about the schema rather than a
/// string trim somebody has to keep matching.
///
/// Unknown values come back as themselves. A daemon newer than the page it is
/// serving is a real situation, and showing the raw name says more than
/// "unknown" does.
export function wordFor(enumSchema, value) {
  const found = enumSchema.values.find((each) => each.name === value || each.localName === value);
  if (found === undefined) return value ?? "";
  return found.localName.toLowerCase().replace(/_/g, " ");
}

/// A refusal from the daemon: the status, the machine-readable code, the
/// sentence, and the context that names what to change.
///
/// `context` is why this is a class rather than a thrown string. A graph set
/// that does not fit names what overflowed; a line map this board refuses names
/// the pin. A UI that shows only `invalid_argument` throws exactly that away.
///
/// The three fields come from `statemachined.v1.Error` in the error's details,
/// not from parsing the message: a client that reads a sentence to find out
/// which refusal it was is a client that breaks when the sentence is reworded.
///
/// **A device's refusal keeps the device's own words.** When the board says
/// `graph_mismatch` or `busy`, `error` is that code — those are the words its
/// documentation uses.
export class DaemonRefusedTheRequest extends Error {
  constructor(status, body) {
    super(body.detail || status);
    this.name = "DaemonRefusedTheRequest";
    this.status = status;
    this.code = body.error || "rpc_failed";
    this.detail = body.detail || status;
    this.context = body.context || "";
  }

  /// Whatever the transport threw, as a refusal.
  ///
  /// A daemon that is not running, a CORS rejection and a cancelled stream all
  /// arrive here too. They have no `statemachined.v1.Error` — nothing refused
  /// anything, the call never landed — so the code is the Connect one and the
  /// detail is what the browser said.
  static from(thrown) {
    if (thrown instanceof DaemonRefusedTheRequest) return thrown;
    const failure = ConnectError.from(thrown);
    const status = (Code[failure.code] ?? "Unknown")
      .replace(/(?<=[a-z])(?=[A-Z])/g, "_")
      .toLowerCase();
    return new DaemonRefusedTheRequest(status, refusalIn(failure) ?? { detail: failure.rawMessage });
  }

  /// Whether retrying the identical request could work.
  ///
  /// Only `unavailable`, which here means **no board attached**. Everything
  /// else is a request to change something, and a panel that retried them
  /// would hammer a rig about a typo.
  get retryable() {
    return this.status === "unavailable";
  }
}

/// `statemachined.v1.Error` out of the error's details, or `null`.
function refusalIn(failure) {
  const [refusal] = failure.findDetails(ErrorSchema);
  if (refusal === undefined) return null;
  return toJson(ErrorSchema, refusal, { alwaysEmitImplicit: true });
}

export class DaemonApiClient {
  constructor(baseUrl) {
    this.baseUrl = (baseUrl || "").replace(/\/+$/, "");
    const transport = createConnectTransport({ baseUrl: this.baseUrl || "/" });
    this.state = createClient(StateService, transport);
    this.trial = createClient(TrialService, transport);
    this.device = createClient(DeviceService, transport);
    this.graphs = createClient(GraphStoreService, transport);
    this.configs = createClient(StateMachineConfigStoreService, transport);
    this.session = createClient(SessionService, transport);
    this.recording = createClient(RecordingService, transport);
    this.configuration = createClient(ConfigurationService, transport);
  }

  /// One unary call: JSON in, JSON out, refusals as `DaemonRefusedTheRequest`.
  ///
  /// The descriptors come off the service rather than being named again here,
  /// so a request or response type that changes in the proto changes here by
  /// itself and cannot be half-updated.
  async call(client, method, request = {}) {
    try {
      const answer = await client[method.localName](fromJson(method.input, request));
      return toJson(method.output, answer, { alwaysEmitImplicit: true });
    } catch (failure) {
      throw DaemonRefusedTheRequest.from(failure);
    }
  }

  /// One server-streaming call, as an async iterable of JSON frames.
  ///
  /// `signal` is how a panel stops it — a stream with no way to end it is a
  /// stream that outlives the panel that opened it. `onHeader` is the moment
  /// the daemon accepted the call, which is what a "streaming" pill means: a
  /// rig between trials produces no frames and is perfectly healthy.
  async *follow(client, method, request, { signal, onHeader } = {}) {
    try {
      const frames = client[method.localName](fromJson(method.input, request), { signal, onHeader });
      for await (const frame of frames) {
        yield toJson(method.output, frame, { alwaysEmitImplicit: true });
      }
    } catch (failure) {
      if (signal?.aborted) return; // our own close, not a failure
      throw DaemonRefusedTheRequest.from(failure);
    }
  }

  // ------------------------------------------------------ state & streams ---

  readState() {
    return this.call(this.state, StateService.method.readState);
  }

  followState(options) {
    return this.follow(this.state, StateService.method.watchState, {}, options);
  }

  readTrace(sinceEntryNumber, limit) {
    return this.call(this.state, StateService.method.readTrace, {
      since_entry_number: sinceEntryNumber ?? 0,
      limit: limit ?? 500,
    });
  }

  followTrace(sinceEntryNumber, options) {
    return this.follow(
      this.state,
      StateService.method.watchTrace,
      { since_entry_number: sinceEntryNumber ?? 0 },
      options,
    );
  }

  readTraceForTrial(trialId) {
    return this.call(this.state, StateService.method.readTrialTrace, { trial_id: trialId });
  }

  readObservers() {
    return this.call(this.state, StateService.method.readObservers);
  }

  // ------------------------------------------------------- the trial loop ---

  configureTrial(request) {
    return this.call(this.trial, TrialService.method.configure, request);
  }

  startTrial(trialId) {
    return this.call(this.trial, TrialService.method.start, { trial_id: trialId });
  }

  cancelTrial(trialId) {
    return this.call(this.trial, TrialService.method.cancel, { trial_id: trialId });
  }

  readLastTrialResult() {
    return this.call(this.trial, TrialService.method.readResult);
  }

  // -------------------------------------------------------------- device ---

  readDevice() {
    return this.call(this.device, DeviceService.method.readDevice);
  }

  connectToTheDevice() {
    return this.call(this.device, DeviceService.method.openLink);
  }

  readDeviceLines() {
    return this.call(this.device, DeviceService.method.readLines);
  }

  /// Replace the line map, as the **file** it is.
  ///
  /// A line map is a document, so what crosses is its text and the model that
  /// owns it decides whether it is one. This client must never convert between
  /// the two — that would be a second description of a line map, in a browser,
  /// disagreeing with the daemon's the first time either changes.
  writeLineMapFile(text) {
    return this.call(this.device, DeviceService.method.writeLineMapFile, { text });
  }

  readSerialMonitor(sinceEntryNumber, limit) {
    return this.call(this.device, DeviceService.method.readSerialMonitor, {
      since_entry_number: sinceEntryNumber ?? 0,
      limit: limit ?? 500,
    });
  }

  followSerialMonitor(sinceEntryNumber, options) {
    return this.follow(
      this.device,
      DeviceService.method.watchSerialMonitor,
      { since_entry_number: sinceEntryNumber ?? 0 },
      options,
    );
  }

  readFirmwareVersions() {
    return this.call(this.device, DeviceService.method.readFirmware);
  }

  readAutorun() {
    return this.call(this.device, DeviceService.method.readAutorun);
  }

  setAutorun(wanted) {
    return this.call(this.device, DeviceService.method.writeAutorun, wanted);
  }

  saveDeviceSettings() {
    return this.call(this.device, DeviceService.method.saveSettings);
  }

  // --------------------------------------------------------------- graphs ---

  listStoredGraphs() {
    return this.call(this.graphs, GraphStoreService.method.listGraphs);
  }

  readStoredGraphFile(name) {
    return this.call(this.graphs, GraphStoreService.method.readGraphFile, { name });
  }

  writeStoredGraphFile(name, text) {
    return this.call(this.graphs, GraphStoreService.method.writeGraphFile, { name, text });
  }

  deleteStoredGraph(name) {
    return this.call(this.graphs, GraphStoreService.method.deleteGraph, { name });
  }

  validateStoredGraph(name) {
    return this.call(this.graphs, GraphStoreService.method.validateGraph, { name });
  }

  /// Check a **draft**: what somebody is typing, before it is saved.
  validateGraphFile(text) {
    return this.call(this.graphs, GraphStoreService.method.validateGraphFile, { text });
  }

  uploadOneGraph(name) {
    return this.call(this.graphs, GraphStoreService.method.uploadGraph, { name });
  }

  // ------------------------------------------- state-machine config store ---

  listStateMachineConfigs() {
    return this.call(this.configs, StateMachineConfigStoreService.method.listConfigs);
  }

  readStateMachineConfigFile(name) {
    return this.call(this.configs, StateMachineConfigStoreService.method.readConfigFile, { name });
  }

  writeStateMachineConfigFile(name, text) {
    return this.call(this.configs, StateMachineConfigStoreService.method.writeConfigFile, {
      name,
      text,
    });
  }

  deleteStateMachineConfig(name) {
    return this.call(this.configs, StateMachineConfigStoreService.method.deleteConfig, { name });
  }

  loadStateMachineConfig(name) {
    return this.call(this.configs, StateMachineConfigStoreService.method.loadConfig, { name });
  }

  // -------------------------------------------------------------- session ---

  readSession() {
    return this.call(this.session, SessionService.method.readSession);
  }

  openSession() {
    return this.call(this.session, SessionService.method.open);
  }

  uploadSessionGraphSet(graphNames) {
    return this.call(this.session, SessionService.method.uploadGraphs, {
      graph_names: graphNames,
    });
  }

  closeSession() {
    return this.call(this.session, SessionService.method.close);
  }

  selectActiveGraph(name) {
    return this.call(this.session, SessionService.method.setActiveGraph, { graph: name });
  }

  clearActiveGraph() {
    return this.call(this.session, SessionService.method.clearActiveGraph);
  }

  // ----------------------------------------------------------- recordings ---

  listRecordings() {
    return this.call(this.recording, RecordingService.method.readRecordings);
  }

  startRecording(name, description) {
    return this.call(this.recording, RecordingService.method.start, {
      name: name ?? "",
      description: description ?? "",
    });
  }

  pauseRecording() {
    return this.call(this.recording, RecordingService.method.pause);
  }

  resumeRecording() {
    return this.call(this.recording, RecordingService.method.resume);
  }

  stopRecording() {
    return this.call(this.recording, RecordingService.method.stop);
  }

  clearRecording() {
    return this.call(this.recording, RecordingService.method.clear);
  }

  readRecording(name) {
    return this.call(this.recording, RecordingService.method.readRecording, { name });
  }

  readRecordingEntries(name, offset, limit) {
    return this.call(this.recording, RecordingService.method.readEntries, {
      name,
      offset: offset ?? 0,
      limit: limit ?? 500,
    });
  }

  deleteRecording(name) {
    return this.call(this.recording, RecordingService.method.deleteRecording, { name });
  }

  // ------------------------------------------------- the box's own settings ---

  readConfiguration() {
    return this.call(this.configuration, ConfigurationService.method.readConfiguration);
  }

  replaceConfiguration(changes) {
    return this.call(this.configuration, ConfigurationService.method.patchConfiguration, changes);
  }

  readHealth() {
    return this.call(this.configuration, ConfigurationService.method.readHealth);
  }
}
