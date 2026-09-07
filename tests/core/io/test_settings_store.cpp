// SPDX-License-Identifier: GPL-3.0-or-later
// What a board remembers across a power cut, saved and loaded on the host.
//
// The store is streamed rather than staged, so the writer here is a vector and
// the reader walks back over it -- which is exactly the shape data flash has,
// and means every rule below is checked without a board.
#include <vector>

#include "doctest.h"
#include "helpers.h"
#include "io/settings_store.h"

using namespace statemachined;
using namespace statemachined::test;

namespace {

/// Storage, near enough: bytes in order, with a switch for the failure that
/// actually happens -- a write that stops part way because the power went.
struct Storage : SettingsWriter, SettingsReader {
  std::vector<uint8_t> bytes;
  size_t read_at = 0;
  size_t stop_writing_after = 0;  ///< 0 means never

  bool write(const void* src, size_t n) override {
    if (stop_writing_after != 0 && bytes.size() + n > stop_writing_after) return false;
    const uint8_t* b = static_cast<const uint8_t*>(src);
    bytes.insert(bytes.end(), b, b + n);
    return true;
  }
  bool read(void* dst, size_t n) override {
    if (read_at + n > bytes.size()) return false;
    for (size_t i = 0; i < n; ++i) static_cast<uint8_t*>(dst)[i] = bytes[read_at + i];
    read_at += n;
    return true;
  }
};

/// A set worth storing: two graphs, a choice distribution with weights (the one
/// thing in a GraphSet that is a pointer), a dwell on a terminal state.
Builder built_set() {
  Builder b;
  const uint8_t wait = b.state();
  const uint8_t hit = b.terminal(TrialOutcome::Hit);
  b.timeout(wait, b.fixed(500), hit);
  b.relight(hit, b.uniform(800, 1200));
  b.on_entry(wait, OutputAction{3, OutputActionKind::Pulse, 40});
  b.on(wait, Transition{bit(1), 0, bit(2), hit, kNoRandomDistribution, false});
  b.entry(wait);

  b.graph();
  const uint8_t only = b.state();
  const uint8_t done = b.terminal(TrialOutcome::Late);
  // A Choice, so the option pool and its weights are exercised: they are the
  // only pointers in a GraphSet and the only thing an address written to flash
  // could get wrong.
  b.g.choice_options[0] = 100;
  b.g.choice_options[1] = 300;
  b.g.choice_weights[0] = 1;
  b.g.choice_weights[1] = 3;
  b.g.n_choice_options = 2;
  const uint8_t choice = b.g.n_distributions++;
  b.g.distributions[choice] =
      RandomDistribution{RandomDistributionKind::Choice, 2, 0, 0, 0, &b.g.choice_options[0],
                         &b.g.choice_weights[0]};
  b.timeout(only, choice, done);
  b.entry(only);
  return b;
}

}  // namespace

TEST_CASE("settings survive a round trip, graph set and all") {
  Builder b = built_set();
  REQUIRE(validate(b.g) == GraphError::None);
  b.g.version = 7;

  StoredSettings out;
  out.wiring.inputs.invert_mask = 0x5;
  out.wiring.inputs.enable_mask = 0xF;
  out.wiring.inputs.debounce_ms[2] = 20;
  out.wiring.output_safe_levels = 0x9;
  out.autorun.enabled = true;
  out.autorun.graph_index = 1;
  out.autorun.cap_ms = 30000;
  out.autorun.seed = 0x0123456789ABCDEFull;
  out.autorun.first_trial_id = 41;
  out.set = &b.g;
  out.has_set = true;

  Storage storage;
  REQUIRE(save_settings(out, 12, storage));

  GraphSet restored;
  StoredSettings in;
  in.set = &restored;
  REQUIRE(load_settings(in, storage) == SettingsError::None);

  CHECK(in.write_count == 12);
  CHECK(in.wiring.inputs.invert_mask == 0x5u);
  CHECK(in.wiring.inputs.enable_mask == 0xFu);
  CHECK(in.wiring.inputs.debounce_ms[2] == 20);
  CHECK(in.wiring.output_safe_levels == 0x9u);
  CHECK(in.autorun.enabled);
  CHECK(in.autorun.graph_index == 1);
  CHECK(in.autorun.cap_ms == 30000);
  CHECK(in.autorun.seed == 0x0123456789ABCDEFull);
  CHECK(in.autorun.first_trial_id == 41u);

  REQUIRE(in.has_set);
  CHECK(restored.version == 7);
  CHECK(restored.n_graphs == b.g.n_graphs);
  CHECK(restored.n_states == b.g.n_states);
  CHECK(restored.n_transitions == b.g.n_transitions);
  CHECK(restored.n_output_actions == b.g.n_output_actions);
  // And it is a graph the machine will accept, which is the only test of a
  // restored set that matters.
  CHECK(validate(restored) == GraphError::None);
}

