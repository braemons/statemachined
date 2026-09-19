"""Which firmware the package ships, and whether a board is running it.

`make image` writes MANIFEST.txt beside the flashable image, and the package
installs both. Its `version:` line and the `fw` a board reports in `hello_ack`
are stamped from the same git tag (packaging/scripts/git-version.sh and
firmware/core/protocol/firmware_version.h), so equal strings mean the same
release.

`0.0.0` is the sentinel on both sides: a build that was not stamped. A board
reporting it cannot be told apart from any other unstamped build, so it is
reported as not comparable rather than as a match or a mismatch.
"""

from __future__ import annotations

from pathlib import Path

#: Where the package puts the image and its manifest.
INSTALLED_MANIFEST = Path("/usr/share/braemons/statemachined/firmware/MANIFEST.txt")

#: What an unstamped build reports.
UNSTAMPED_VERSION = "0.0.0"


def installed_firmware_version(manifest: Path = INSTALLED_MANIFEST) -> str | None:
    """The `version:` line of the package's MANIFEST.txt, when there is one.

    None on a checkout, which is not an error: its absence means "nobody
    installed a firmware image here", which is the truth on a developer's
    machine. None too for a manifest from before images were stamped.
    """
    if not manifest.exists():
        return None
    for line in manifest.read_text().splitlines():
        if line.startswith("version:"):
            version = line.split(":", 1)[1].strip()
            return version or None
    return None


def compare_firmware(running: str | None, installed: str | None) -> dict:
    """What `GET /api/device/firmware` answers, and the link trace records.

    `comparable` is whether both sides name a stamped build; `matches` is only
    ever true when they do and agree.
    """
    running_is_stamped = bool(running) and running != UNSTAMPED_VERSION
    comparable = running_is_stamped and installed is not None
    return {
        "running": running,
        "installed": installed,
        "running_is_stamped": running_is_stamped,
        "comparable": comparable,
        "matches": comparable and running == installed,
    }
