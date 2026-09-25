// SPDX-License-Identifier: AGPL-3.0-or-later
//! The request/response session, against a board that answers out of order.
//!
//! **What can be wrong here is the routing**, not the happy path. The protocol
//! lets `event`, `log` and `result_*` arrive between a command and its reply, so
//! a session that matched on arrival order would take an event for an answer.
//! These drive a fake board over a real socket and make it interleave.

use std::io::{BufReader, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde_json::{json, Value};
use statemachined::device::link_codec::{decode_host_message, encode_device_message};
use statemachined::device::message_framing::{open_frame, seal_frame};
use statemachined::device::message_vocabulary::MsgType;
use statemachined::device::request_response_session::{
    RequestProblem, RequestResponseSession, Sink,
};
use statemachined::device::serial_link::SerialLink;

/// What the session set aside, so a test can say where a message went.
#[derive(Default, Clone)]
struct Recorded {
    unsolicited: Arc<Mutex<Vec<Value>>>,
    junk: Arc<Mutex<Vec<(String, String)>>>,
}

impl Sink for Recorded {
    fn unsolicited(&mut self, message: &Value, _payload: &[u8]) {
        self.unsolicited.lock().unwrap().push(message.clone());
    }
    fn junk(&mut self, line: &str, why: &str) {
        self.junk
            .lock()
            .unwrap()
            .push((line.to_string(), why.to_string()));
    }
}

/// A session, and a handle to the fake board on the other end.
fn session_with_a_board() -> (RequestResponseSession<Recorded>, Board, Recorded) {
    let listener = TcpListener::bind("127.0.0.1:0").expect("a port");
    let address = listener.local_addr().unwrap().to_string();
    let opening = std::thread::spawn(move || {
        SerialLink::open(&address, 115_200, Duration::from_secs(2)).expect("the link opens")
    });
    let (stream, _) = listener.accept().expect("the link connects");
    let recorded = Recorded::default();
    (
        RequestResponseSession::new(opening.join().unwrap(), recorded.clone()),
        Board::new(stream),
        recorded,
    )
}

/// The far end: reads whole command frames, writes whatever it is told.
struct Board {
    reader: BufReader<TcpStream>,
    writer: TcpStream,
}

impl Board {
    fn new(stream: TcpStream) -> Self {
        Self {
            reader: BufReader::new(stream.try_clone().unwrap()),
            writer: stream,
        }
    }

    /// The next command, decoded.
    fn next_command(&mut self) -> Value {
        let mut stuffed = Vec::new();
        let mut byte = [0u8];
        loop {
            self.reader.read_exact(&mut byte).expect("a command");
            if byte[0] == 0 {
                break;
            }
            stuffed.push(byte[0]);
        }
        let payload = open_frame(&stuffed).expect("a command is a good frame");
        decode_host_message(&payload).expect("a command is a HostMessage")
    }

    /// Send a message, framed the way the device frames one.
    fn send(&mut self, msg_type: &str, message_id: u16, in_reply_to: Option<u64>, fields: Value) {
        let payload = encode_device_message(
            msg_type,
            message_id,
            in_reply_to.map(|id| id as u16),
            fields.as_object().unwrap(),
        )
        .expect("a DeviceMessage");
        self.send_bytes(&seal_frame(&payload));
    }

    fn send_bytes(&mut self, bytes: &[u8]) {
        self.writer.write_all(bytes).unwrap();
        self.writer.flush().unwrap();
    }
}

#[test]
fn a_reply_is_matched_by_in_reply_to_and_not_by_arrival_order() {
    // **The rule the whole module exists for.** An event arrives first; the
    // session must not take it for the answer.
    let (mut session, mut board, recorded) = session_with_a_board();
    let asking = std::thread::spawn(move || {
        let reply = session.ping(Duration::from_secs(3));
        (session, reply)
    });

    let command = board.next_command();
    let message_id = command["message_id"].as_u64().unwrap();
    board.send("event", 900, None, json!({"us": 12, "word": 8}));
    board.send(
        "log",
        901,
        None,
        json!({"level": "info", "message": "a board talking"}),
    );
    board.send("pong", 902, Some(message_id), json!({}));

    let (_session, reply) = asking.join().unwrap();
    let reply = reply.expect("the pong is the answer");
    assert_eq!(reply["msg_type"], "pong");
    assert_eq!(reply["in_reply_to"].as_u64(), Some(message_id));

    let aside = recorded.unsolicited.lock().unwrap();
    assert_eq!(aside.len(), 2, "the event and the log went to the sink");
    assert_eq!(aside[0]["msg_type"], "event");
    assert_eq!(aside[1]["msg_type"], "log");
}

#[test]
fn an_error_with_no_in_reply_to_is_still_the_answer() {
    // The protocol requires `in_reply_to` on every reply, but a refusal that
    // arrives while exactly one command is outstanding is about that command
    // whatever it is labelled. Swallowing it would turn a clear "no hello yet"
    // into a five-second silence.
    let (mut session, mut board, recorded) = session_with_a_board();
    let asking = std::thread::spawn(move || session.state(Duration::from_secs(3)));

    board.next_command();
    board.send(
        "error",
        7,
        None,
        json!({"code": "not_ready", "message": "no hello yet", "context": "hello"}),
    );

    match asking.join().unwrap() {
        Err(RequestProblem::Refused(refusal)) => {
            assert_eq!(refusal.code, "not_ready");
            // The context names what to change, and is the useful half.
            assert_eq!(refusal.context, "hello");
        }
        other => panic!("expected a refusal, got {other:?}"),
    }
    let junk = recorded.junk.lock().unwrap();
    assert_eq!(junk.len(), 1, "and it is noted rather than taken silently");
    assert!(junk[0].1.contains("naming no message_id"), "{:?}", junk[0]);
}

#[test]
fn a_started_with_no_in_reply_to_is_an_event_not_an_answer() {
    // A trial can begin two ways: because the host said so, and because a start
    // line went high on a board arming its own trials. Routing `started` by
    // type alone would make `start_trial` unable to recognise its own reply.
    let (mut session, mut board, recorded) = session_with_a_board();
    let asking = std::thread::spawn(move || {
        let reply = session.ping(Duration::from_secs(3));
        (session, reply)
    });

    let message_id = board.next_command()["message_id"].as_u64().unwrap();
    board.send("started", 50, None, json!({"trial_id": 9}));
    board.send("pong", 51, Some(message_id), json!({}));

    let (_session, reply) = asking.join().unwrap();
    assert_eq!(reply.expect("the pong")["msg_type"], "pong");
    let aside = recorded.unsolicited.lock().unwrap();
    assert_eq!(aside.len(), 1);
    assert_eq!(aside[0]["msg_type"], "started");
}

#[test]
fn a_started_that_answers_a_command_is_the_answer() {
    // The other half of the same rule.
    let (mut session, mut board, recorded) = session_with_a_board();
    let asking =
        std::thread::spawn(move || session.request(MsgType::Start, Duration::from_secs(3), &[]));

    let message_id = board.next_command()["message_id"].as_u64().unwrap();
    board.send("started", 60, Some(message_id), json!({"trial_id": 9}));

    let reply = asking.join().unwrap().expect("started is the answer here");
    assert_eq!(reply["msg_type"], "started");
    assert!(
        recorded.unsolicited.lock().unwrap().is_empty(),
        "it was routed aside instead of answered with"
    );
}

#[test]
fn a_frame_with_a_bad_crc_is_junk_and_the_wait_goes_on() {
    let (mut session, mut board, recorded) = session_with_a_board();
    let asking = std::thread::spawn(move || {
        let reply = session.ping(Duration::from_secs(3));
        (session, reply)
    });

    let message_id = board.next_command()["message_id"].as_u64().unwrap();
    // A good frame with one bit of its CRC flipped, answering the very
    // command being waited for: believed, it would end the wait.
    let payload = encode_device_message(
        "pong",
        1,
        Some(message_id as u16),
        json!({}).as_object().unwrap(),
    )
    .unwrap();
    let mut bad = seal_frame(&payload);
    let crc_low = bad.len() - 2;
    bad[crc_low] ^= 0x01;
    if bad[crc_low] == 0 {
        bad[crc_low] ^= 0x03;
    }
    board.send_bytes(&bad);
    board.send("pong", 2, Some(message_id), json!({}));

    let (_session, reply) = asking.join().unwrap();
    assert_eq!(reply.expect("the good pong")["msg_type"], "pong");
    let junk = recorded.junk.lock().unwrap();
    assert!(
        junk.iter().any(|(_, why)| why.contains("bad_crc")),
        "a bad crc was not reported: {junk:?}"
    );
}

#[test]
fn a_board_that_says_nothing_times_out_and_says_what_it_was_asked() {
    let (mut session, _board, _recorded) = session_with_a_board();
    match session.ping(Duration::from_millis(250)) {
        Err(RequestProblem::NoReplyInTime(sentence)) => {
            assert!(sentence.contains("ping"), "{sentence}");
            assert!(sentence.contains("message_id"), "{sentence}");
        }
        other => panic!("expected a timeout, got {other:?}"),
    }
}

#[test]
fn message_ids_count_from_one_and_wrap_through_zero() {
    // Counting from 1 keeps a session's first command out of a hole older
    // firmware had, where 0 read as "no id could be read".
    let (mut session, mut board, _recorded) = session_with_a_board();
    let asking = std::thread::spawn(move || {
        for _ in 0..3 {
            let _ = session.ping(Duration::from_millis(80));
        }
    });
    let ids: Vec<u64> = (0..3)
        .map(|_| board.next_command()["message_id"].as_u64().unwrap())
        .collect();
    asking.join().unwrap();
    assert_eq!(ids, vec![1, 2, 3], "the first command must not be id 0");
}

#[test]
fn random_seeds_use_all_sixty_four_bits() {
    // A seed drawn from 32 bits, or from a double's 53, would repeat across
    // sessions far sooner than anybody would think to check.
    use statemachined::device::request_response_session::random_seed;
    let seeds: Vec<u64> = (0..32).map(|_| random_seed()).collect();
    assert!(seeds.iter().any(|seed| *seed > u64::MAX / 2));
    assert!(seeds.iter().any(|seed| seed & 1 == 1));
}

#[test]
fn hello_sends_the_protocol_version_and_every_bit_of_the_seed() {
    let (mut session, mut board, _recorded) = session_with_a_board();
    let asking = std::thread::spawn(move || {
        let _ = session.hello(Some(0x0123_4567_89AB_CDEF), Duration::from_millis(200));
    });
    let command = board.next_command();
    asking.join().unwrap();
    assert_eq!(command["msg_type"], "hello");
    assert_eq!(command["proto"], 2);
    // A number, and all of it: the link carries a uint64, where the NDJSON
    // wire needed a hex string because a JSON number is a double.
    assert_eq!(command["seed"], json!(0x0123_4567_89AB_CDEFu64));
}
