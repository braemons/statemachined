// SPDX-License-Identifier: AGPL-3.0-or-later
//! The firmware, on this machine, reachable at a TCP address.
//!
//! `statemachined_native_device` is the firmware's own session and engine built
//! for the host (`firmware/native/`). It takes its link on stdin and stdout, and
//! the daemon opens a *target*, so something has to sit between the two. This is
//! that something: a listening socket pumped to the child's pipes, so a daemon
//! pointed at `127.0.0.1:5300` talks to the real protocol with no board on the
//! desk.
//!
//! A socket is not a test-only contrivance -- it is how a daemon reaches an
//! ethernet-attached MCU, which the serial link makes indistinguishable from a
//! cable. So the transport under the bench is the transport the daemon ships.
//!
//! `statemachined device` runs it for an operator on a fresh install and for the
//! family's e2e suite; this daemon's own tests start it in-process.

use std::io::{self, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};

/// An explicit override: a checkout with an unusual build directory, or a
/// harness that knows where it put the binary.
pub const BINARY_ENVIRONMENT_VARIABLE: &str = "STATEMACHINED_NATIVE_DEVICE";

/// The name the package installs the binary under, beside this one's prefix.
pub const INSTALLED_BINARY_NAME: &str = "statemachined-device";

/// Where a bench listens when nobody says otherwise, so the URL can be written
/// down.
pub const DEFAULT_BENCH_PORT: u16 = 5300;

/// Everywhere the device could be, nearest first.
///
/// An installed daemon has the binary under its own prefix; a checkout has
/// `build/`; somebody who built elsewhere says so in the environment.
pub fn candidate_paths() -> Vec<PathBuf> {
    if let Some(named) = std::env::var_os(BINARY_ENVIRONMENT_VARIABLE) {
        return vec![PathBuf::from(named)];
    }
    let mut candidates = Vec::new();
    // /opt/braemons/statemachined/{bin/statemachined, libexec/statemachined-device}.
    if let Ok(executable) = std::env::current_exe() {
        if let Some(prefix) = executable.parent().and_then(Path::parent) {
            candidates.push(prefix.join("libexec").join(INSTALLED_BINARY_NAME));
        }
    }
    // A checkout: `make integration-device` writes into build/.
    candidates.push(PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../build/statemachined_native_device"));
    if let Some(path) = std::env::var_os("PATH") {
        for directory in std::env::split_paths(&path) {
            candidates.push(directory.join(INSTALLED_BINARY_NAME));
        }
    }
    candidates
}

/// The first candidate that exists, or a sentence saying where it looked.
pub fn the_native_device_binary() -> Result<PathBuf, String> {
    let candidates = candidate_paths();
    if let Some(found) = candidates.iter().find(|candidate| candidate.is_file()) {
        return Ok(found.clone());
    }
    let looked_in: Vec<String> = candidates
        .iter()
        .map(|candidate| format!("    {}", candidate.display()))
        .collect();
    Err(format!(
        "The native device binary is not here. Looked in:\n{}\n  From a checkout, build it \
         with `make integration-device`.\n  From a package, it ships at \
         <prefix>/libexec/{INSTALLED_BINARY_NAME}.\n  Or name it in ${BINARY_ENVIRONMENT_VARIABLE}.",
        looked_in.join("\n")
    ))
}

/// What the device is started with, beyond the binary.
#[derive(Debug, Clone, Default)]
pub struct NativeDeviceOptions {
    /// Where the device keeps what it remembers across a restart -- its data
    /// flash, in effect. Two devices started on one path are one board before
    /// and after a power cut.
    pub store_path: Option<PathBuf>,
    /// Outputs wired back to inputs in software: `"8"` for output line n on
    /// input line (n + 4) mod 8, or `"<width>:<shift>"`. The loopback harness of
    /// docs/operations/hardware.md with no jumper wires in it.
    pub loopback: Option<String>,
}

/// One child process, reachable at a TCP address, until dropped.
///
/// Two pumps, because the child's stdin and stdout are separate pipes and
/// either may block: one loop would deadlock the first time the device wrote a
/// result while the host was still sending an upload.
pub struct NativeDeviceOnASocket {
    child: Child,
    pub port: u16,
    connection: Arc<Mutex<Option<TcpStream>>>,
}

impl NativeDeviceOnASocket {
    /// Start the device and listen. `port` 0 asks the kernel for a free one.
    pub fn start(port: u16, options: &NativeDeviceOptions) -> Result<Self, String> {
        let binary = the_native_device_binary()?;
        let listener =
            TcpListener::bind(("127.0.0.1", port)).map_err(|problem| format!("port {port}: {problem}"))?;
        let port = listener.local_addr().map_err(|problem| problem.to_string())?.port();
        let mut command = Command::new(&binary);
        command.stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::null());
        if let Some(store) = &options.store_path {
            command.env("STATEMACHINED_STORE", store);
        }
        if let Some(loopback) = &options.loopback {
            command.env("STATEMACHINED_LOOPBACK", loopback);
        }
        // The device dies with whoever started it. Without this a daemon or a
        // test harness that was killed rather than asked to stop left a device
        // behind on its port for every run -- which the Python bridge did.
        // SAFETY: prctl is async-signal-safe, and is all that runs between
        // fork and exec here.
        unsafe {
            use std::os::unix::process::CommandExt;
            command.pre_exec(|| {
                if libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGKILL) != 0 {
                    return Err(io::Error::last_os_error());
                }
                Ok(())
            });
        }
        let mut child = command
            .spawn()
            .map_err(|problem| format!("{}: {problem}", binary.display()))?;
        let to_device = child.stdin.take().expect("a piped stdin");
        let from_device = child.stdout.take().expect("a piped stdout");
        let connection: Arc<Mutex<Option<TcpStream>>> = Arc::new(Mutex::new(None));

        // One permanent reader of the device's output. What it says with nobody
        // connected goes nowhere, which is what link loss *is*.
        let shared = Arc::clone(&connection);
        std::thread::spawn(move || pump_device_into_whatever_is_connected(from_device, shared));
        let shared = Arc::clone(&connection);
        std::thread::spawn(move || accept_connections_forever(listener, to_device, shared));

        Ok(Self {
            child,
            port,
            connection,
        })
    }

    pub fn target(&self) -> String {
        format!("socket://127.0.0.1:{}", self.port)
    }

    /// Close the socket under the daemon, the way a USB port closing does. The
    /// device keeps running, with its committed set.
    pub fn drop_the_link(&self) {
        if let Some(stream) = self.connection.lock().expect("not poisoned").take() {
            let _ = stream.shutdown(std::net::Shutdown::Both);
        }
    }
}

