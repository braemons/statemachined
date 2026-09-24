// SPDX-License-Identifier: AGPL-3.0-or-later
//! Write `daemon-rs/src/wire/` from `proto/`.
//!
//! `prost-build` writes the Rust types and `tonic-prost-build` writes the
//! `service` blocks as traits the daemon implements. **An rpc with no
//! implementation is a compile error** — which is the same guarantee
//! `tests/unit/test_every_rpc_is_implemented.py` gives the Python daemon by
//! reading the descriptor at runtime, moved to where the compiler can give it.
//!
//! The descriptor set is written beside them and kept: server reflection serves
//! it, which is how `grpcurl` and a client discover this daemon's services
//! without having the `.proto` to hand. The Python daemon does not offer
//! reflection; this is one of the few things the port gains rather than keeps.
//!
//! The output is committed, the same inversion mousewheeld makes: a reviewer
//! sees an interface change in a diff, a checkout builds without protoc, and
//! anybody reading the repository can read the types instead of inferring them
//! from a build directory.

use std::path::PathBuf;

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let repository = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .and_then(|p| p.parent())
        .expect("tools/protogen lives two levels below the repository root")
        .to_path_buf();

    let proto_root = repository.join("proto");
    // One argument, and only `make check-proto` passes it: generate somewhere
    // else so the committed tree can be compared against a fresh one. Asking
    // git whether the tree changed cannot answer for a *new* file — an
    // untracked one has no diff — and a staleness check that goes quiet the
    // first time a message is added is worse than none.
    let out_dir = match std::env::args().nth(1) {
        Some(elsewhere) => PathBuf::from(elsewhere),
        None => repository.join("daemon-rs/src/wire"),
    };
    std::fs::create_dir_all(&out_dir)?;

    // Found rather than listed, because a proto added to the interface is part
    // of the interface and a list here is a way to leave one out. `braemons/v1/`
    // is the vendored copy the family agrees on — the `.tdr` outcome taxonomy,
    // which triald speaks too and neither daemon owns.
    let mut files: Vec<PathBuf> = Vec::new();
    for package in ["statemachined/v1", "braemons/v1"] {
        let mut found: Vec<PathBuf> = std::fs::read_dir(proto_root.join(package))?
            .filter_map(Result::ok)
            .map(|entry| entry.path())
            .filter(|path| path.extension().is_some_and(|e| e == "proto"))
            .collect();
        found.sort();
        files.extend(found);
    }

    let service_dir = out_dir.join("service");
    std::fs::create_dir_all(&service_dir)?;
    let mut tonic = tonic_prost_build::Config::new();
    tonic
        .out_dir(&service_dir)
        // `google.protobuf.Struct` is the one well-known type this interface
        // uses: `state.proto` carries a graph's own parameters, which are a
        // document rather than a schema this daemon owns.
        .extern_path(".google.protobuf", "::prost_types")
        // Point at the messages prost already generated rather than making a
        // second set: without this the trait asks for `service::DeviceInfo` and
        // the daemon has a `wire::DeviceInfo`, which are different types with
        // the same fields.
        // The nested spelling, because there are two packages: prost names a
        // type in the other one `super::super::braemons::v1::X`, which only
        // resolves if each package sits in a module matching its own name.
        .extern_path(".statemachined.v1", "crate::wire::statemachined::v1")
        .extern_path(".braemons.v1", "crate::wire::braemons::v1")
        .compile_well_known_types()
        .btree_map(["."]);
    tonic_prost_build::configure()
        .build_server(true)
        // No Rust client: this daemon calls nobody over gRPC. The Python client
        // is generated in its own tree, from the same files.
        .build_client(false)
        .out_dir(&service_dir)
        .file_descriptor_set_path(out_dir.join("descriptor_for_reflection.bin"))
        .compile_with_config(tonic, &files, &[proto_root.clone()])?;

    let mut config = prost_build::Config::new();
    config
        .out_dir(&out_dir)
        .compile_well_known_types()
        .extern_path(".google.protobuf", "::prost_types")
        .btree_map(["."]);
    config.compile_protos(&files, &[&proto_root])?;

    // The board's link: a separate package, and only its descriptor. The daemon
    // does not compile the link into types; it reads this at run time and
    // carries each message as the `msg_type` and fields it has always handled,
    // so only the codec under them had to change. See
    // `daemon-rs/src/device/link_codec.rs`.
    let scratch = std::env::temp_dir().join(format!("protogen-link-{}", std::process::id()));
    std::fs::create_dir_all(&scratch)?;
    prost_build::Config::new()
        .out_dir(&scratch)
        .file_descriptor_set_path(out_dir.join("link_descriptor.bin"))
        .compile_protos(
            &[proto_root.join("statemachined/link/v1/link.proto")],
            &[&proto_root],
        )?;
    std::fs::remove_dir_all(&scratch)?;

    println!("wrote {}", out_dir.display());
    Ok(())
}
