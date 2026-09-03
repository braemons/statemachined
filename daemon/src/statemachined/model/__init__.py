# SPDX-License-Identifier: LGPL-3.0-or-later
"""The graph as a person authors it, before any of it becomes an index.

The wire speaks indices because the device has 32 KB (dev/PROTOCOL.md); a person
speaks names. Everything in this package is the name-shaped side of that, and
`statemachined.compile` is the translation. Nothing here knows what a message
looks like, and nothing in `statemachined.device` knows what a name is.

That split is the daemon's main reason to exist, and it is also why these models
are pydantic rather than dataclasses: what arrives here comes from a file
somebody edited or from an HTTP request, and "refuse it, naming the field" is
the whole job at that boundary.
"""
