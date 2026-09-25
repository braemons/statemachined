// SPDX-License-Identifier: AGPL-3.0-or-later
//! The transport, over a real socket and a real loopback.
//!
//! **Not a mock.** The thing that can actually be wrong in a link is the frame
//! splitting: a message that straddles two reads, a partial frame that must
//! wait rather than be delivered in halves, junk that must be given up on. A
//! fake that hands over whole frames exercises none of that, so these drive a
//! TCP socket with the bytes deliberately cut in the wrong places.

use std::io::Write;
use std::net::{TcpListener, TcpStream};
use std::time::{Duration, Instant};

use statemachined::device::link_codec::{encode_device_message, encode_host_message};
use statemachined::device::message_framing::{seal_frame, MAX_FRAME};
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
    assert_eq!(
        to_target("/dev/ttyACM0"),
        Target::Port("/dev/ttyACM0".into())
    );
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

/// A payload with zeros in it, as nearly every protobuf message has.
fn payload(salt: u8, length: usize) -> Vec<u8> {
    (0..length)
        .map(|at| {
            if at % 3 == 0 {
                0
            } else {
                salt.wrapping_add(at as u8)
            }
        })
        .collect()
}

fn frame_bytes(message: &[u8]) -> Vec<u8> {
    seal_frame(message)
}

#[test]
fn a_frame_split_across_reads_arrives_whole() {
    // **The failure this buffering exists for.** Delivered in halves, both
    // halves fail their CRC and the message is lost for no reason the person
    // reading the log can see.
    let (mut link, mut far_end) = linked();
    let message = payload(7, 40);
    let bytes = frame_bytes(&message);
    far_end.write_all(&bytes[..17]).unwrap();
    far_end.flush().unwrap();

    // Nothing yet: half a frame is not a frame.
    assert_eq!(
        link.read_frame(Some(Duration::from_millis(150))).unwrap(),
        None,
        "half a frame was delivered as a whole one"
    );

    far_end.write_all(&bytes[17..]).unwrap();
    far_end.flush().unwrap();
    assert_eq!(
        link.read_frame(Some(Duration::from_secs(2))).unwrap(),
        Some(Ok(message)),
        "the halves were not rejoined"
    );
}

#[test]
fn several_frames_in_one_read_come_back_one_at_a_time() {
    let (mut link, mut far_end) = linked();
    let messages = [payload(1, 5), payload(2, 300), payload(3, 1)];
    let mut bytes = Vec::new();
    for message in &messages {
        bytes.extend(frame_bytes(message));
    }
    far_end.write_all(&bytes).unwrap();
    far_end.flush().unwrap();
    for expected in messages {
        assert_eq!(
            link.read_frame(Some(Duration::from_secs(2))).unwrap(),
            Some(Ok(expected))
        );
    }
    assert_eq!(
        link.read_frame(Some(Duration::from_millis(50))).unwrap(),
        None
    );
}

#[test]
fn back_to_back_delimiters_are_not_frames() {
    // How a sender resynchronises a receiver that may be mid-frame.
    let (mut link, mut far_end) = linked();
    let message = payload(4, 12);
    let mut bytes = vec![0, 0, 0];
    bytes.extend(frame_bytes(&message));
    far_end.write_all(&bytes).unwrap();
    far_end.flush().unwrap();
    assert_eq!(
        link.read_frame(Some(Duration::from_secs(2))).unwrap(),
        Some(Ok(message))
    );
}

#[test]
fn a_bad_frame_is_handed_up_as_the_refusal_it_is_and_the_next_one_still_arrives() {
    let (mut link, mut far_end) = linked();
    let mut bad = frame_bytes(&payload(5, 20));
    let crc_low = bad.len() - 2;
    bad[crc_low] ^= 0x01;
    if bad[crc_low] == 0 {
        bad[crc_low] ^= 0x03;
    }
    let good = payload(6, 9);
    bad.extend(frame_bytes(&good));
    far_end.write_all(&bad).unwrap();
    far_end.flush().unwrap();
    let refused = link
        .read_frame(Some(Duration::from_secs(2)))
        .unwrap()
        .unwrap();
    assert_eq!(refused.unwrap_err().code, "bad_crc");
    assert_eq!(
        link.read_frame(Some(Duration::from_secs(2))).unwrap(),
        Some(Ok(good))
    );
}

