#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the Python line-map resolution says, case by case.

**The silent wrong-valve bug lives here.** A line that says `pin_label = "A0"`
and `line_index = 4` when the board's A0 is line 5 looks right in every listing,
pushes cleanly, uploads cleanly, and drives the wrong pin. Nothing downstream
notices. So the resolution is the one place in the device layer where a
disagreement between the two daemons would be invisible until an animal is in
the booth, and it gets the same treatment the documents got.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE / "daemon" / "src"))

from statemachined.device.device_pin_map import DevicePinMap  # noqa: E402
from statemachined.model.line_map import LineMap  # noqa: E402

#: A board that answered `pins`, and one that could not.
FROM_THE_DEVICE = {
    "input_pin_labels": ["D2", "D3", "A0", "A1"],
    "output_pin_labels": ["D8", "D9", "D10"],
    "source": "device",
}
ASSUMED = {**FROM_THE_DEVICE, "source": "assumed"}
NOTHING_KNOWN = {"input_pin_labels": [], "output_pin_labels": [], "source": "unknown"}


def cases() -> list[dict]:
    def case(why, pin_map, inputs=(), outputs=()):
        return {
            "why": why,
            "pin_map": pin_map,
            "line_map": {"input_lines": list(inputs), "output_lines": list(outputs)},
        }

    return [
        case("a pin alone is resolved from the board", FROM_THE_DEVICE,
             inputs=[{"name": "lever", "pin_label": "A0"}]),
        case("a pin alone, lower case: the same hole", FROM_THE_DEVICE,
             inputs=[{"name": "lever", "pin_label": "a0"}]),
        case("an index alone is left as it is", FROM_THE_DEVICE,
             inputs=[{"name": "lever", "line_index": 2}]),
        case("both, agreeing", FROM_THE_DEVICE,
             inputs=[{"name": "lever", "line_index": 2, "pin_label": "A0"}]),
        case("**both, disagreeing: the silent wrong-valve bug**", FROM_THE_DEVICE,
             inputs=[{"name": "lever", "line_index": 3, "pin_label": "A0"}]),
        case("a pin this board has not got", FROM_THE_DEVICE,
             inputs=[{"name": "lever", "pin_label": "A7"}]),
        case("an index past this board's lines", FROM_THE_DEVICE,
             inputs=[{"name": "lever", "line_index": 9}]),
        case("an output pin resolved over the output numbering", FROM_THE_DEVICE,
             outputs=[{"name": "valve", "pin_label": "D10"}]),
        case("an input pin name used as an output", FROM_THE_DEVICE,
             outputs=[{"name": "valve", "pin_label": "A0"}]),
        case("the two numberings do not collide", FROM_THE_DEVICE,
             inputs=[{"name": "lever", "pin_label": "D2"}],
             outputs=[{"name": "valve", "pin_label": "D8"}]),
        # An assumed map fills a gap and never overrules or refuses.
        case("assumed: a pin alone is filled in", ASSUMED,
             inputs=[{"name": "lever", "pin_label": "A0"}]),
        case("assumed: an index wins over the table", ASSUMED,
             inputs=[{"name": "lever", "line_index": 3, "pin_label": "A0"}]),
        case("assumed: an unknown pin with an index is fine", ASSUMED,
             inputs=[{"name": "lever", "line_index": 1, "pin_label": "A7"}]),
        case("assumed: a pin it does not know, and no index", ASSUMED,
             inputs=[{"name": "lever", "pin_label": "A7"}]),
        case("no pin map at all: an index still works", NOTHING_KNOWN,
             inputs=[{"name": "lever", "line_index": 1}]),
        case("no pin map at all: a pin alone cannot be resolved", NOTHING_KNOWN,
             inputs=[{"name": "lever", "pin_label": "A0"}]),
        case("several lines, one of them wrong", FROM_THE_DEVICE,
             inputs=[{"name": "ok", "pin_label": "D2"}, {"name": "bad", "pin_label": "A7"}]),
        case("an empty map resolves to an empty map", FROM_THE_DEVICE),
    ]


def verdict(case: dict) -> dict:
    pin_map = DevicePinMap(**case["pin_map"])
    line_map = LineMap.model_validate(case["line_map"])
    try:
        resolved = line_map.resolved_against(pin_map)
    except ValueError as problem:
        return {"resolved": None, "refused": str(problem)}
    return {
        "resolved": {
            "input_lines": [
                {"name": line.name, "line_index": line.line_index}
                for line in resolved.input_lines
            ],
            "output_lines": [
                {"name": line.name, "line_index": line.line_index}
                for line in resolved.output_lines
            ],
        },
        "refused": None,
    }


def main() -> int:
    out = HERE / "daemon-rs" / "tests" / "line_map_cases.json"
    corpus = [{**case, "answer": verdict(case)} for case in cases()]
    out.write_text(json.dumps(corpus, indent=1) + "\n")
    refused = sum(1 for case in corpus if case["answer"]["refused"])
    print(f"{len(corpus)} cases -> {out.relative_to(HERE)}")
    print(f"  {len(corpus) - refused} resolved, {refused} refused")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
