// SPDX-License-Identifier: AGPL-3.0-or-later
//! Which firmware this package ships, against what the board runs.
//!
//! A board in a rack cannot be asked which commit it is running, so the package
//! carries a manifest and the daemon says whether the two agree.

use std::path::Path;

pub const INSTALLED_MANIFEST: &str = "/usr/share/braemons/statemachined/firmware/MANIFEST.txt";

/// What a local build that nobody stamped reports as its version.
pub const UNSTAMPED_VERSION: &str = "0.0.0";

/// The `version:` line of the package's MANIFEST.txt, when there is one.
///
/// `None` on a checkout, which is not an error: it means "nobody installed a
/// firmware image here", which is the truth on a developer's machine. `None`
/// too for a manifest from before images were stamped.
pub fn installed_firmware_version(manifest: &Path) -> Option<String> {
    let text = std::fs::read_to_string(manifest).ok()?;
    text.lines()
        .find_map(|line| line.strip_prefix("version:"))
        .map(|version| version.trim().to_string())
        .filter(|version| !version.is_empty())
}

/// What the board runs against what this package ships.
///
/// `comparable` is whether both sides name a stamped build; `matches` is only
/// ever true when they do and agree. An unstamped local build makes the
/// comparison *meaningless* rather than false.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FirmwareComparison {
    pub running: Option<String>,
    pub installed: Option<String>,
    pub running_is_stamped: bool,
    pub comparable: bool,
    pub matches: bool,
}

pub fn compare_firmware(running: Option<&str>, installed: Option<&str>) -> FirmwareComparison {
    let running_is_stamped =
        running.is_some_and(|running| !running.is_empty() && running != UNSTAMPED_VERSION);
    let comparable = running_is_stamped && installed.is_some();
    FirmwareComparison {
        running: running.map(str::to_string),
        installed: installed.map(str::to_string),
        running_is_stamped,
        comparable,
        matches: comparable && running == installed,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn an_unstamped_build_is_not_comparable_rather_than_different() {
        let comparison = compare_firmware(Some("0.0.0"), Some("0.2.0"));
        assert!(!comparison.running_is_stamped);
        assert!(!comparison.comparable);
        assert!(!comparison.matches);
    }

    #[test]
    fn two_stamped_builds_that_agree_match() {
        assert!(compare_firmware(Some("0.2.0"), Some("0.2.0")).matches);
        assert!(!compare_firmware(Some("0.2.0"), Some("0.2.1")).matches);
    }

    #[test]
    fn nothing_installed_is_nothing_to_compare() {
        let comparison = compare_firmware(Some("0.2.0"), None);
        assert!(comparison.running_is_stamped);
        assert!(!comparison.comparable);
    }

    #[test]
    fn the_version_is_read_from_its_own_line() {
        let directory = tempfile::tempdir().unwrap();
        let manifest = directory.path().join("MANIFEST.txt");
        std::fs::write(&manifest, "board: uno\nversion: 0.2.0\n").unwrap();
        assert_eq!(
            installed_firmware_version(&manifest).as_deref(),
            Some("0.2.0")
        );
        std::fs::write(&manifest, "version:\n").unwrap();
        assert_eq!(installed_firmware_version(&manifest), None);
        assert_eq!(
            installed_firmware_version(&directory.path().join("absent")),
            None
        );
    }
}
