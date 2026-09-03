*** Settings ***
Documentation     fsmd on an emulated Uno R4 Minima.
...
...               These tests cover the code the host build physically cannot
...               compile: the pin map, the port-register arithmetic, the timer
...               ISR, and the protocol over a real UART peripheral. Everything
...               above the HAL is tested far better by tests/core on the host,
...               and is not re-tested here.
...
...               Nothing here measures time. Renode runs on virtual time against
...               a nominal MIPS figure, so a scan takes exactly as long as we
...               tell it to. The 10 kHz claim needs a board -- dev/HARDWARE.md.

Library           ${CURDIR}/fsmd_protocol.py
Resource          ${RENODEKEYWORDS}
Suite Setup       Setup
Suite Teardown    Teardown
Test Setup        Boot Fsmd
Test Teardown     Test Teardown

*** Variables ***
${ELF}            ${CURDIR}/../../.pio/build/uno_r4_minima_sci/firmware.elf
# port1 PCNTR1: PDR in the low half, PODR -- the output levels -- in the high.
${PORT1_PCNTR1}   0x40040020
${PORT0_PCNTR1}   0x40040000

*** Keywords ***
Boot Fsmd
    Execute Command           $elf = @${ELF}
    Execute Script            ${CURDIR}/../fsmd.resc
    # defaultPauseEmulation is what makes this suite deterministic rather than
    # merely usually-green. With it, the machine advances ONLY where a test says
    # so -- an explicit RunFor, or a wait that stops the moment it matches -- so
    # nothing depends on how fast the host is relative to the emulation.
    #
    # Without it the machine free-runs between steps, and how much firmware
    # executes between two assertions becomes a property of the host's load.
    # That is not a hypothetical: this suite passed on a laptop, failed on a CI
    # runner, and then failed *differently* under `docker run --cpus=0.5`.
    Create Terminal Tester    sysbus.sci2    timeout=30    defaultPauseEmulation=true
    Wait For The Link To Be Serviced

Wait For The Link To Be Serviced
    [Documentation]    Let the board finish booting before pushing bytes at it.
    ...
    ...                The link is open from hal::init(), but nothing drains it
    ...                until loop() runs, and setup() spends a while between the
    ...                two -- it measures its own scan floor and zeroes an 8.9 kB
    ...                session. Bytes arriving in that window overflow the SCI
    ...                receive FIFO however carefully they are paced, because
    ...                pacing does not help a CPU that is busy elsewhere.
    ...
    ...                This is measured in *virtual* time, not wall time, which
    ...                is the whole point: a wall-clock sleep is a different
    ...                amount of firmware on a laptop and on a CI runner, and
    ...                that is exactly how this got through review and failed on
    ...                GitHub. The window that bit us was ~36 ms of virtual time;
    ...                this is an order of magnitude past it.
    ...
    ...                A real bridge has the same problem and the protocol
    ...                already answers it: a command that draws no reply is
    ...                resent. See dev/PROTOCOL.md 3.1.
    Advance    0.5

Send And Expect
    [Documentation]    Send one command and wait for the reply it must draw,
    ...                resending if none arrives.
    ...
    ...                The retry is not harness sugar covering a flaky test. It
    ...                is what dev/PROTOCOL.md 3.1 requires of a bridge -- a
    ...                command that draws no reply is resent -- and it is safe
    ...                because a repeated `seq` is answered from
    ...                DuplicateCommandGuard rather than acted on twice. A
    ...                harness that could not do what the protocol demands of a
    ...                real bridge would be testing a rig nobody will build, and
    ...                this way the retry path is exercised on every run instead
    ...                of only when something goes wrong.
    [Arguments]    ${body}    ${pattern}
    Wait Until Keyword Succeeds    3x    0s    Send Once And Match    ${body}    ${pattern}

Send Once And Match
    [Arguments]    ${body}    ${pattern}
    Send Fsmd                 ${body}
    Wait For Line On Uart     ${pattern}    treatAsRegex=true    timeout=8

Send Fsmd
    [Documentation]    One protocol line into SCI2, paced so the receive FIFO
    ...                drains between bytes.
    ...
    ...                Two things here are not obvious and both cost an
    ...                afternoon. Renode's own `Write Line To Uart` delivers
    ...                nothing to this firmware -- the tester's read side works,
    ...                its write side does not -- so bytes go in through
    ...                `WriteChar`. And a monitor command runs with the machine
    ...                paused, so without an explicit `RunFor` between bytes the
    ...                CPU never gets to drain the FIFO and a long line arrives
    ...                truncated. The symptom is a bad_crc reply to a line that
    ...                was built correctly.
    [Arguments]    ${body}
    @{codes}=      Line Codes    ${body}
    # Explicitly, every time. A wait's pause-on-match is asynchronous, so under
    # load the machine can still be running when the next keyword starts -- and
    # bytes written to a running machine race the firmware's own polling. That
    # is what a bad_crc reply to a correctly built line means, and it is a race
    # that only shows up on a loaded host.
    Execute Command    pause
    FOR    ${c}    IN    @{codes}
        Execute Command    sysbus.sci2 WriteChar ${c}
        Execute Command    emulation RunFor "0.0005"
    END
    # Injection happens with the machine paused, so the FIFO cannot overflow.
    # Running it again is what lets the reply be produced; the wait that follows
    # stops the machine the instant it matches, so whatever a test asserts next
    # is asserted against a machine standing still.
    Execute Command    start

