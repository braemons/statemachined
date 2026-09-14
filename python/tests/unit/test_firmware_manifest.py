# SPDX-License-Identifier: GPL-3.0-or-later
"""Whether a board runs the firmware its package ships, and when nobody can say."""

from __future__ import annotations

from statemachined.daemon.firmware_manifest import compare_firmware, installed_firmware_version

MANIFEST = """statemachined firmware for the uno_r4_minima

version: 0.3.0~alpha1
commit: 3adea8478b3a7477980ba271e6576cd4ece492ef
built:  2026-09-08T18:12:23Z
"""


def test_the_version_is_read_from_the_manifest(tmp_path):
    manifest = tmp_path / "MANIFEST.txt"
    manifest.write_text(MANIFEST)
    assert installed_firmware_version(manifest) == "0.3.0~alpha1"


def test_no_manifest_and_an_unstamped_manifest_both_have_no_version(tmp_path):
    assert installed_firmware_version(tmp_path / "missing.txt") is None
    # The manifests released before images were stamped carry only a commit.
    old = tmp_path / "MANIFEST.txt"
    old.write_text(MANIFEST.replace("version: 0.3.0~alpha1\n", ""))
    assert installed_firmware_version(old) is None


def test_a_stamped_board_running_the_shipped_build_matches():
    answer = compare_firmware("0.3.0~alpha1", "0.3.0~alpha1")
    assert answer["comparable"] and answer["matches"]


def test_a_stamped_board_running_another_build_does_not():
    answer = compare_firmware("0.2.0~alpha1", "0.3.0~alpha1")
    assert answer["comparable"]
    assert not answer["matches"]


def test_an_unstamped_board_is_not_comparable_rather_than_a_mismatch():
    # 0.0.0 is every unstamped build at once; calling it a mismatch would be as
    # much a guess as calling it a match.
    answer = compare_firmware("0.0.0", "0.3.0~alpha1")
    assert answer["running_is_stamped"] is False
    assert not answer["comparable"]
    assert not answer["matches"]


def test_nothing_installed_is_not_comparable():
    answer = compare_firmware("0.3.0~alpha1", None)
    assert not answer["comparable"]
    assert not answer["matches"]
