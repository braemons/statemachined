// SPDX-License-Identifier: GPL-3.0-or-later
// The version a device reports in `hello_ack`, stamped at build time.
//
// Stamped from the same git tag as every other statemachined artifact
// (packaging/scripts/git-version.sh): the Makefile hands it to PlatformIO as
// $STATEMACHINED_FIRMWARE_VERSION and to CMake as a cache variable, and `make
// image` writes the same string into MANIFEST.txt. That is what lets the daemon
// compare the build on a board with the build its package ships
// (GET /api/device/firmware), which a hard-coded number never could.
//
// 0.0.0 is the sentinel, as it is for the Python package: a device reporting it
// was built without stamping, and nothing can say which build it is.
#pragma once

#ifndef STATEMACHINED_FIRMWARE_VERSION
#define STATEMACHINED_FIRMWARE_VERSION ""
#endif

namespace statemachined {

inline const char* firmware_version() {
  // An unset environment variable reaches PlatformIO's build flags as "".
  const char* stamped = STATEMACHINED_FIRMWARE_VERSION;
  return stamped[0] != '\0' ? stamped : "0.0.0";
}

}  // namespace statemachined
