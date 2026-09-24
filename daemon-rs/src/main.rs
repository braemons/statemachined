// SPDX-License-Identifier: AGPL-3.0-or-later
//! The daemon: one port, the panels and every rpc on it.

use std::sync::Arc;

use clap::Parser;
use statemachined::daemon_state::{DaemonState, RigConfiguration};
use statemachined::grpc::DaemonServices;
use statemachined::web;
use statemachined::wire;

/// What the daemon is asked to do. `serve` is the only thing this binary does,
/// and the word is accepted so the packaged unit — `statemachined serve …`,
/// written for the Python daemon — starts this one unchanged. The Python
/// command's bench tools (`hello`, `pins`, `monitor`, `device`, …) are not
/// here.
#[derive(Clone, Copy, clap::ValueEnum)]
enum Mode {
    Serve,
}

#[derive(Parser)]
#[command(name = "statemachined", about = "Runs a trial's state machine on the board")]
// argparse takes the last of a repeated option, and the unit relies on it: an
// override in /etc/braemons/statemachined.env follows the unit's own --port.
#[command(args_override_self = true)]
struct Arguments {
    /// `serve`, or nothing: the same thing.
    #[arg(value_enum)]
    mode: Option<Mode>,

    /// Do not advertise `_statemachined._tcp`. The API is served either way; a
    /// console then needs this rig's address by hand.
    #[arg(long)]
    no_mdns: bool,

    /// The port the panels and the rpcs share.
    ///
    /// **One port is all this daemon needs**, where the Python one needs two:
    /// `grpc.aio` owns its socket, so that daemon serves the panels on `port`
    /// and gRPC on `port + 1`. tonic is a tower service and merges into the
    /// axum router, so everything is on `port` here.
    #[arg(long, default_value_t = 8081)]
    port: u16,

    /// Serve the same thing on `port + 1` as well, as the Python daemon's gRPC
    /// port is. On by default, **for the cutover**: `statemachined-client`
    /// dials 8082 when told nothing, triald's executor setting names it, and
    /// the plan is that nothing which talks to this daemon changes when the
    /// daemon does (`dev/RUST_PORT.md` §2, §6). Dropping the second port is a
    /// family decision to take afterwards, in `contracts/DAEMON_LAYOUT.md`.
    #[arg(long, default_value_t = true, action = clap::ArgAction::Set)]
    serve_on_the_next_port_too: bool,

    /// Bind address. Loopback on a development box; a rig's unit binds the rig
    /// network.
    ///
    /// `--host` is the Python daemon's spelling, which the unit uses. The
    /// default is loopback where the Python daemon's is every interface: a rig
    /// says `--host 0.0.0.0` in its unit, and a daemon somebody starts by hand
    /// on a laptop should not be reachable from the lab network by accident.
    #[arg(long, alias = "host", default_value = "127.0.0.1")]
    bind: String,

    /// The board's port. `loop://` is no board at all.
    ///
    /// `-t`/`--target` is the Python command's spelling. Given, it wins over
    /// the rig config's `device_target`.
    #[arg(long, short = 't', alias = "target", default_value = "loop://")]
    device: String,

    /// Where the documents live. A rig uses `/var/lib/braemons/statemachined`;
    /// this is here so a bench can point at a copy.
    #[arg(long)]
    storage_dir: Option<std::path::PathBuf>,

    /// The TOML that says what this box is. Its absence means the built-in
    /// defaults, which is what a bench with no conffile runs on.
    #[arg(long, default_value = statemachined::rig_configuration::DEFAULT_CONFIGURATION_PATH)]
    config: std::path::PathBuf,
}

