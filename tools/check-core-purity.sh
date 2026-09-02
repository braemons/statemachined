#!/bin/sh
# The portable core's constraints, enforced rather than remembered.
#
# firmware/core/ is plain C++17 with no Arduino.h, no dynamic allocation and no
# standard-library containers. It has to be, for three separate reasons:
#
#   * 32 KB of SRAM, most of it already spoken for. A container that allocates
#     fails at trial 300 rather than at compile time.
#   * The scan runs from a 10 kHz timer ISR. Allocation inside one is a
#     latency spike at best and a heap corruption at worst.
#   * The native build is meant to be a real test of the firmware rather than a
#     parallel implementation of it, which is only true while the two compile
#     the same code.
#
# The tests are exempt and deliberately so: they run on the host, they never
# reach a board, and std::string is the right tool for building a protocol line
# to feed in. `pio run` compiles +<src/> +<core/> and never tests/.
set -eu

root=$(dirname "$0")/..
cd "$root"

fail=0
report() {
  fail=1
  printf 'core purity: %s\n' "$1" >&2
}

# Only these two headers. Anything else in the core is either a container, an
# allocator, or something that drags one in.
bad_includes=$(grep -rn '#include <' firmware/core || true)
echo "$bad_includes" | while IFS= read -r line; do
  [ -n "$line" ] || continue
  case "$line" in
    *'<cstdint>'* | *'<cstddef>'*) ;;
    *) printf '%s\n' "$line" ;;
  esac
done > /tmp/fsmd-bad-includes.$$ || true

if [ -s /tmp/fsmd-bad-includes.$$ ]; then
  report 'firmware/core may include only <cstdint> and <cstddef>:'
  cat /tmp/fsmd-bad-includes.$$ >&2
fi
rm -f /tmp/fsmd-bad-includes.$$

# Arduino.h belongs in firmware/src and firmware/hal, never in the core. This is
# what lets the core be compiled and tested on the host at all.
if grep -rn 'Arduino\.h' firmware/core >/dev/null 2>&1; then
  report 'firmware/core must not include Arduino.h:'
  grep -rn 'Arduino\.h' firmware/core >&2
fi

# No allocation, and no standard library beyond the fixed-width integer types.
for pattern in 'std::' '\bnew\b' '\bdelete\b' '\bmalloc\b' '\bcalloc\b' '\brealloc\b' '\bfree\b'; do
  hits=$(grep -rnE "$pattern" firmware/core --include='*.h' --include='*.cpp' || true)
  # Comments and doc text may say the words; code may not.
  hits=$(printf '%s\n' "$hits" | grep -vE ':[[:space:]]*(//|\*|///)' || true)
  if [ -n "$hits" ]; then
    report "firmware/core must not use $pattern:"
    printf '%s\n' "$hits" >&2
  fi
done

# Floating point reaches no value that crosses the wire. The truncated
# exponential is integer-only for exactly this reason: a float path would make
# the native simulator's numbers merely close to the firmware's.
floats=$(grep -rnE '\b(float|double)\b' firmware/core --include='*.h' --include='*.cpp' || true)
floats=$(printf '%s\n' "$floats" | grep -vE ':[[:space:]]*(//|\*|///)' || true)
if [ -n "$floats" ]; then
  report 'firmware/core must not use float or double:'
  printf '%s\n' "$floats" >&2
fi

if [ "$fail" -eq 0 ]; then
  echo 'core purity: ok'
fi
exit "$fail"