Advance
    [Documentation]    Run the machine for a fixed span of virtual time, then
    ...                leave it paused. Virtual, not wall: this is the same
    ...                amount of firmware on any host.
    [Arguments]    ${seconds}
    Execute Command    pause
    Execute Command    emulation RunFor "${seconds}"

Greet
    Send And Expect           {"t":"hello","seq":1,"proto":1,"seed":"0123456789ABCDEF"    "t":"hello_ack".*"board":"uno_r4_minima"

Upload Reward Graph
    [Documentation]    wait --(${ms} ms)--> Hit, holding output line 0 high for
    ...                the whole of the wait state. Output line 0 is D10, which
    ...                is P112: port1, pin 12.
    [Arguments]    ${ms}
    @{bodies}=    Create List
    ...    {"t":"graph_begin","seq":10,"graph_version":1,"n_states":2,"entry":0
    ...    {"t":"graph_dist","seq":11,"i":0,"kind":"fixed","a":${ms}
    ...    {"t":"graph_state","seq":12,"i":0,"terminal":null,"timeout":{"dist":0,"target":1}
    ...    {"t":"graph_action","seq":13,"on":"entry","line":0,"kind":"high"
    ...    {"t":"graph_state","seq":14,"i":1,"terminal":1,"timeout":null
    FOR    ${b}    IN    @{bodies}
        Send And Expect           ${b}    "t":"ack"
    END
    ${sum}=    Graph Checksum      ${bodies}
    Send And Expect    {"t":"graph_end","seq":15,"n_transitions":0,"n_output_actions":1,"checksum":"${sum}"    "t":"graph_ok"

*** Test Cases ***
The Firmware Boots And Answers On Real Peripherals
    [Documentation]    The narrowest question, and the one everything else rests
    ...                on: does this binary get through the Renesas clock and USB
    ...                bring-up, reach loop(), and put a line back out of SCI2.
    Greet

An Input Pin Reaches The Line Number A Graph Would Name
    [Documentation]    The test this whole emulator exists for. Nothing on the
    ...                host can check that fsmd input line 0 is the pin somebody
    ...                wired to D2 -- that arithmetic only runs on the board, and
    ...                getting it wrong means a lever press arriving as a lick.
    ...
    ...                fsmd input line 0 is D2, which is P105: port1, pin 5.
    ...                Line 6 is D8, which is P304: port3, pin 4 -- a different
    ...                port on purpose, because a HAL that reads one port
    ...                register and gathers every line out of it would pass a
    ...                single-port test and be wrong.
    Greet
    Execute Command           sysbus.port1 OnGPIO 5 true
    Send And Expect           {"t":"state","seq":2    "io":{"in":1,

    Execute Command           sysbus.port3 OnGPIO 4 true
    Send And Expect           {"t":"state","seq":3    "io":{"in":65,

    Execute Command           sysbus.port1 OnGPIO 5 false
    Send And Expect           {"t":"state","seq":4    "io":{"in":64,

An Output Action Reaches A Real Pin
    [Documentation]    The other half, and it is checked at the port register
    ...                rather than by asking the device -- `io.out` is the
    ...                engine's own shadow, so believing it here would be the
    ...                device marking its own homework.
    ...
    ...                Output line 0 is D10, which is P112: port1, pin 12. PODR
    ...                sits in the top half of PCNTR1, so pin 12 is bit 28.
    Greet
    Upload Reward Graph       9000
    Send And Expect           {"t":"configure","seq":20,"trial_id":1,"graph_version":1    "t":"armed"

    ${before}=    Execute Command    sysbus ReadDoubleWord ${PORT1_PCNTR1}
    Should Not Match Regexp   ${before}    (?i)0x1[0-9a-f]{7}

    Send And Expect           {"t":"start","seq":21,"trial_id":1    "t":"started"
    Advance                   0.05
    ${during}=    Execute Command    sysbus ReadDoubleWord ${PORT1_PCNTR1}
    Should Match Regexp       ${during}    (?i)0x1[0-9a-f]{7}

A Whole Trial Runs On The Board's Own Timer
    [Documentation]    End to end, and the only test that proves the scan is
    ...                actually being driven: nothing here touches an input, so
    ...                the trial can only end by its timeout expiring, which
    ...                means FspTimer fired, the ISR counted, loop() scanned, and
    ...                micros() advanced. Terminal code 1 is HIT.
    Greet
    Upload Reward Graph       50
    Send And Expect           {"t":"configure","seq":20,"trial_id":7,"graph_version":1    "t":"armed"
    Send And Expect           {"t":"start","seq":21,"trial_id":7    "t":"started"
    Wait For Line On Uart     "t":"result_begin".*"trial_id":7.*"outcome":1    treatAsRegex=true
    Wait For Line On Uart     "t":"result_end"    treatAsRegex=true

The Board Measures Its Own Scan Rate At Boot
    [Documentation]    Not a timing assertion -- Renode's virtual time makes the
    ...                number meaningless. What is being checked is that the
    ...                measurement ran and produced something, so that the field
    ...                is not silently zero on a real board.
    Greet
    Send And Expect           {"t":"state","seq":2    "scan":{"hz":[0-9]+,"overruns":[0-9]+
