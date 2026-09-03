# Emulation

The reference board under [Renode](https://renode.io/), so `firmware/hal/` has
tests.

```sh
make emulate          # in the devcontainer; builds the SCI firmware and runs the suite
```

## What this is for

Everything above the HAL is tested on the host by `tests/core`, far better and
far faster than anything here could be. What is left over is precisely the code
the host build **cannot compile**, and until this existed it was covered by
nothing:

- **the pin map** — that statemachined input line 0 is the pin somebody wired to D2. That
  arithmetic runs only on the board, and getting it wrong means a lever press
  arriving as a lick.
- **the port-register access** — one `PCNTR2` read per port and a bit gather in,
  `PCNTR3` set-and-reset out.
- **the timer ISR** — that `FspTimer` fires, that `loop()` consumes ticks, and
  that a trial can therefore end on its own timeout.
- **the protocol over a real UART peripheral**, rather than over a `std::string`.

It has already earned its keep: it found that `HostLinkSession::on_start()`
discarded the entry state's output actions, so "house light on at trial start"
did nothing at all on a board. Every host test passed, because they call
`TrialRunner::start()` and read the update themselves — nothing asked whether
the session passed it on.

## What this is **not** for

**Timing.** Renode runs on virtual time against a nominal MIPS figure, so a scan
here takes exactly as long as we tell it to and the number means nothing. The
10 kHz claim and the RAM high-water mark both need a board. See
`dev/HARDWARE.md`.

Nothing in this directory should ever grow an assertion about microseconds.

## How it is put together

| | |
|---|---|
| `statemachined-uno-r4.repl` | Renode's own `arduino_uno_r4_minima.repl`, plus a USB boot shim |
| `statemachined.resc` | loads the platform and the ELF |
| `tests/statemachined.robot` | the suite |
| `tests/statemachined_protocol.py` | the wire protocol, as Robot keywords |

It runs the **`uno_r4_minima_sci`** build, where the host is on SCI2 (D0/D1)
rather than USB CDC. tinyusb against an emulated `USBFS` is by far the most
fragile thing in the picture, and the point is to test our code. That build is
independently useful for a rig that wants a hardware serial bridge, so it is not
a test-only artefact.

`tests/statemachined_protocol.py` is deliberately a **second implementation** of the
framing rules, written from `dev/PROTOCOL.md` rather than bound to
`firmware/core/protocol/`. If both ends were the same code, these tests could
only prove the device agreed with itself.

## Three things that cost an afternoon

Written down so nobody re-treads them.

**Renode's `Write Line To Uart` does not reach this firmware.** The terminal
tester's *read* side works fine; its *write* side silently delivers nothing.
Bytes go in through `sysbus.sci2 WriteChar` instead, which does work. The
symptom is a device that is demonstrably running and replying to a byte poked in
by hand, while the test sees an empty UART.

**A monitor command runs with the machine paused**, so `WriteChar` in a loop
delivers a whole line into a receive FIFO that the CPU never gets to drain.
Every byte past the FIFO is lost, and a Robot-side `Sleep` does not help because
it does not advance *virtual* time. Each byte is followed by an explicit
`emulation RunFor`. The symptom is a `bad_crc` reply to a line that was built
correctly — or, for a longer line, `line is not a JSON object`.

**`emulation RunFor` requires a paused emulation**, hence the `Advance` keyword,
which brackets it with `pause`/`start`.

**The firmware hangs before `main()` without the USB shim.** Renode's RA4M1
description *tags* the USB module rather than modelling it, so every read
returns zero; the Arduino core initialises USB before it reaches `setup()`
whatever the sketch does, and tinyusb's `dcd_init()` sets the clock-enable bit
in `SYSCFG` and spins until it reads back as one. Plain RAM at `0x40090000` is
enough — the write sticks, the read-back satisfies the wait, and the rest of
`dcd_init()` finds its status bits clear and returns.

## Renode outside the container

The devcontainer pins Renode 1.16.1 and installs Renode's own
`tests/requirements.txt` for `renode-test`. If you are running on the host
instead, match that version: the Robot keyword library ships with Renode, so a
different Renode is a different set of keywords.
