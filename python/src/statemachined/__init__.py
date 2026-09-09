# SPDX-License-Identifier: LGPL-3.0-or-later
"""statemachined: the host side of one statemachined device.

`device/` is the wire -- framing, the session, the graph upload, the result --
and is what dev/DAEMON.md builds the daemon out of. `command_line_interface.py` is the bench
instrument dev/BRINGUP.md §4 and §5 ask for: it sends one command at a time,
prints what came back, and knows nothing about paradigms, trials or graphs.
Anything that starts to look like it is running an experiment belongs below
`device/`, not in the CLI.

The version is the packaging sentinel, not a number anybody maintains by hand:
packaging/scripts/git-version.sh stamps the real one from the tag at build
time, so a `0.0.0` in the wild means the stamping was bypassed.
"""

from importlib.metadata import PackageNotFoundError, version as _version

__all__ = ["__version__"]

try:
    __version__ = _version("statemachined")
except PackageNotFoundError:  # not installed: running from a source tree
    __version__ = "0.0.0"
