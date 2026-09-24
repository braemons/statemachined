// SPDX-License-Identifier: AGPL-3.0-or-later
//! The transport, over a real socket and a real loopback.
//!
//! **Not a mock.** The thing that can actually be wrong in a link is the line
//! splitting: a message that straddles two reads, a `\r` that should be
//! stripped, a partial line that must wait rather than be delivered in halves.
//! A fake that hands over whole lines exercises none of that, so these drive a
//! TCP socket with the bytes deliberately cut in the wrong places.

use std::io::Write;
use std::net::{TcpListener, TcpStream};
use std::time::{Duration, Instant};

use statemachined::device::serial_link::{to_target, SerialLink, Target, FROM_DEVICE, TO_DEVICE};

/// A listener and the target string that reaches it.
fn a_socket() -> (TcpListener, String) {
    let listener = TcpListener::bind("127.0.0.1:0").expect("a port");
    let address = listener.local_addr().expect("its address").to_string();
    (listener, address)
}

/// A link, and the far end of it, for a server that writes what it is told.
fn linked() -> (SerialLink, TcpStream) {
    let (listener, address) = a_socket();
    let link = std::thread::spawn(move || {
        SerialLink::open(&address, 115_200, Duration::from_secs(2)).expect("the link opens")
    });
    let (far_end, _) = listener.accept().expect("the link connects");
    (link.join().expect("the opening thread"), far_end)
}

#[test]
fn a_target_names_its_transport() {
    assert_eq!(to_target("/dev/ttyACM0"), Target::Port("/dev/ttyACM0".into()));
    // `host:port` with no scheme, so the ethernet case needs none from a
    // person's fingers.
    assert_eq!(
        to_target("rig-3.local:9000"),
        Target::Socket("rig-3.local:9000".into())
    );
    assert_eq!(
        to_target("socket://127.0.0.1:9000"),
        Target::Socket("127.0.0.1:9000".into())
    );
    assert_eq!(to_target("loop://"), Target::Loopback);
    // A path that merely contains a colon is not a socket.
    assert_eq!(
        to_target("/dev/serial/by-id/usb-x:y"),
        Target::Port("/dev/serial/by-id/usb-x:y".into())
    );
}

