// SPDX-License-Identifier: AGPL-3.0-or-later
//! The graph-set upload, message for message, and then against the firmware.
//!
//! **The rolling checksum is the part that can be wrong silently.** It folds
//! over the protobuf of every message sent, so a checksum folded over anything
//! else still uploads, still passes every frame's CRC — and makes `set_end`
//! disagree with the device, which then refuses a set that was fine.
//!
//! Two checks, because they catch different things:
//!
//! * `tools/graph_set_cases.py` records every message Python uploaded for each
//!   compiled case. What a board decodes off a real socket has to be those
//!   messages, and `set_end` has to carry the fold of the bytes that arrived.
//! * `build/statemachined_native_device` — the firmware's own session and
//!   engine, built for this machine — has to answer `set_ok`. That is the
//!   device's word that its fold agrees, and it needs no Python to say so.
//!   Skipped, and says so, when `make integration-device` has not been run.

use std::io::{BufReader, Read, Write};
use std::net::TcpListener;
use std::thread::JoinHandle;
use std::time::Duration;

use serde::Deserialize;
use serde_json::{json, Map, Value};
use statemachined::device::graph_set_upload::send_compiled_upload_messages;
use statemachined::device::link_codec::{decode_host_message, encode_device_message};
use statemachined::device::message_framing::{crc16_ccitt, open_frame, seal_frame, CRC_INIT};
use statemachined::device::request_response_session::{
    Discard, RequestProblem, RequestResponseSession,
};
use statemachined::device::serial_link::SerialLink;
use statemachined::graph_set_compiler::{compile_graph_set_for_device, DeviceCapabilities};
use statemachined::model::graph_definition::GraphDefinition;
use statemachined::model::line_map::LineMap;
use statemachined::native_device_on_a_socket::{NativeDeviceOnASocket, NativeDeviceOptions};

const TIMEOUT: Duration = Duration::from_secs(5);

#[derive(Debug, Deserialize)]
struct Case {
    why: String,
    graphs: Vec<GraphDefinition>,
    line_map: LineMap,
    hello_ack: Value,
    set_version: i64,
    answer: Answer,
}

#[derive(Debug, Deserialize)]
struct Answer {
    compiled: Option<Compiled>,
}

#[derive(Debug, Deserialize)]
struct Compiled {
    upload_messages: Vec<PythonMessage>,
}

#[derive(Debug, Deserialize)]
struct PythonMessage {
    msg_type: String,
    fields: Map<String, Value>,
}

fn compiled_cases() -> Vec<(Case, Vec<PythonMessage>)> {
    let cases: Vec<Case> = serde_json::from_str(include_str!("graph_set_cases.json"))
        .expect("the cases tools/graph_set_cases.py writes");
    cases
        .into_iter()
        .filter_map(|mut case| {
            let messages = case.answer.compiled.take()?.upload_messages;
            Some((case, messages))
        })
        .collect()
}

/// A command frame off the socket, as its protobuf and as the message it is.
fn next_command(reader: &mut impl Read) -> Option<(Vec<u8>, Value)> {
    let mut stuffed = Vec::new();
    let mut byte = [0u8];
    loop {
        if reader.read(&mut byte).ok()? == 0 {
            return None;
        }
        if byte[0] == 0 {
            if stuffed.is_empty() {
                continue;
            }
            break;
        }
        stuffed.push(byte[0]);
    }
    let payload = open_frame(&stuffed).expect("a frame the device could read");
    let command = decode_host_message(&payload).expect("a command the device could read");
    Some((payload, command))
}

/// A reply, framed the way the device frames one.
fn reply(writer: &mut impl Write, msg_type: &str, answering: &Value, fields: Value) {
    let in_reply_to = answering["message_id"].as_u64().unwrap() as u16;
    let payload = encode_device_message(
        msg_type,
        900,
        Some(in_reply_to),
        fields.as_object().unwrap(),
    )
    .expect("a DeviceMessage");
    writer.write_all(&seal_frame(&payload)).unwrap();
}

fn session_on(address: &str) -> RequestResponseSession<Discard> {
    let link = SerialLink::open(address, 115_200, Duration::from_secs(2)).expect("the link opens");
    RequestResponseSession::new(link, Discard)
}

/// A board that says yes to everything and keeps every command it was sent,
/// with the checksum it folded over them by itself.
///
/// `set_ok` for `set_end`, `ack` for the rest, each naming the command it
/// answers. Whether the set is any good is the firmware's question, below.
fn an_agreeable_board() -> (String, JoinHandle<(Vec<Value>, u16)>) {
    let listener = TcpListener::bind("127.0.0.1:0").expect("a port");
    let address = listener.local_addr().unwrap().to_string();
    let board = std::thread::spawn(move || {
        let (stream, _) = listener.accept().expect("the link connects");
        let mut writer = stream.try_clone().unwrap();
        let mut reader = BufReader::new(stream);
        let mut received = Vec::new();
        let mut folded = CRC_INIT;
        while let Some((payload, command)) = next_command(&mut reader) {
            if command["msg_type"] == "set_end" {
                reply(&mut writer, "set_ok", &command, json!({}));
            } else {
                folded = crc16_ccitt(&payload, folded);
                reply(&mut writer, "ack", &command, json!({}));
            }
            received.push(command);
        }
        (received, folded)
    });
    (address, board)
}

