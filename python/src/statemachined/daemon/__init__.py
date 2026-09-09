# SPDX-License-Identifier: LGPL-3.0-or-later
"""Being the daemon: the HTTP API, the web UI, the stores and the trace.

The `[serve]` tier, and the only one with a web framework in it. What is here
is everything that exists because a daemon **outlives the script that spoke to
it**: a graph store on disk, a bounded trace ring, recordings taken off it,
the list of who is watching, and a config file describing the box.

That is also exactly the list of things `statemachined.device` does not have,
and the reason the two are different classes rather than two backends behind
one interface. A direct connection has no ring to record from -- the caller was
the only listener, and what it did not keep is gone. Offering a `recordings`
that meant something else there would be the dishonest kind of symmetry.
"""