TEST_CASE("a restored choice distribution points into its own pool") {
  // The bug this exists for: an address written to flash means nothing to the
  // run that reads it back, and a distribution left pointing at one would draw
  // its foreperiod from whatever happened to be at that address.
  Builder b = built_set();
  b.g.version = 3;
  StoredSettings out;
  out.set = &b.g;
  out.has_set = true;

  Storage storage;
  REQUIRE(save_settings(out, 1, storage));

  GraphSet restored;
  StoredSettings in;
  in.set = &restored;
  REQUIRE(load_settings(in, storage) == SettingsError::None);

  const RandomDistribution& choice = restored.distributions[restored.n_distributions - 1];
  REQUIRE(choice.kind == RandomDistributionKind::Choice);
  REQUIRE(choice.opts == &restored.choice_options[0]);
  REQUIRE(choice.weights == &restored.choice_weights[0]);
  CHECK(choice.opts[1] == 300);
  CHECK(choice.weights[1] == 3);

  // Drawing from it gives one of its own options, which is what "points at its
  // own pool" is actually for.
  Rng rng(99);
  for (int i = 0; i < 20; ++i) {
    const Milliseconds drawn = choice.draw(rng);
    CHECK((drawn == 100 || drawn == 300));
  }
}

TEST_CASE("a board that has been told its wiring but given no graph stores that") {
  StoredSettings out;
  out.wiring.output_safe_levels = 0xFF;
  out.has_set = false;

  Storage storage;
  REQUIRE(save_settings(out, 4, storage));

  GraphSet untouched;
  untouched.version = 99;
  StoredSettings in;
  in.set = &untouched;
  REQUIRE(load_settings(in, storage) == SettingsError::None);
  CHECK_FALSE(in.has_set);
  CHECK(in.wiring.output_safe_levels == 0xFFu);
  CHECK(untouched.version == 99);  // not overwritten by a record with no set in it
}

TEST_CASE("blank storage is a board with no settings, not a damaged one") {
  // Erased data flash reads as 0xFF. The difference matters: one is a board
  // nobody has saved to yet, the other is a fault worth reporting.
  Storage storage;
  storage.bytes.assign(64, 0xFF);
  GraphSet g;
  StoredSettings in;
  in.set = &g;
  CHECK(load_settings(in, storage) == SettingsError::BadMagic);
}

TEST_CASE("an interrupted save reads back as damaged rather than as settings") {
  // The power went during the write. What must not happen is a board coming up
  // with half a record read as a whole one.
  Builder b = built_set();
  StoredSettings out;
  out.set = &b.g;
  out.has_set = true;
  out.wiring.output_safe_levels = 0x3;

  Storage storage;
  storage.stop_writing_after = 40;
  CHECK_FALSE(save_settings(out, 2, storage));

  GraphSet g;
  StoredSettings in;
  in.set = &g;
  const SettingsError e = load_settings(in, storage);
  CHECK((e == SettingsError::Io || e == SettingsError::BadCrc));
  CHECK(e != SettingsError::None);
}

TEST_CASE("a record whose bytes have rotted is refused") {
  Builder b = built_set();
  StoredSettings out;
  out.set = &b.g;
  out.has_set = true;
  out.autorun.enabled = true;

  Storage storage;
  REQUIRE(save_settings(out, 5, storage));
  // One bit, in the middle of the graph set. A rig that came up running this
  // would be running a paradigm nobody wrote.
  storage.bytes[storage.bytes.size() / 2] ^= 0x01;

  GraphSet g;
  StoredSettings in;
  in.set = &g;
  CHECK(load_settings(in, storage) == SettingsError::BadCrc);
  CHECK_FALSE(in.autorun.enabled);  // nothing is adopted from a record that failed
}

TEST_CASE("a record from another format version is refused, not interpreted") {
  Builder b = built_set();
  StoredSettings out;
  out.set = &b.g;
  out.has_set = true;

  Storage storage;
  REQUIRE(save_settings(out, 1, storage));
  storage.bytes[4] = static_cast<uint8_t>(kSettingsFormat + 1);

  GraphSet g;
  StoredSettings in;
  in.set = &g;
  CHECK(load_settings(in, storage) == SettingsError::BadVersion);
}

TEST_CASE("the write counter is whatever the caller stamped, and comes back") {
  // Wear is the reason it exists: data flash is good for about 100,000 erase
  // cycles, and a board that has been saved to forty thousand times should be
  // able to say so.
  StoredSettings out;
  Storage first;
  REQUIRE(save_settings(out, 0, first));
  GraphSet g;
  StoredSettings in;
  in.set = &g;
  REQUIRE(load_settings(in, first) == SettingsError::None);
  CHECK(in.write_count == 0);

  Storage next;
  REQUIRE(save_settings(out, in.write_count + 1, next));
  StoredSettings again;
  again.set = &g;
  REQUIRE(load_settings(again, next) == SettingsError::None);
  CHECK(again.write_count == 1);
}