/// Where a decoded command differs from the message Python uploaded, if it
/// does. A `null` Python sent is an absent field now; a field Python left out
/// may come back at its zero, since a plain protobuf field always does.
fn difference(mine: &Value, python: &PythonMessage) -> Option<String> {
    if mine["msg_type"] != python.msg_type.as_str() {
        return Some(format!(
            "{} where python sent {}",
            mine["msg_type"], python.msg_type
        ));
    }
    for (key, theirs) in &python.fields {
        let ours = mine.get(key);
        let agrees = match (theirs, ours) {
            (Value::Null, None) => true,
            (theirs, Some(ours)) => theirs == ours,
            _ => false,
        };
        if !agrees {
            return Some(format!("{key}: rust {ours:?}, python {theirs}"));
        }
    }
    None
}

#[test]
fn every_message_the_board_decodes_is_the_one_python_uploaded() {
    let cases = compiled_cases();
    assert!(cases.len() > 15, "only {} compiled cases", cases.len());
    let mut disagreed = Vec::new();
    for (case, theirs) in cases {
        let compiled = compile_graph_set_for_device(
            &case.graphs,
            &case.line_map,
            &DeviceCapabilities::from_hello_ack(&case.hello_ack),
            case.set_version,
        )
        .expect("python compiled it");
        let (address, board) = an_agreeable_board();
        let mut session = session_on(&address);
        let reply = send_compiled_upload_messages(&mut session, &compiled.upload_messages, TIMEOUT)
            .expect("an agreeable board agrees");
        assert_eq!(reply["msg_type"], "set_ok", "{}", case.why);
        drop(session);
        let (ours, folded) = board.join().unwrap();
        if ours.len() != theirs.len() {
            disagreed.push(format!(
                "{}: {} messages, python sent {}",
                case.why,
                ours.len(),
                theirs.len()
            ));
            continue;
        }
        if let Some((position, why)) =
            ours.iter()
                .zip(&theirs)
                .enumerate()
                .find_map(|(position, (mine, python))| {
                    difference(mine, python).map(|why| (position, why))
                })
        {
            disagreed.push(format!("{}: message {position}: {why}", case.why));
            continue;
        }
        // The checksum set_end carries is the fold of what the board actually
        // received -- computed here from the bytes off the socket, not taken
        // from the daemon's word for it.
        let claimed = ours.last().and_then(|end| end["checksum"].as_u64());
        if claimed != Some(folded as u64) {
            disagreed.push(format!(
                "{}: set_end carries {claimed:?}, the bytes that arrived fold to {folded}",
                case.why
            ));
        }
    }
    assert!(
        disagreed.is_empty(),
        "{} cases disagree:\n  {}",
        disagreed.len(),
        disagreed.join("\n  ")
    );
}

#[test]
fn a_refusal_mid_upload_stops_it_and_names_the_message() {
    // The device refuses the first graph_state. Nothing after it is sent: a
    // set the device has begun refusing is not one to keep talking at.
    let (case, _) = compiled_cases().remove(0);
    let compiled = compile_graph_set_for_device(
        &case.graphs,
        &case.line_map,
        &DeviceCapabilities::from_hello_ack(&case.hello_ack),
        case.set_version,
    )
    .unwrap();
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let address = listener.local_addr().unwrap().to_string();
    let board = std::thread::spawn(move || {
        let (stream, _) = listener.accept().unwrap();
        let mut writer = stream.try_clone().unwrap();
        let mut reader = BufReader::new(stream);
        let mut seen = 0;
        while let Some((_, command)) = next_command(&mut reader) {
            seen += 1;
            if command["msg_type"] == "graph_state" {
                reply(
                    &mut writer,
                    "error",
                    &command,
                    json!({"code": "bad_state", "context": "i"}),
                );
            } else {
                reply(&mut writer, "ack", &command, json!({}));
            }
        }
        seen
    });
    let mut session = session_on(&address);
    let problem = send_compiled_upload_messages(&mut session, &compiled.upload_messages, TIMEOUT)
        .expect_err("the board refused");
    assert!(matches!(problem, RequestProblem::Refused(_)), "{problem:?}");
    drop(session);
    let sent = board.join().unwrap();
    let first_state = compiled
        .upload_messages
        .iter()
        .position(|message| message.msg_type.as_str() == "graph_state")
        .unwrap();
    assert_eq!(sent, first_state + 1, "kept sending after the refusal");
}

// -- the firmware -------------------------------------------------------------

#[test]
fn the_firmware_commits_every_set_python_would_have_uploaded() {
    let device = match NativeDeviceOnASocket::start(0, &NativeDeviceOptions::default()) {
        Ok(device) => device,
        Err(problem) => {
            eprintln!("skipped: {problem}");
            return;
        }
    };
    let mut session = session_on(&device.target());
    let hello_ack = session.hello(None, TIMEOUT).expect("the device greets");
    let capabilities = DeviceCapabilities::from_hello_ack(&hello_ack);

    let mut uploaded = 0;
    for (version, (case, _)) in compiled_cases().into_iter().enumerate() {
        // Against *this* board's caps, not the reference board the cases were
        // written for: a set that does not fit here is not what is being
        // tested, and is skipped rather than miscounted as a pass.
        let Ok(compiled) = compile_graph_set_for_device(
            &case.graphs,
            &case.line_map,
            &capabilities,
            version as i64 + 1,
        ) else {
            eprintln!("does not fit the native device, not uploaded: {}", case.why);
            continue;
        };
        let reply = send_compiled_upload_messages(&mut session, &compiled.upload_messages, TIMEOUT)
            .unwrap_or_else(|problem| panic!("{}: {problem:?}", case.why));
        assert_eq!(reply["msg_type"], "set_ok", "{}: {reply}", case.why);
        uploaded += 1;
    }
    assert!(uploaded > 10, "only {uploaded} sets reached the firmware");
}