#[test]
fn junk_with_no_delimiter_is_given_up_on_rather_than_buffered_for_ever() {
    let (mut link, mut far_end) = linked();
    far_end.write_all(&vec![0x41; MAX_FRAME + 10]).unwrap();
    far_end.flush().unwrap();
    let refused = link
        .read_frame(Some(Duration::from_secs(2)))
        .unwrap()
        .unwrap();
    assert_eq!(refused.unwrap_err().code, "too_long");
}

#[test]
fn a_quiet_link_returns_none_rather_than_waiting_for_the_long_timeout() {
    // **The reason `timeout` is a parameter.** A poll that blocked for the
    // command timeout would hold the device lock for that long and make every
    // request wait behind it.
    let (mut link, _far_end) = linked();
    let started = Instant::now();
    assert_eq!(
        link.read_frame(Some(Duration::from_millis(120))).unwrap(),
        None
    );
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
    let problem = link
        .read_frame(Some(Duration::from_millis(200)))
        .unwrap_err();
    assert_eq!(problem.to_string(), "read failed: socket disconnected");
}

#[test]
fn the_loopback_reads_back_what_was_written() {
    // `loop://` is how a daemon runs with no board at all, which is what every
    // bench and this repository's own defaults do.
    let mut link = SerialLink::open("loop://", 115_200, Duration::from_millis(200))
        .expect("a loopback needs nothing");
    let message = payload(9, 30);
    link.write_frame(&message).unwrap();
    assert_eq!(
        link.read_frame(Some(Duration::from_millis(200))).unwrap(),
        Some(Ok(message))
    );
    assert_eq!(
        link.read_frame(Some(Duration::from_millis(50))).unwrap(),
        None
    );
}

#[test]
fn the_monitor_sees_both_directions_including_what_the_session_would_not() {
    // A monitor that only saw what the session understood would miss exactly
    // what somebody opens a monitor for: the junk, the reply to a command that
    // had already timed out, the frame with the bad CRC. It sees each message
    // as the daemon reads it, since the bytes themselves are not for reading.
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

    let no_fields = serde_json::Map::new();
    link.write_frame(&encode_host_message("ping", 3, &no_fields).unwrap())
        .unwrap();
    let pong = encode_device_message("pong", 8, Some(3), &no_fields).unwrap();
    let mut bytes = frame_bytes(&pong);
    bytes.extend([0x05, 0x11, 0x00]);
    far_end.write_all(&bytes).unwrap();
    far_end.flush().unwrap();
    link.read_frame(Some(Duration::from_secs(2))).unwrap();
    link.read_frame(Some(Duration::from_secs(2))).unwrap();

    let seen = seen.lock().unwrap();
    assert_eq!(seen.len(), 3);
    assert_eq!(seen[0].0, TO_DEVICE);
    assert_eq!(seen[0].1, r#"{"msg_type":"ping","message_id":3}"#);
    assert_eq!(seen[1].0, FROM_DEVICE);
    assert_eq!(
        seen[1].1,
        r#"{"msg_type":"pong","message_id":8,"in_reply_to":3,"up_us":0,"us":0}"#
    );
    assert_eq!(seen[2].0, FROM_DEVICE);
    assert!(seen[2].1.starts_with("<bad_cobs"), "{}", seen[2].1);
}

#[test]
fn resetting_drops_a_partial_frame_rather_than_carrying_it_into_a_session() {
    // A board that has been running on its own has been talking to nobody, and
    // a partial frame left in the buffer produces one spurious framing
    // complaint at the start of the next session.
    let (mut link, mut far_end) = linked();
    let stale = frame_bytes(&payload(2, 20));
    far_end.write_all(&stale[..10]).unwrap();
    far_end.flush().unwrap();
    assert_eq!(
        link.read_frame(Some(Duration::from_millis(150))).unwrap(),
        None
    );

    link.reset_input();
    let message = payload(3, 8);
    far_end.write_all(&frame_bytes(&message)).unwrap();
    far_end.flush().unwrap();
    assert_eq!(
        link.read_frame(Some(Duration::from_secs(2))).unwrap(),
        Some(Ok(message)),
        "the stale partial frame was glued to the next one"
    );
}
