// Board entry point. Deliberately thin: everything of substance is in core/,
// which knows nothing about Arduino and is tested on the host.
//
// NOT YET IMPLEMENTED -- see dev/PLAN.md, milestone M3. What goes here:
//   - a HAL for the board (direct port-register reads; the Renesas core's
//     digitalRead() costs 1-2 us per call and would eat the scan budget)
//   - a hardware-timer ISR at 10 kHz driving TrialStateMachine::scan()
//   - the NDJSON protocol codec on USB CDC
//   - fail-safe: outputs to their configured safe levels on watchdog timeout,
//     reset, link loss or a refused graph
#include <Arduino.h>

void setup() {}
void loop() {}
