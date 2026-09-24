// SPDX-License-Identifier: AGPL-3.0-or-later
//! The framing: COBS, the CRC, and a frame read back.
//!
//! The board's side is `firmware/core/protocol/framing.cpp`, and the two are
//! held to each other end to end in `tests/upload.rs`, where the daemon talks to
//! the firmware compiled for this machine. What this holds them to is the
//! arithmetic both are written from: the published COBS vectors, the published
//! CRC check values, and the frame the protocol document draws.

use statemachined::device::message_framing::{
    cobs_decode, cobs_encode, crc16_ccitt, open_frame, seal_frame, CRC_INIT,
};

#[test]
fn crc16_ccitt_false_matches_its_published_check_values() {
    assert_eq!(crc16_ccitt(b"123456789", CRC_INIT), 0x29B1);
    assert_eq!(crc16_ccitt(b"", CRC_INIT), 0xFFFF);
    assert_eq!(crc16_ccitt(b"A", CRC_INIT), 0xB915);
}

#[test]
fn the_seed_makes_the_crc_an_accumulator() {
    // set_end's and result_end's checksums fold many messages without keeping
    // any of them, which only works if a split is invisible.
    let whole = crc16_ccitt(b"123456789", CRC_INIT);
    let folded = crc16_ccitt(b"56789", crc16_ccitt(b"1234", CRC_INIT));
    assert_eq!(folded, whole);
}

#[test]
fn cobs_encodes_the_published_vectors() {
    let cases: &[(&[u8], &[u8])] = &[
        (&[0x00], &[0x01, 0x01]),
        (&[0x00, 0x00], &[0x01, 0x01, 0x01]),
        (&[0x00, 0x11, 0x00], &[0x01, 0x02, 0x11, 0x01]),
        (&[0x11, 0x22, 0x00, 0x33], &[0x03, 0x11, 0x22, 0x02, 0x33]),
        (&[0x11, 0x22, 0x33, 0x44], &[0x05, 0x11, 0x22, 0x33, 0x44]),
        (&[0x11, 0x00, 0x00, 0x00], &[0x02, 0x11, 0x01, 0x01, 0x01]),
        (&[], &[0x01]),
    ];
    for (raw, stuffed) in cases {
        assert_eq!(&cobs_encode(raw), stuffed, "{raw:02x?}");
        assert_eq!(
            cobs_decode(stuffed).as_deref(),
            Some(*raw),
            "{stuffed:02x?}"
        );
    }
}

#[test]
fn a_run_longer_than_one_cobs_group_round_trips() {
    for length in [253, 254, 255, 508, 509, 600] {
        let raw: Vec<u8> = (0..length).map(|at| 1 + (at % 255) as u8).collect();
        let stuffed = cobs_encode(&raw);
        assert!(
            !stuffed.contains(&0),
            "a zero in the stuffing of {length} bytes"
        );
        assert_eq!(cobs_decode(&stuffed), Some(raw), "{length} bytes");
    }
}

#[test]
fn a_frame_is_the_payload_and_its_crc_big_endian_stuffed_and_ended_with_a_zero() {
    let payload = [0x08, 0x29, 0xBA, 0x01, 0x00];
    let crc = crc16_ccitt(&payload, CRC_INIT);
    let mut sealed = payload.to_vec();
    sealed.extend_from_slice(&[(crc >> 8) as u8, crc as u8]);
    let mut expected = cobs_encode(&sealed);
    expected.push(0);
    let frame = seal_frame(&payload);
    assert_eq!(frame, expected);
    assert_eq!(frame.iter().filter(|byte| **byte == 0).count(), 1);
    assert_eq!(open_frame(&frame[..frame.len() - 1]), Ok(payload.to_vec()));
}

#[test]
fn every_corrupted_byte_is_refused_rather_than_partly_believed() {
    let payload: Vec<u8> = (0..40u8)
        .map(|at| if at % 4 == 0 { 0 } else { at })
        .collect();
    let frame = seal_frame(&payload);
    let body = &frame[..frame.len() - 1];
    for at in 0..body.len() {
        for flip in [0x01u8, 0x80, 0xFF] {
            let mut bad = body.to_vec();
            bad[at] ^= flip;
            if bad[at] == 0 {
                continue;
            }
            let refused = open_frame(&bad);
            assert!(refused.is_err(), "byte {at} ^ {flip:02x} got through");
        }
    }
}

#[test]
fn a_refusal_names_itself_in_the_boards_own_words() {
    let frame = seal_frame(&[0x11, 0x22, 0x33]);
    let mut bad_crc = frame[..frame.len() - 1].to_vec();
    let last = bad_crc.len() - 1;
    bad_crc[last] ^= 0x01;
    assert_eq!(open_frame(&bad_crc).unwrap_err().code, "bad_crc");
    assert_eq!(open_frame(&[0x05, 0x11]).unwrap_err().code, "bad_cobs");
    assert_eq!(open_frame(&[0x02, 0x11]).unwrap_err().code, "bad_cobs");
}
