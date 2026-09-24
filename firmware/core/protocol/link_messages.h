// SPDX-License-Identifier: GPL-3.0-or-later
// The link's messages, under names that read as this codebase's rather than as
// nanopb's. The structs are generated from proto/statemachined/link/v1/link.proto
// by `make firmware-proto` and live in core/proto/; this only renames them.
#pragma once
#include <cstddef>
#include <cstdint>

#include "statemachined/link/v1/link.pb.h"

namespace statemachined::link {

using HostMessage = statemachined_link_v1_HostMessage;
using DeviceMessage = statemachined_link_v1_DeviceMessage;

using SetBegin = statemachined_link_v1_SetBegin;
using SetEnd = statemachined_link_v1_SetEnd;
using GraphBegin = statemachined_link_v1_GraphBegin;
using GraphDist = statemachined_link_v1_GraphDist;
using GraphState = statemachined_link_v1_GraphState;
using GraphTransition = statemachined_link_v1_GraphTransition;
using GraphAction = statemachined_link_v1_GraphAction;
using GraphTimer = statemachined_link_v1_GraphTimer;
using GraphEnd = statemachined_link_v1_GraphEnd;

using Hello = statemachined_link_v1_Hello;
using Configure = statemachined_link_v1_Configure;
using Start = statemachined_link_v1_Start;
using Cancel = statemachined_link_v1_Cancel;
using Wiring = statemachined_link_v1_Wiring;
using Timers = statemachined_link_v1_Timers;
using Pins = statemachined_link_v1_Pins;
using Autorun = statemachined_link_v1_Autorun;

using StateVisit = statemachined_link_v1_StateVisit;

/// The bytes a message arrived as, which is what the upload's and the
/// result's rolling checksums fold -- not the struct, which has no single
/// encoding, but the protobuf the sender actually put on the wire.
struct PayloadSpan {
  const uint8_t* bytes = nullptr;
  size_t len = 0;
};

}  // namespace statemachined::link