#[tokio::main]
async fn main() {
    env_logger::Builder::from_env(env_logger::Env::default().default_filter_or("info")).init();
    let arguments = Arguments::parse();

    let mut configuration = match RigConfiguration::read(&arguments.config) {
        Ok(configuration) => configuration,
        Err(problem) => {
            eprintln!("statemachined: {problem}");
            std::process::exit(1);
        }
    };
    // The flag wins over the file, which is what a flag is for.
    if std::env::args().any(|argument| {
        argument.starts_with("--device") || argument.starts_with("--target") || argument.starts_with("-t")
    }) {
        configuration.device_target = arguments.device.clone();
    }
    if let Err(problem) = configuration.validate() {
        eprintln!("statemachined: {}: {problem}", arguments.config.display());
        std::process::exit(1);
    }
    if let Some(root) = &arguments.storage_dir {
        configuration.graph_store_directory = root.join("graphs");
        configuration.state_machine_config_directory = root.join("configs");
        configuration.trace_directory = root.join("trace");
        configuration.recording_directory = root.join("recordings");
    }
    let state = Arc::new(DaemonState::new(configuration));
    // Before the server, so no call is answered out of a half-started daemon:
    // the startup config is loaded and the board greeted (if the rig config
    // says so) first. Blocking — a greeting waits on a serial port.
    {
        let state = state.clone();
        tokio::task::spawn_blocking(move || state.start())
            .await
            .expect("startup does not panic");
    }
    // Then the thread that reads the board between requests: visits, results,
    // and the heartbeat the device's link-loss watchdog waits for.
    let _link = state.read_the_link_forever();
    let services = DaemonServices::new(state.clone());

    let address = format!("{}:{}", arguments.bind, arguments.port);
    let listener = match tokio::net::TcpListener::bind(&address).await {
        Ok(listener) => listener,
        Err(problem) => {
            eprintln!("statemachined: cannot bind {address}: {problem}");
            std::process::exit(1);
        }
    };
    log::info!("statemachined on {address}  (panels at /, gRPC and reflection on the same port)");
    log::warn!(
        "this is the Rust port: every rpc answers as the Python daemon does, and it has not \
         yet run a session on a rig"
    );

    // **axum and tonic on one listener**, exactly as mousewheeld does it. Each
    // service registers its own path — `/statemachined.v1.State/…` — so no
    // route is written down anywhere and renaming a service in the `.proto`
    // moves it.
    use wire::statemachined::v1::service;
    let mut routes = tonic::service::Routes::builder();
    routes
        .add_service(service::state_server::StateServer::new(services.clone()))
        .add_service(service::trial_server::TrialServer::new(services.clone()))
        .add_service(service::device_server::DeviceServer::new(services.clone()))
        .add_service(service::graph_store_server::GraphStoreServer::new(services.clone()))
        .add_service(
            service::state_machine_config_store_server::StateMachineConfigStoreServer::new(
                services.clone(),
            ),
        )
        .add_service(service::session_server::SessionServer::new(services.clone()))
        .add_service(service::recording_server::RecordingServer::new(services.clone()))
        .add_service(service::configuration_server::ConfigurationServer::new(services));

    // Both reflection versions: clients disagree about which to ask for, and
    // grpcurl and Python's reflection database still want v1alpha.
    let reflection = tonic_reflection::server::Builder::configure()
        .register_encoded_file_descriptor_set(wire::DESCRIPTOR)
        .build_v1()
        .expect("the descriptor set this binary was built from");
    let reflection_alpha = tonic_reflection::server::Builder::configure()
        .register_encoded_file_descriptor_set(wire::DESCRIPTOR)
        .build_v1alpha()
        .expect("the descriptor set this binary was built from");
    routes.add_service(reflection).add_service(reflection_alpha);

    // **CORS is open, and `/elements/` is why.** A console served from
    // somewhere else imports these panels by URL and calls this daemon from its
    // own origin; that is the contract, not an accident.
    // `permissive` and not `very_permissive`: the latter allows credentials,
    // which cannot be combined with a wildcard in `expose-headers` — and
    // exposing the headers is the point, because `grpc-status` and
    // `grpc-message` are how a refusal reaches a panel across an origin.
    let cors = tower_http::cors::CorsLayer::permissive();

    // gRPC-Web on the whole stack rather than per service: the layer only acts
    // on requests that arrive with a gRPC-Web content type, so the panels pass
    // through it untouched. **This is the replacement for `web_edge.py`** — 400
    // lines of hand-written protocol, for one layer.
    let app = web::router()
        .merge(routes.routes().into_axum_router().layer(tonic_web::GrpcWebLayer::new()))
        .layer(cors);

    // With the peer's address, which tonic's own server would provide and
    // axum only does when asked: `ReadObservers` names who is watching by it.
    // The Python daemon's gRPC port, answered by the same app — see
    // `serve_on_the_next_port_too`. A bind failure there is reported and not
    // fatal: the one port that is this daemon's own is already serving.
    let next = if arguments.serve_on_the_next_port_too {
        let address = format!("{}:{}", arguments.bind, arguments.port.saturating_add(1));
        match tokio::net::TcpListener::bind(&address).await {
            Ok(listener) => {
                log::info!("and on {address}, where the Python daemon served gRPC");
                Some(listener)
            }
            Err(problem) => {
                log::warn!("not serving on {address} as well: {problem}");
                None
            }
        }
    } else {
        None
    };

    let stopping = tokio::sync::watch::channel(false);
    let serve = |listener: tokio::net::TcpListener| {
        let app = app.clone().into_make_service_with_connect_info::<std::net::SocketAddr>();
        let mut stop = stopping.1.clone();
        async move {
            axum::serve(listener, app)
                .with_graceful_shutdown(async move {
                    let _ = stop.changed().await;
                })
                .await
        }
    };
    // Advertised once the port is bound, so a console that finds the record
    // finds a daemon; withdrawn before the process ends.
    let mut advertisement = (!arguments.no_mdns).then(|| {
        let mut advertisement = statemachined::mdns_service_advertisement::MdnsServiceAdvertisement::new(
            arguments.port,
            &state.configuration().device_target,
            env!("CARGO_PKG_VERSION"),
        );
        advertisement.start();
        advertisement
    });

    let mut main = tokio::spawn(serve(listener));
    let second = next.map(|listener| tokio::spawn(serve(listener)));
    tokio::select! {
        _ = tokio::signal::ctrl_c() => log::info!("statemachined: stopping"),
        // The daemon's own port failing is the daemon failing, as it was when
        // there was only one.
        finished = &mut main => {
            if let Ok(Err(problem)) = finished {
                eprintln!("statemachined: {problem}");
            }
            std::process::exit(1);
        }
    }
    if let Some(advertisement) = advertisement.as_mut() {
        advertisement.stop();
    }
    let _ = stopping.0.send(true);
    let _ = main.await;
    if let Some(second) = second {
        let _ = second.await;
    }
}