impl Drop for NativeDeviceOnASocket {
    fn drop(&mut self) {
        self.drop_the_link();
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

/// One connection at a time, and then the next: a reconnect is one of the
/// things worth exercising, and the device it comes back to is the same one.
fn accept_connections_forever(
    listener: TcpListener,
    mut to_device: impl Write,
    connection: Arc<Mutex<Option<TcpStream>>>,
) {
    for stream in listener.incoming() {
        let Ok(stream) = stream else { return };
        let _ = stream.set_nodelay(true);
        let Ok(mut inbound) = stream.try_clone() else { continue };
        *connection.lock().expect("not poisoned") = Some(stream);
        // A plain loop rather than `io::copy`, which on Linux may splice a
        // socket into a pipe in the kernel and hold bytes the device waits for.
        let mut buffer = [0u8; 4096];
        loop {
            match inbound.read(&mut buffer) {
                Ok(0) | Err(_) => break,
                Ok(read) => {
                    if to_device.write_all(&buffer[..read]).and_then(|()| to_device.flush()).is_err() {
                        return;
                    }
                }
            }
        }
        connection.lock().expect("not poisoned").take();
    }
}

fn pump_device_into_whatever_is_connected(
    mut from_device: impl Read,
    connection: Arc<Mutex<Option<TcpStream>>>,
) {
    let mut buffer = [0u8; 4096];
    loop {
        let read = match from_device.read(&mut buffer) {
            Ok(0) | Err(_) => return,
            Ok(read) => read,
        };
        if let Some(stream) = connection.lock().expect("not poisoned").as_mut() {
            let _ = stream.write_all(&buffer[..read]);
        }
    }
}

/// `statemachined device`: run it until interrupted.
pub fn run_until_interrupted(port: u16, options: &NativeDeviceOptions) -> io::Result<()> {
    let device = NativeDeviceOnASocket::start(port, options)
        .map_err(|problem| io::Error::new(io::ErrorKind::NotFound, problem))?;
    println!("native device on {} (ctrl-c to stop)", device.target());
    // Parked until a signal ends the process; Drop is not reached then, and the
    // child goes with its parent's process group.
    loop {
        std::thread::park();
    }
}
