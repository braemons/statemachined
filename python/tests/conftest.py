# SPDX-License-Identifier: GPL-3.0-or-later
"""The root of the test tree, and the one thing it is here to do.

pytest puts the directory holding a `conftest.py` on `sys.path`, so this file
existing is what lets `unit/`, `integration/` and `e2e/` all say `import
bench_rig` and mean the same module. Without it each tier would carry its own
copy of the bench rig, which is three rigs wearing one name.

It holds no fixtures on purpose. A fixture here would apply to every suite
including `hardware/`, which needs a board, and the tiers differ in exactly what
they are willing to stand up.
"""
