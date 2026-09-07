// SPDX-License-Identifier: GPL-3.0-or-later
// What a board remembers across a power cut: its wiring, its autorun settings
// and the graph set itself.
//
// Everything else in this firmware arrives from a host and is gone at reset,
// which is the right default for a rig -- a device that runs a paradigm nobody
// uploaded is a hazard -- and it is exactly what stops a board being useful on
// its own. A box that has to be told what it is wired to before it can fail
// safe correctly is a box whose safety depends on the daemon being up, and one
// that has to be handed a graph before it can run anything cannot run anything
// unattended. Storage is what closes both.
//
// ---------------------------------------------------------------------------
// Streamed, not staged.
// ---------------------------------------------------------------------------
//
// The largest thing here is the graph set: 2856 B on the reference build,
// against 32 KB of SRAM most of which is already spoken for. Encoding it into a
// buffer to hand to a writer would briefly double the largest structure in the
// system, so this writes through a sink a few bytes at a time and reads back
// the same way. Nothing here allocates and nothing here holds a second copy.
//
// ---------------------------------------------------------------------------
// Field by field, not memcpy.
// ---------------------------------------------------------------------------
//
// A struct copied wholesale into flash is a record whose meaning is this
// build's field offsets. It reads back correctly until somebody adds a member,
// and then it reads back *plausibly*, which is worse -- a rig that comes up
// with a debounce that used to be a safe level. So every field is written by
// name and in a stated order, the format carries a version, and a record whose
// version is not this one is refused rather than interpreted.
//
// The pointers inside RandomDistribution are the clearest case: `opts` and
// `weights` point into the set's own pools, and an address from the run that
// saved is meaningless to the run that loads. They are stored as offsets into
// those pools and re-pointed on the way back in, which is the same fix-up
// GraphSet's copy constructor makes and for the same reason.
//
// ---------------------------------------------------------------------------
// The write counter.
// ---------------------------------------------------------------------------
//
// Data flash wears out -- on the RA4M1 the endurance is about 100,000 erase
// cycles per block, which is a large number until something writes settings in
// a loop. So every record carries how many times this board has been written,
// and it is reported to the host. A rig that has been saved to forty thousand
// times can say so, which is the difference between a board that fails in a
// year and a board that told somebody first.
#pragma once
#include <cstddef>
#include <cstdint>

#include "graph/graph_set.h"
#include "io/wiring.h"
#include "trial/autorun.h"

namespace statemachined {

/// Where the bytes go. A sink rather than a buffer, for the reason at the top:
/// the graph set is too big to stage a copy of.
///
/// `write` returns false on the first failure and the encoder stops there. A
/// half-written record is not a hazard, because a record is only ever accepted
/// with its CRC intact -- an interrupted save reads back as a bad record, which
/// is a board that has forgotten its settings rather than one that has invented
/// some.
class SettingsWriter {
 public:
  virtual ~SettingsWriter() = default;
  virtual bool write(const void* src, size_t n) = 0;
};

/// Where they come from, in the order they were written. `read` returns false
/// when it cannot fill the request, which decode treats as a truncated record.
class SettingsReader {
 public:
  virtual ~SettingsReader() = default;
  virtual bool read(void* dst, size_t n) = 0;
};

/// Everything a board remembers, gathered by reference so that the graph set is
/// never copied.
///
/// `set` may be null, which is what a board that has been told its wiring but
/// never given a paradigm stores. On load it must point at the GraphSet to fill
/// in, and `has_set` says whether the record had one.
struct StoredSettings {
  DeviceWiring wiring;
  AutorunConfig autorun;
  GraphSet* set = nullptr;
  bool has_set = false;

  /// How many times this board has been written, including the write being
  /// described. Set by save_settings(), read back by load_settings(), and
  /// reported to the host so that flash wear is a number somebody can see
  /// rather than a surprise.
  uint32_t write_count = 0;
};

enum class SettingsError : uint8_t {
  None = 0,
  Io,          ///< the reader or writer gave up; nothing else is known
  BadMagic,    ///< not a settings record. Blank flash reads as this
  BadVersion,  ///< a record this firmware refuses to interpret
  BadCrc,      ///< a record that was interrupted, or storage that has failed
  TooBig,      ///< counts in the record exceed this build's capacities
};

const char* settings_error_str(SettingsError e);

/// The format version written into every record. Bumped whenever the field
/// order below changes, which is what turns "reads back plausibly" into "is
/// refused".
constexpr uint16_t kSettingsFormat = 1;

/// Write `s` through `out`, stamping it with `write_count`. The caller supplies
/// the count, having read the previous one back at boot: the store does not
/// keep state between calls, and a counter maintained in two places is a
/// counter that disagrees with itself.
bool save_settings(const StoredSettings& s, uint32_t write_count, SettingsWriter& out);

/// Read a record through `in`. `s.set` must point at somewhere to put a graph
/// set; it is left untouched if the record has none.
///
/// On anything but None the settings are not to be trusted and the caller keeps
/// its compiled-in defaults -- which is why those defaults have to stay correct
/// on their own. See STATEMACHINED_SAFE_LEVELS.
SettingsError load_settings(StoredSettings& s, SettingsReader& in);

/// Does the store already hold exactly this record?
///
/// An erase cycle spent to change nothing is an erase cycle spent, and there is
/// a button in the web UI that invites being pressed twice. So a save compares
/// first: the record is encoded again and matched against the stored bytes as
/// it goes, a few bytes at a time -- no second copy, and it stops at the first
/// difference.
///
/// `write_count` must be the count the store is believed to hold, not the one a
/// write would stamp: the counter is inside the record, so comparing against a
/// bumped one would differ on every call and answer nothing.
bool settings_already_stored(const StoredSettings& s, uint32_t write_count, SettingsReader& in);

/// The store as the session sees it: something that can be saved to and loaded
/// from, with the counter kept for it.
///
/// An interface rather than a call into the HAL, for the same reason ReplySink
/// is one: the session touches no port and no pin, and a board's data flash is
/// as much a piece of hardware as its serial link. main.cpp implements this
/// over hal::storage_*, the native device over a file, and a test over a
/// vector -- and the rule about who saves what is then testable on the host
/// like everything else here.
class SettingsPort {
 public:
  virtual ~SettingsPort() = default;

  /// Whether this board has anywhere to put settings at all. A board with none
  /// is not broken; it is one whose settings do not survive a power cut, and
  /// `save` is refused on it rather than silently doing nothing.
  virtual bool has_storage() const = 0;

  /// Write these settings, stamped with `write_count`. False if any part of it
  /// failed, in which case the store holds no valid record -- see
  /// save_settings().
  virtual bool save(const StoredSettings& s, uint32_t write_count) = 0;

  /// Whether the store already holds exactly this record, so that a save would
  /// spend an erase cycle to change nothing. See settings_already_stored().
  virtual bool holds(const StoredSettings& s, uint32_t write_count) = 0;

  /// Read what is stored into `s`, whose `set` must point somewhere.
  virtual SettingsError load(StoredSettings& s) = 0;
};

}  // namespace statemachined
