// SPDX-License-Identifier: AGPL-3.0-or-later
//! The transport: one newline-delimited line in, one out.
//!
//! USB CDC is what the reference board offers, but **nothing in the protocol is
//! serial** — it is lines of ASCII with a CRC, which is as true of a TCP socket
//! to an ethernet-attached MCU as it is of a tty. So the target is a URL and the
//! layers above never learn which they got.
//!
//! Three transports, which is what the Python daemon gets from pyserial's
//! `serial_for_url`:
//!
//! * a device path — a real board on USB CDC
//! * `socket://host:port` — the native device, or an MCU on a switch
//! * `loop://` — a loopback that reads back what was written, which is how a
//!   daemon runs with no board at all
//!
//! Raw mode is not optional for the tty. A port in canonical mode buffers by
//! line, echoes what it receives and translates `\r`; on a link whose messages
//! are newline-delimited that is three ways to corrupt a message.
//!
//! **The buffering is a correctness fix, not a performance one.** pyserial's
//! `readline()` returns whatever it has when the timeout expires, newline or
//! not, so a line that straddled a timeout arrived in halves and both halves
//! failed their CRC. It never bit at a two-second timeout and a board that
//! writes a line in microseconds; it bites immediately once anything polls the
//! link on a short timeout, which is what a daemon that must stay responsive
//! between commands has to do. This reader has the same shape for the same
//! reason: whole lines only, and a partial one stays in the buffer.

use std::collections::VecDeque;
use std::io::{self, Read, Write};
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
use std::time::{Duration, Instant};

pub const DEFAULT_TARGET: &str = "/dev/ttyACM0";
pub const DEFAULT_BAUD: u32 = 115_200;
pub const DEFAULT_TIMEOUT: Duration = Duration::from_secs(2);

/// Which way a line crossed the link, as the serial monitor labels it.
pub const TO_DEVICE: &str = "to_device";
pub const FROM_DEVICE: &str = "from_device";

/// What a target names.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Target {
    /// A tty, opened and put in raw mode.
    Port(String),
    /// A TCP socket: the native device, or a board on a switch.
    Socket(String),
    /// No board. Writes come back as reads, which is what `loop://` is for.
    Loopback,
}

/// Turn what somebody typed into a target.
///
/// A device path stays a device path; `host:port` becomes a socket so the
/// ethernet case needs no scheme from a person's fingers; anything with a
/// scheme is taken as it is.
pub fn to_target(target: &str) -> Target {
    if let Some(rest) = target.strip_prefix("socket://") {
        return Target::Socket(rest.to_string());
    }
    if target.starts_with("loop://") {
        return Target::Loopback;
    }
    if target.contains("://") {
        // `rfc2217://` and friends. Not supported here yet, and named as a
        // port so the failure is "cannot open that" rather than a silent
        // fallback to something else.
        return Target::Port(target.to_string());
    }
    if is_host_and_port(target) {
        return Target::Socket(target.to_string());
    }
    Target::Port(target.to_string())
}

/// `host:port`, with a bracketed IPv6 literal allowed.
fn is_host_and_port(target: &str) -> bool {
    let Some((host, port)) = target.rsplit_once(':') else {
        return false;
    };
    if host.is_empty() || port.is_empty() || !port.bytes().all(|b| b.is_ascii_digit()) {
        return false;
    }
    if host.starts_with('[') && host.ends_with(']') {
        return host[1..host.len() - 1]
            .bytes()
            .all(|b| b.is_ascii_hexdigit() || b == b':');
    }
    host.bytes()
        .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'.' | b'-'))
}

/// Watches every line that crosses the link, in both directions.
pub type LineObserver = Box<dyn FnMut(&str, &str) + Send>;

enum Channel {
    File(std::fs::File),
    Socket(std::net::TcpStream),
    /// Lines written, waiting to be read back.
    Loopback(VecDeque<u8>),
}

/// A line channel to exactly one device.
pub struct SerialLink {
    channel: Channel,
    timeout: Duration,
    /// Whatever has arrived and is not yet a whole line.
    receive_buffer: Vec<u8>,
    /// Called with `(direction, line)` for every whole line that crosses.
    ///
    /// **Here rather than in the session above**, because a monitor that only
    /// saw what the session understood would miss exactly what somebody opens a
    /// monitor for: the junk, the reply to a command that had already timed
    /// out, the line with the bad CRC.
    observer: Option<LineObserver>,
}