#[test]
fn a_line_split_across_reads_arrives_whole() {
    // **The failure this buffering exists for.** Delivered in halves, both
    // halves fail their CRC and the message is lost for no reason the person
    // reading the log can see.
    let (mut link, mut far_end) = linked();
    far_end.write_all(b"{\"msg_type\":\"pon").unwrap();
    far_end.flush().unwrap();

    // Nothing yet: half a line is not a line.
    assert_eq!(
        link.read_line(Some(Duration::from_millis(150))).unwrap(),
        None,
        "half a line was delivered as a whole one"
    );

    far_end.write_all(b"g\",\"message_id\":4}\n").unwrap();
    far_end.flush().unwrap();
    assert_eq!(
        link.read_line(Some(Duration::from_secs(2))).unwrap().as_deref(),
        Some(r#"{"msg_type":"pong","message_id":4}"#),
        "the halves were not rejoined"
    );
}

#[test]
fn several_lines_in_one_read_come_back_one_at_a_time() {
    let (mut link, mut far_end) = linked();
    far_end.write_all(b"one\ntwo\nthree\n").unwrap();
    far_end.flush().unwrap();
    for expected in ["one", "two", "three"] {
        assert_eq!(
            link.read_line(Some(Duration::from_secs(2))).unwrap().as_deref(),
            Some(expected)
        );
    }
    assert_eq!(link.read_line(Some(Duration::from_millis(50))).unwrap(), None);
}

#[test]
fn a_carriage_return_is_not_part_of_the_line() {
    let (mut link, mut far_end) = linked();
    far_end.write_all(b"pong\r\n").unwrap();
    far_end.flush().unwrap();
    assert_eq!(
        link.read_line(Some(Duration::from_secs(2))).unwrap().as_deref(),
        Some("pong"),
        "a CRLF link would fail every CRC"
    );
}

#[test]
fn a_quiet_link_returns_none_rather_than_waiting_for_the_long_timeout() {
    // **The reason `timeout` is a parameter.** A poll that blocked for the
    // command timeout would hold the device lock for that long and make every
    // request wait behind it.
    let (mut link, _far_end) = linked();
    let started = Instant::now();
    assert_eq!(link.read_line(Some(Duration::from_millis(120))).unwrap(), None);
    let waited = started.elapsed();
    assert!(
        waited < Duration::from_millis(900),
        "a short poll waited {waited:?}, so it used the link's timeout"
    );
}

#[test]
fn a_board_that_goes_away_is_an_error_and_not_a_quiet_link() {
    // A closed socket reads as end of file, and end of file is not a board
    // with nothing to say: taken for silence, the daemon sat on a dead link
    // until a request timed out, and never wrote down that it was lost. It is
    // pyserial's error, word for word, because the daemon records it.
    let (mut link, far_end) = linked();
    drop(far_end);
    let problem = link.read_line(Some(Duration::from_millis(200))).unwrap_err();
    assert_eq!(problem.to_string(), "read failed: socket disconnected");
}

#[test]
fn the_loopback_reads_back_what_was_written() {
    // `loop://` is how a daemon runs with no board at all, which is what every
    // bench and this repository's own defaults do.
    let mut link = SerialLink::open("loop://", 115_200, Duration::from_millis(200))
        .expect("a loopback needs nothing");
    link.write_line("{\"msg_type\":\"ping\"}").unwrap();
    assert_eq!(
        link.read_line(Some(Duration::from_millis(200))).unwrap().as_deref(),
        Some("{\"msg_type\":\"ping\"}")
    );
    assert_eq!(link.read_line(Some(Duration::from_millis(50))).unwrap(), None);
}

#[test]
fn the_monitor_sees_both_directions_including_what_the_session_would_not() {
    // A monitor that only saw what the session understood would miss exactly
    // what somebody opens a monitor for: the junk, the reply to a command that
    // had already timed out, the line with the bad CRC.
    use std::sync::{Arc, Mutex};

    let seen: Arc<Mutex<Vec<(String, String)>>> = Arc::new(Mutex::new(Vec::new()));
    let recorded = Arc::clone(&seen);

    let (mut link, mut far_end) = linked();
    link.observe(move |direction, line| {
        recorded
            .lock()
            .unwrap()
            .push((direction.to_string(), line.to_string()));
    });

    link.write_line("a command").unwrap();
    far_end.write_all(b"!! not a message at all\n").unwrap();
    far_end.flush().unwrap();
    link.read_line(Some(Duration::from_secs(2))).unwrap();

    let seen = seen.lock().unwrap();
    assert_eq!(
        seen.as_slice(),
        [
            (TO_DEVICE.to_string(), "a command".to_string()),
            (FROM_DEVICE.to_string(), "!! not a message at all".to_string()),
        ]
    );
}

#[test]
fn resetting_drops_a_partial_line_rather_than_carrying_it_into_a_session() {
    // A board that has been running on its own has been talking to nobody, and
    // a partial line left in the buffer produces one spurious framing complaint
    // at the start of the next session.
    let (mut link, mut far_end) = linked();
    far_end.write_all(b"half a line with no newline").unwrap();
    far_end.flush().unwrap();
    assert_eq!(link.read_line(Some(Duration::from_millis(150))).unwrap(), None);

    link.reset_input();
    far_end.write_all(b"pong\n").unwrap();
    far_end.flush().unwrap();
    assert_eq!(
        link.read_line(Some(Duration::from_secs(2))).unwrap().as_deref(),
        Some("pong"),
        "the stale partial line was glued to the next one"
    );
}
