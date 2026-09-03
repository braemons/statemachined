# SPDX-License-Identifier: GPL-3.0-or-later
"""A small host-side CLI for talking to a statemachined device.

This is the bench instrument dev/BRINGUP.md §4 and §5 ask for, not the bridge.
It sends one command at a time, prints what came back, and knows nothing about
paradigms, trials or graphs. The bridge is a separate program with a separate
job; anything here that starts to look like it is running an experiment belongs
there instead.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
