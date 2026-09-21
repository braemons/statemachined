// SPDX-License-Identifier: AGPL-3.0-or-later
//! The framing, against the bytes the device agrees to.
//!
//! `daemon/tests/unit/wire_vectors.json` is **data, not code**, and it is the
//! authority: every CRC in it was produced by the emulator's independent copy
//! and verified against `firmware/core/protocol/crc16.cpp`. So this is not a
//! test that the Rust framing agrees with the Python framing — it is a test
//! that it agrees with the board, which is the thing that matters and the
//! stronger of the two.
//!
//! The same file is what `daemon/tests/unit/test_message_framing.py` asserts
//! against, so the two daemons are held to one set of bytes rather than to each
//! other.

use serde::Deserialize;
use statemachined::device::message_framing::{
    covered_bytes, crc16_ccitt, parse_reply, rolling_checksum, statemachined_line, CRC_INIT,
};

#[derive(Debug, Deserialize)]
struct Vectors {
    lines: Vec<LineVector>,
    rolling: RollingVector,
}

#[derive(Debug, Deserialize)]
struct LineVector {
    why: String,
    /// A message without its closing brace.
    body: String,
    /// What must go on the wire.
    line: String,
}

#[derive(Debug, Deserialize)]
struct RollingVector {
    why: String,
    /// Indices into `lines`.
    over: Vec<usize>,
    checksum: String,
}

fn vectors() -> Vectors {
    serde_json::from_str(include_str!(
        "../../daemon/tests/unit/wire_vectors.json"
    ))
    .expect("the golden vectors")
}

#[test]
fn the_vectors_are_there() {
    // A guard on the guard: an empty file would make every test below pass.
    let vectors = vectors();
    assert!(!vectors.lines.is_empty(), "no line vectors");
    assert!(!vectors.rolling.over.is_empty(), "no rolling vector");
}

#[test]
fn every_golden_line_is_framed_exactly() {
    for vector in vectors().lines {
        assert_eq!(
            statemachined_line(&vector.body),
            vector.line,
            "{}: the bytes this daemon would put on the wire are not the device's",
            vector.why
        );
    }
}

#[test]
fn every_golden_line_parses_back() {
    // The other direction: what the device sends is what this reads. The CRC is
    // checked before the JSON is touched, which is the rule the module exists
    // to keep, so a line that parses here is one whose CRC was good.
    for vector in vectors().lines {
        let parsed = parse_reply(&vector.line)
            .unwrap_or_else(|problem| panic!("{}: {problem}", vector.why));
        assert!(parsed.get("msg_type").is_some(), "{}", vector.why);
    }
}

#[test]
fn the_rolling_checksum_folds_the_covered_bytes_in_order() {
    let vectors = vectors();
    let folded: Vec<String> = vectors
        .rolling
        .over
        .iter()
        .map(|at| vectors.lines[*at].line.clone())
        .collect();
    assert_eq!(
        format!("{:04X}", rolling_checksum(&folded, CRC_INIT)),
        vectors.rolling.checksum,
        "{}",
        vectors.rolling.why
    );
}

#[test]
fn a_line_with_a_wrong_crc_is_refused_before_it_is_parsed() {
    // **The rule this module exists for.** A line whose CRC does not match is
    // not acted on, and parsing it is acting on it.
    let line = r#"{"msg_type":"ping","message_id":1,"crc":"0000"}"#;
    let problem = parse_reply(line).expect_err("a bad crc is refused");
    assert!(problem.to_string().contains("crc mismatch"), "{problem}");
}

#[test]
fn a_line_that_is_not_ascii_is_a_framing_error() {
    let problem = parse_reply("{\"msg_type\":\"pîng\",\"crc\":\"0000\"}")
        .expect_err("a non-ascii line is refused");
    assert!(problem.to_string().contains("0x80"), "{problem}");
}

#[test]
fn a_line_with_no_crc_member_is_a_framing_error() {
    let problem =
        parse_reply(r#"{"msg_type":"ping","message_id":1}"#).expect_err("no crc is refused");
    assert!(problem.to_string().contains("no trailing crc"), "{problem}");
}

#[test]
fn good_crc_but_broken_json_says_which_it_was() {
    // Worth telling apart: a bad CRC means the wire ate something, and good CRC
    // with bad JSON means the *device* sent something wrong. A person on a
    // bench chases different things for each.
    let body = r#"{"msg_type":"ping","message_id":"#;
    let line = statemachined_line(body);
    let problem = parse_reply(&line).expect_err("truncated json is refused");
    assert!(problem.to_string().contains("not JSON"), "{problem}");
}

#[test]
fn the_covered_span_stops_before_the_crc_member() {
    for vector in vectors().lines {
        assert_eq!(
            covered_bytes(&vector.line),
            vector.body.as_bytes(),
            "{}: the rolling checksum would fold the wrong bytes",
            vector.why
        );
    }
}

#[test]
fn the_seed_is_what_makes_the_fold_work() {
    // Folding two lines one after another must equal seeding the second with
    // the first's result -- which is the whole mechanism of the rolling
    // checksum, and is worth pinning separately from the golden value.
    let vectors = vectors();
    let first = crc16_ccitt(covered_bytes(&vectors.lines[0].line), CRC_INIT);
    let both = crc16_ccitt(covered_bytes(&vectors.lines[1].line), first);
    assert_eq!(
        both,
        rolling_checksum(
            &[vectors.lines[0].line.clone(), vectors.lines[1].line.clone()],
            CRC_INIT
        )
    );
}

#[test]
fn a_built_command_is_a_golden_line() {
    // `command_line` is what every caller actually uses; `statemachined_line`
    // is the half of it the vectors name. This ties the two together, so a
    // change to how members are written shows up against the device's bytes
    // rather than only against this module's own idea of them.
    use statemachined::device::message_framing::command_line;
    use statemachined::device::message_vocabulary::MsgType;

    assert_eq!(
        command_line(MsgType::Ping.as_str(), 1, &[]),
        r#"{"msg_type":"ping","message_id":1,"crc":"6FE7"}"#
    );
    assert_eq!(
        command_line(MsgType::State.as_str(), 0, &[]),
        r#"{"msg_type":"state","message_id":0,"crc":"2B9F"}"#
    );
    // Member order is part of the bytes: a map that sorted these would frame a
    // different line with a different CRC.
    assert_eq!(
        command_line(
            MsgType::Hello.as_str(),
            1,
            &[
                ("proto", serde_json::json!(1)),
                ("seed", serde_json::json!("0123456789ABCDEF")),
            ]
        ),
        r#"{"msg_type":"hello","message_id":1,"proto":1,"seed":"0123456789ABCDEF","crc":"4D87"}"#
    );
}

#[test]
fn a_null_field_is_written_and_not_dropped() {
    // **The protocol distinguishes the two.** `terminal` must be present on
    // every graph_state, as an outcome code or as null for a state that is not
    // terminal, and a device that silently accepted the member's absence would
    // be guessing which a graph meant.
    use statemachined::device::message_framing::command_line;

    let line = command_line("graph_state", 7, &[("terminal", serde_json::Value::Null)]);
    assert!(line.contains(r#""terminal":null"#), "{line}");
}
