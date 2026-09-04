# SPDX-License-Identifier: LGPL-3.0-or-later
"""The HTTP surface, as dev/API.md specifies it.

One router per subject, and a `RigService` underneath them that owns the device.
Routers hold no state: everything that outlives a request lives in the service,
because two requests arriving at once must not each think they own the link.
"""
