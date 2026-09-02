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

# Every line of the core as file:line:code, with the comments actually removed.
#
# The rules below look for words like `new` and `float`, and those words are
# perfectly legitimate in prose -- "accept the new level" must not fail the
# allocation check, while `int* p = new int;  // honest` must. So this strips
# comments and tests what is left, rather than skipping lines that look like
# comments.
#
# It tracks /* */ across lines and steps over string literals, because both are
# ways a naive stripper gets it wrong: a block comment saying `new` would be a
# false positive, and breaking at the // inside "http://..." would silently drop
# real code after it and hide a violation. String *contents* are dropped for the
# same reason comments are -- a literal is data, not an allocation. Block state
# resets per file so an unterminated comment cannot swallow the next one.
core_code() {
  grep -rn '' firmware/core --include='*.h' --include='*.cpp' |
    awk '
      {
        p = index($0, ":"); r = substr($0, p + 1)
        q = index(r, ":")
        file = substr($0, 1, p - 1)
        prefix = substr($0, 1, p + q)
        code = substr(r, q + 1)
        if (file != lastfile) { inblock = 0; lastfile = file }

        out = ""; i = 1; n = length(code); instr = 0
        while (i <= n) {
          ch = substr(code, i, 1); two = substr(code, i, 2)
          if (inblock) {
            if (two == "*/") { inblock = 0; i += 2 } else { i += 1 }
            continue
          }
          if (instr) {
            # The contents of a string literal are dropped, the quotes kept: a
            # message that happens to contain "new" is not an allocation, and a
            # path like "http://..." must not be read as starting a comment.
            if (ch == "\\") { i += 2; continue }
            if (ch == "\"") { instr = 0; out = out ch }
            i += 1
            continue
          }
          if (two == "/*") { inblock = 1; i += 2; continue }
          if (two == "//") break
          if (ch == "\"") { instr = 1; out = out ch; i += 1; continue }
          out = out ch; i += 1
        }
        if (out ~ /[^ \t]/) print prefix out
      }
    '
}

# No allocation, and no standard library beyond the fixed-width integer types.
for pattern in 'std::' '\bnew\b' '\bdelete\b' '\bmalloc\b' '\bcalloc\b' '\brealloc\b' '\bfree\b'; do
  hits=$(core_code | grep -E "$pattern" || true)
  if [ -n "$hits" ]; then
    report "firmware/core must not use $pattern:"
    printf '%s\n' "$hits" >&2
  fi
done

# Floating point reaches no value that crosses the wire. The truncated
# exponential is integer-only for exactly this reason: a float path would make
# the native simulator's numbers merely close to the firmware's.
floats=$(core_code | grep -E '\b(float|double)\b' || true)
if [ -n "$floats" ]; then
  report 'firmware/core must not use float or double:'
  printf '%s\n' "$floats" >&2
fi

if [ "$fail" -eq 0 ]; then
  echo 'core purity: ok'
fi
exit "$fail"
