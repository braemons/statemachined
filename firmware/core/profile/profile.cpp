// SPDX-License-Identifier: GPL-3.0-or-later
#include "profile/profile.h"

#if defined(STATEMACHINED_PROFILE)

namespace statemachined {
namespace profile {
namespace {
Profile g_profile;

uint32_t g_reads = 0;
uint32_t count_reads() { return ++g_reads; }

Clock g_clock = count_reads;
uint32_t g_per_second = 1;
}  // namespace

Profile& get() { return g_profile; }

void set_clock(Clock clock, uint32_t per_second) {
  g_clock = clock;
  g_per_second = per_second;
}
uint32_t cycles() { return g_clock(); }
uint32_t cycles_per_second() { return g_per_second; }

void reset() { g_profile = Profile{}; }

}  // namespace profile
}  // namespace statemachined

#endif
