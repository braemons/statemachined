// SPDX-License-Identifier: AGPL-3.0-or-later
//! The graph-set upload, byte for byte, and then against the firmware itself.
//!
//! **The rolling checksum is the part that can be wrong silently.** It folds
//! over the CRC-covered bytes of every line sent, so a line built one member
//! out of order still uploads, still carries a valid CRC — and makes `set_end`
//! disagree with the device, which then refuses a set that was fine.
//!
//! Two checks, because they catch different things:
//!
//! * `tools/graph_set_cases.py` records every line Python puts on the wire for
//!   each compiled case. Rust has to put the same bytes on a real socket.
//! * `build/statemachined_native_device` — the firmware's own session and
//!   engine, built for this machine — has to answer `set_ok`. That is the
//!   device's word that its fold agrees, and it needs no Python to say so.
//!   Skipped, and says so, when `make integration-device` has not been run.

use std::io::{BufRead, BufReader, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::thread::JoinHandle;
use std::time::Duration;

use serde::Deserialize;
use serde_json::Value;
use statemachined::device::graph_set_upload::send_compiled_upload_messages;
use statemachined::device::message_framing::{parse_reply, statemachined_line};
use statemachined::device::request_response_session::{
    Discard, RequestProblem, RequestResponseSession,
};
use statemachined::device::serial_link::SerialLink;
use statemachined::graph_set_compiler::{compile_graph_set_for_device, DeviceCapabilities};
use statemachined::model::graph_definition::GraphDefinition;
use statemachined::model::line_map::LineMap;

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
    framed_lines: Vec<String>,
}

fn compiled_cases() -> Vec<(Case, Vec<String>)> {
    let cases: Vec<Case> = serde_json::from_str(include_str!("graph_set_cases.json"))
        .expect("the cases tools/graph_set_cases.py writes");
    cases
        .into_iter()
        .filter_map(|mut case| {
            let lines = case.answer.compiled.take()?.framed_lines;
            Some((case, lines))
        })
        .collect()
}

fn session_on(address: &str) -> RequestResponseSession<Discard> {
    let link = SerialLink::open(address, 115_200, Duration::from_secs(2)).expect("the link opens");
    RequestResponseSession::new(link, Discard)
}

/// A board that says yes to everything and keeps every line it was sent.
///
/// `set_ok` for `set_end`, `ack` for the rest, each naming the command it
/// answers. Whether the set is any good is the firmware's question, below.
fn an_agreeable_board() -> (String, JoinHandle<Vec<String>>) {
    let listener = TcpListener::bind("127.0.0.1:0").expect("a port");
    let address = listener.local_addr().unwrap().to_string();
    let board = std::thread::spawn(move || {
        let (stream, _) = listener.accept().expect("the link connects");
        let mut writer = stream.try_clone().unwrap();
        let mut received = Vec::new();
        for line in BufReader::new(stream).lines() {
            let Ok(line) = line else { break };
            let command = parse_reply(&line).expect("a command the device could read");
            let reply = if command["msg_type"] == "set_end" {
                "set_ok"
            } else {
                "ack"
            };
            let body = format!(
                // Without its closing brace: the framing puts the crc before it.
                "{{\"msg_type\":\"{reply}\",\"in_reply_to\":{}",
                command["message_id"]
            );
            writer
                .write_all(format!("{}\n", statemachined_line(&body)).as_bytes())
                .unwrap();
            received.push(line);
        }
        received
    });
    (address, board)
}

#[test]
fn every_line_is_the_line_python_sent() {
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
        let ours = board.join().unwrap();
        if let Some((position, (mine, python))) = ours
            .iter()
            .zip(&theirs)
            .enumerate()
            .find(|(_, (mine, python))| mine != python)
        {
            disagreed.push(format!(
                "{}: line {position}\n    rust:   {mine}\n    python: {python}",
                case.why
            ));
        } else if ours.len() != theirs.len() {
            disagreed.push(format!(
                "{}: {} lines, python sent {}",
                case.why,
                ours.len(),
                theirs.len()
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
        let mut seen = 0;
        for line in BufReader::new(stream).lines() {
            let Ok(line) = line else { break };
            seen += 1;
            let command = parse_reply(&line).unwrap();
            let body = if command["msg_type"] == "graph_state" {
                format!(
                    "{{\"msg_type\":\"error\",\"in_reply_to\":{},\"code\":\"bad_state\",\"context\":\"i\"",
                    command["message_id"]
                )
            } else {
                format!(
                    "{{\"msg_type\":\"ack\",\"in_reply_to\":{}",
                    command["message_id"]
                )
            };
            writer
                .write_all(format!("{}\n", statemachined_line(&body)).as_bytes())
                .unwrap();
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

/// The native device on a socket, killed when dropped.
///
/// It takes its link on stdin and stdout; this pumps a listening socket to
/// them, as `native_device_on_a_socket.py` does for the Python daemon.
struct NativeDevice {
    child: Child,
    address: String,
}

impl NativeDevice {
    fn start() -> Option<Self> {
        let binary =
            PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../build/statemachined_native_device");
        if !binary.exists() {
            eprintln!(
                "skipped: {} is not built; `make integration-device` builds it",
                binary.display()
            );
            return None;
        }
        let mut child = Command::new(&binary)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .expect("the native device starts");
        let mut to_device = child.stdin.take().unwrap();
        let mut from_device = child.stdout.take().unwrap();
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap().to_string();
        std::thread::spawn(move || {
            let (stream, _) = listener.accept().unwrap();
            let mut inbound: TcpStream = stream.try_clone().unwrap();
            let mut outbound = stream;
            // A plain loop rather than `io::copy`, which on Linux may splice a
            // socket into a pipe in the kernel and hold bytes the device is
            // waiting for.
            std::thread::spawn(move || {
                let mut buffer = [0u8; 4096];
                while let Ok(read) = inbound.read(&mut buffer) {
                    if read == 0
                        || to_device.write_all(&buffer[..read]).is_err()
                        || to_device.flush().is_err()
                    {
                        break;
                    }
                }
            });
            let mut buffer = [0u8; 4096];
            while let Ok(read) = from_device.read(&mut buffer) {
                if read == 0 || outbound.write_all(&buffer[..read]).is_err() {
                    break;
                }
            }
        });
        Some(Self { child, address })
    }
}

impl Drop for NativeDevice {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

#[test]
fn the_firmware_commits_every_set_python_would_have_uploaded() {
    let Some(device) = NativeDevice::start() else {
        return;
    };
    let mut session = session_on(&device.address);
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
