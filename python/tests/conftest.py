# SPDX-License-Identifier: GPL-3.0-or-later
"""The root of the test tree, and the one thing it is here to do.

pytest puts the directory holding a `conftest.py` on `sys.path`, so this file
existing is what lets `unit/`, `integration/` and `e2e/` all say `import
bench_rig` and mean the same module. Without it each tier would carry its own
copy of the bench rig, which is three rigs wearing one name.

It holds no fixtures on purpose. A fixture here would apply to every suite
including `hardware/`, which needs a board, and the tiers differ in exactly what
they are willing to stand up.

It does hold one *option*, which is the opposite case. `--target` names the far
end, and two suites now take it -- `hardware/` and `runs/`. pytest registers
options once per run across every conftest it loads, so defining it in both is
not a duplicate default but a hard collision: `pytest python/tests/hardware
python/tests/runs` dies during collection with "option names {'--target'}
already added", and so does anything that collects the whole tree. Defining it
here is the only place it can be defined once.

The default is deliberately None rather than either suite's answer, because the
two want different ones: a bare `--target` means "a board at the usual path" to
`hardware/` and "no board, use the host build" to `runs/`. Each reads None its
own way; neither has to know the other exists.
"""

#: What "no --target was given" looks like to whichever suite is asking.
NO_TARGET_GIVEN = None


def pytest_addoption(parser):
    parser.addoption(
        "--target",
        default=NO_TARGET_GIVEN,
        help="the far end: a device path, host:port, or any pyserial URL. "
        "Left out, hardware/ uses the reference board's usual path and runs/ "
        "uses the firmware built for this machine.",
    )