impl SerialLink {
    pub fn open(target: &str, baud: u32, timeout: Duration) -> io::Result<Self> {
        let channel = match to_target(target) {
            Target::Port(path) => Channel::File(std::fs::File::from(open_port(&path, baud)?)),
            Target::Socket(address) => {
                let stream = std::net::TcpStream::connect(&address)?;
                stream.set_read_timeout(Some(timeout))?;
                stream.set_nodelay(true)?;
                Channel::Socket(stream)
            }
            Target::Loopback => Channel::Loopback(VecDeque::new()),
        };
        Ok(Self {
            channel,
            timeout,
            receive_buffer: Vec::new(),
            observer: None,
        })
    }

    /// Watch every line that crosses, in both directions.
    pub fn observe(&mut self, observer: impl FnMut(&str, &str) + Send + 'static) {
        self.observer = Some(Box::new(observer));
    }

    pub fn write_line(&mut self, line: &str) -> io::Result<()> {
        let bytes = format!("{line}\n");
        match &mut self.channel {
            Channel::File(file) => {
                file.write_all(bytes.as_bytes())?;
                file.flush()?;
            }
            Channel::Socket(stream) => {
                stream.write_all(bytes.as_bytes())?;
                stream.flush()?;
            }
            Channel::Loopback(pending) => pending.extend(bytes.as_bytes()),
        }
        self.observed(TO_DEVICE, line);
        Ok(())
    }

    /// One **complete** line, or `None` if none arrived before the timeout.
    ///
    /// `timeout` overrides the link's for this call. A daemon polling between
    /// commands wants tens of milliseconds where a command waiting for its reply
    /// wants seconds, and the difference matters: a poll that blocked for the
    /// command timeout would hold the device lock for that long and make every
    /// request wait behind it.
    ///
    /// **A timeout is not an error.** Sitting quiet is what a healthy device
    /// does between replies, and a serial monitor spends its whole life doing
    /// it.
    pub fn read_line(&mut self, timeout: Option<Duration>) -> io::Result<Option<String>> {
        let effective = timeout.unwrap_or(self.timeout);
        let deadline = Instant::now() + effective;
        loop {
            if let Some(line) = self.take_buffered_line() {
                self.observed(FROM_DEVICE, &line);
                return Ok(Some(line));
            }
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                return Ok(None);
            }
            if !self.fill(remaining)? {
                return Ok(None);
            }
        }
    }

    /// A whole line out of the buffer, leaving a partial one behind.
    fn take_buffered_line(&mut self) -> Option<String> {
        let at = self.receive_buffer.iter().position(|byte| *byte == b'\n')?;
        let line: Vec<u8> = self.receive_buffer.drain(..=at).collect();
        let text = String::from_utf8_lossy(&line[..at]);
        Some(text.trim_end_matches('\r').to_string())
    }

    /// Read whatever is there, waiting at most `remaining`. True if the caller
    /// should look again; false only when the loopback has nothing.
    ///
    /// **End of file is an error, not a quiet link.** A socket whose far end
    /// closed, or a tty whose device was unplugged, reads nothing for ever;
    /// taken for silence, the daemon sat on a dead link until a request timed
    /// out and never wrote down that it was lost. The errors are pyserial's,
    /// word for word, because the daemon records them.
    fn fill(&mut self, remaining: Duration) -> io::Result<bool> {
        let mut chunk = [0u8; 4096];
        let read = match &mut self.channel {
            Channel::Loopback(pending) => {
                let taken = pending.len().min(chunk.len());
                for (at, byte) in pending.drain(..taken).enumerate() {
                    chunk[at] = byte;
                }
                if taken == 0 {
                    // Nothing was written, so nothing will arrive. Waiting out
                    // the deadline is what a real quiet link does, and is what
                    // keeps a caller's timeout meaning the same thing here.
                    std::thread::sleep(remaining);
                }
                taken
            }
            Channel::File(file) => {
                if !wait_readable(file.as_raw_fd(), remaining)? {
                    return Ok(true); // the deadline is the caller's to notice
                }
                file.read(&mut chunk)?
            }
            Channel::Socket(stream) => {
                stream.set_read_timeout(Some(remaining))?;
                match stream.read(&mut chunk) {
                    Ok(read) => read,
                    Err(problem)
                        if matches!(
                            problem.kind(),
                            io::ErrorKind::WouldBlock | io::ErrorKind::TimedOut
                        ) =>
                    {
                        return Ok(true)
                    }
                    Err(problem) => return Err(problem),
                }
            }
        };
        if read == 0 {
            return match &self.channel {
                Channel::Loopback(_) => Ok(false),
                Channel::Socket(_) => Err(io::Error::new(
                    io::ErrorKind::ConnectionAborted,
                    "read failed: socket disconnected",
                )),
                Channel::File(_) => Err(io::Error::new(
                    io::ErrorKind::BrokenPipe,
                    "device reports readiness to read but returned no data (device \
                     disconnected or multiple access on port?)",
                )),
            };
        }
        self.receive_buffer.extend_from_slice(&chunk[..read]);
        Ok(true)
    }

    fn observed(&mut self, direction: &str, line: &str) {
        if let Some(observer) = self.observer.as_mut() {
            observer(direction, line);
        }
    }

    /// Drop whatever is already buffered.
    ///
    /// A board that has been running on its own has been talking to nobody, and
    /// a serial monitor left open earlier may have left a partial line in the
    /// driver; starting a session on top of that produces one spurious framing
    /// complaint.
    pub fn reset_input(&mut self) {
        self.receive_buffer.clear();
        if let Channel::File(file) = &self.channel {
            // SAFETY: a descriptor this link owns. TCIFLUSH discards what has
            // arrived and not been read. Not every transport has one, and
            // nothing depends on it.
            unsafe { libc::tcflush(file.as_raw_fd(), libc::TCIFLUSH) };
        }
    }
}

