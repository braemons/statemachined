// SPDX-License-Identifier: AGPL-3.0-or-later
//! The request/response session, against a board that answers out of order.
//!
//! **What can be wrong here is the routing**, not the happy path. The protocol
//! lets `event`, `log` and `result_*` arrive between a command and its reply, so
//! a session that matched on arrival order would take an event for an answer.
//! These drive a fake board over a real socket and make it interleave.

use std::io::{BufRead, BufReader, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde_json::Value;
use statemachined::device::message_framing::statemachined_line;
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
    fn unsolicited(&mut self, message: &Value, _line: &str) {
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

/// The far end: reads whole command lines, writes whatever it is told.
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

    /// The next command, parsed.
    fn next_command(&mut self) -> Value {
        let mut line = String::new();
        self.reader.read_line(&mut line).expect("a command");
        serde_json::from_str(line.trim()).expect("a command is json")
    }

    /// Send a message, framed the way the device frames one.
    fn send(&mut self, body: &str) {
        let line = statemachined_line(body);
        self.writer.write_all(line.as_bytes()).unwrap();
        self.writer.write_all(b"\n").unwrap();
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
    board.send(r#"{"msg_type":"event","message_id":900,"line":3,"edge":"rise""#);
    board.send(r#"{"msg_type":"log","message_id":901,"text":"a board talking""#);
    board.send(&format!(
        r#"{{"msg_type":"pong","message_id":902,"in_reply_to":{message_id}"#
    ));

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
    board.send(r#"{"msg_type":"error","message_id":7,"code":"not_ready","message":"no hello yet","context":"hello""#);

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
    board.send(r#"{"msg_type":"started","message_id":50,"trial_id":9"#);
    board.send(&format!(
        r#"{{"msg_type":"pong","message_id":51,"in_reply_to":{message_id}"#
    ));

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
    let asking = std::thread::spawn(move || session.request(MsgType::Start, Duration::from_secs(3), &[]));

    let message_id = board.next_command()["message_id"].as_u64().unwrap();
    board.send(&format!(
        r#"{{"msg_type":"started","message_id":60,"in_reply_to":{message_id},"trial_id":9"#
    ));

    let reply = asking.join().unwrap().expect("started is the answer here");
    assert_eq!(reply["msg_type"], "started");
    assert!(
        recorded.unsolicited.lock().unwrap().is_empty(),
        "it was routed aside instead of answered with"
    );
}

#[test]
fn a_line_with_a_bad_crc_is_junk_and_the_wait_goes_on() {
    let (mut session, mut board, recorded) = session_with_a_board();
    let asking = std::thread::spawn(move || {
        let reply = session.ping(Duration::from_secs(3));
        (session, reply)
    });

    let message_id = board.next_command()["message_id"].as_u64().unwrap();
    // Written by hand so the CRC is wrong.
    board
        .writer
        .write_all(b"{\"msg_type\":\"pong\",\"message_id\":1,\"crc\":\"0000\"}\n")
        .unwrap();
    board.writer.flush().unwrap();
    board.send(&format!(
        r#"{{"msg_type":"pong","message_id":2,"in_reply_to":{message_id}"#
    ));

    let (_session, reply) = asking.join().unwrap();
    assert_eq!(reply.expect("the good pong")["msg_type"], "pong");
    let junk = recorded.junk.lock().unwrap();
    assert!(
        junk.iter().any(|(_, why)| why.contains("crc mismatch")),
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
fn a_seed_is_sixteen_hex_digits_and_never_a_number() {
    // 64 bits do not survive a double, and a seed that silently changes is a
    // reproducibility bug nobody would find.
    use statemachined::device::request_response_session::random_seed;
    for _ in 0..8 {
        let seed = random_seed();
        assert_eq!(seed.len(), 16, "{seed}");
        assert!(seed.bytes().all(|b| b.is_ascii_hexdigit()), "{seed}");
        assert!(seed.bytes().all(|b| !b.is_ascii_lowercase()), "{seed}");
    }
}

#[test]
fn hello_sends_the_protocol_version_and_the_seed_as_text() {
    let (mut session, mut board, _recorded) = session_with_a_board();
    let asking = std::thread::spawn(move || {
        let _ = session.hello(Some("0123456789ABCDEF"), Duration::from_millis(200));
    });
    let command = board.next_command();
    asking.join().unwrap();
    assert_eq!(command["msg_type"], "hello");
    assert_eq!(command["proto"], 1);
    assert_eq!(
        command["seed"],
        Value::String("0123456789ABCDEF".into()),
        "the seed must be text on the wire"
    );
}