/// Wait for a descriptor to have something, or for the deadline.
fn wait_readable(fd: i32, remaining: Duration) -> io::Result<bool> {
    let mut polled = libc::pollfd {
        fd,
        events: libc::POLLIN,
        revents: 0,
    };
    let milliseconds = remaining.as_millis().min(i32::MAX as u128) as i32;
    // SAFETY: one initialised pollfd, and a count that matches.
    let ready = unsafe { libc::poll(&mut polled, 1, milliseconds) };
    if ready < 0 {
        let problem = io::Error::last_os_error();
        // A signal is not a failure of the link; the caller's deadline still
        // governs, and the loop above will come back here.
        if problem.kind() == io::ErrorKind::Interrupted {
            return Ok(false);
        }
        return Err(problem);
    }
    Ok(ready > 0)
}

/// Open a serial port and put it in raw mode.
fn open_port(path: &str, baud: u32) -> io::Result<OwnedFd> {
    let c_path = std::ffi::CString::new(path)
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, "a NUL in the port name"))?;
    // SAFETY: a valid C string. O_NOCTTY keeps this process from acquiring the
    // port as its controlling terminal, which would route signals through it.
    let fd = unsafe { libc::open(c_path.as_ptr(), libc::O_RDWR | libc::O_NOCTTY) };
    if fd < 0 {
        return Err(io::Error::last_os_error());
    }
    // SAFETY: `open` returned an owned descriptor.
    let fd = unsafe { OwnedFd::from_raw_fd(fd) };
    make_raw(&fd, baud)?;
    Ok(fd)
}

fn make_raw(fd: &OwnedFd, baud: u32) -> io::Result<()> {
    // SAFETY: a zeroed termios is a valid starting point for tcgetattr to fill.
    let mut settings: libc::termios = unsafe { std::mem::zeroed() };
    // SAFETY: an owned descriptor and a struct to fill.
    if unsafe { libc::tcgetattr(fd.as_raw_fd(), &mut settings) } != 0 {
        // Not a tty -- a pty under test, or a file. Nothing to set, and not an
        // error: the caller wanted a line channel and has one.
        return Ok(());
    }
    // SAFETY: the struct tcgetattr just filled.
    unsafe { libc::cfmakeraw(&mut settings) };
    settings.c_cflag |= libc::CLOCAL | libc::CREAD;
    if let Some(speed) = termios_speed(baud) {
        // SAFETY: a filled termios and a speed constant.
        unsafe {
            libc::cfsetispeed(&mut settings, speed);
            libc::cfsetospeed(&mut settings, speed);
        }
    }
    // SAFETY: an owned descriptor and a filled struct.
    if unsafe { libc::tcsetattr(fd.as_raw_fd(), libc::TCSANOW, &settings) } != 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

fn termios_speed(baud: u32) -> Option<libc::speed_t> {
    Some(match baud {
        9600 => libc::B9600,
        19200 => libc::B19200,
        38400 => libc::B38400,
        57600 => libc::B57600,
        115_200 => libc::B115200,
        230_400 => libc::B230400,
        460_800 => libc::B460800,
        921_600 => libc::B921600,
        // USB CDC ignores the line speed entirely, so an unusual one is not
        // worth refusing a board over.
        _ => return None,
    })
}
