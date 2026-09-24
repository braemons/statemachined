// SPDX-License-Identifier: AGPL-3.0-or-later
//! Documents on disk: one JSON file per document, addressed by name.
//!
//! One file per document rather than one database, and it is not laziness. The
//! files are greppable, diffable and copyable; **a rig at two in the morning
//! with no network is fixed with an editor.** A database would buy indexed
//! queries over a directory that will hold tens of entries.
//!
//! **Nothing invalid is ever written.** A document is validated on the way in,
//! so the store cannot hold a paradigm that could not be run — which means the
//! failure surfaces when somebody saves, with the field named, rather than when
//! a session starts.
//!
//! Ported from `daemon/graph_store.py` and `state_machine_config_store.py`,
//! which are the same store twice. Here it is once, over a document type: the
//! rules about names, atomic writes and the "stored under a name that is not
//! the one inside it" case are identical, and having written them twice in
//! Python is a reason to write them once here rather than a reason not to.

use std::path::{Path, PathBuf};

use crate::model::graph_definition::{GraphDefinition, Refused};
use crate::model::state_machine_config::StateMachineConfig;

/// What went wrong, in the shape the rpcs answer with.
///
/// Separate from `Refused` because the store's failures are not the document's:
/// "no graph called that is stored" is `not_found`, and "this file does not
/// parse" is `invalid_argument`. The Python daemon draws the same line with
/// `GraphNotInStore` against a `ValidationError`.
#[derive(Debug)]
pub enum StoreProblem {
    /// No document of that name. The message lists what is stored.
    NotStored(String),
    /// A name that is not usable as a file name, or one that disagrees with the
    /// document it holds.
    BadName(String),
    /// The document does not parse, or breaks its own rules.
    Unreadable(Refused),
    /// The disk said no.
    Io(String),
}

impl std::fmt::Display for StoreProblem {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::NotStored(sentence) | Self::BadName(sentence) | Self::Io(sentence) => {
                f.write_str(sentence)
            }
            Self::Unreadable(refused) => write!(f, "{refused}"),
        }
    }
}

type Stored<T> = Result<T, StoreProblem>;

/// A document a store can hold.
///
/// The two implementations differ in three things — how to parse one, what it
/// is called, and how to write it back — so that is what the trait asks for.
pub trait Document: Sized {
    const WHAT: &'static str;
    /// What the file is called after the name.
    ///
    /// **Not always `.json`, and `Path::stem` is the trap.** A config is
    /// `go-nogo.config.json` and is the config `go-nogo`; `stem` answers
    /// `go-nogo.config`, so the suffix is taken off by length instead. The
    /// Python store documents the same trap in a comment, which is how this
    /// port found it -- by falling into it and having the difference shown by a
    /// differential test.
    const SUFFIX: &'static str;
    fn parse(text: &str) -> Result<Self, Refused>;
    fn name(&self) -> &str;
    fn to_json(&self) -> String;
}

impl Document for GraphDefinition {
    const WHAT: &'static str = "graph";
    const SUFFIX: &'static str = ".json";

    fn parse(text: &str) -> Result<Self, Refused> {
        Self::from_json(text)
    }

    fn name(&self) -> &str {
        &self.name
    }

    fn to_json(&self) -> String {
        serde_json::to_string_pretty(self).expect("a checked document serialises")
    }
}

impl Document for StateMachineConfig {
    const WHAT: &'static str = "state-machine config";
    /// `.config.json`, which is what vstimd's scene-configs use.
    const SUFFIX: &'static str = ".config.json";

    fn parse(text: &str) -> Result<Self, Refused> {
        Self::from_json(text)
    }

    fn name(&self) -> &str {
        &self.name
    }

    fn to_json(&self) -> String {
        serde_json::to_string_pretty(self).expect("a checked document serialises")
    }
}

/// The documents this rig knows, by name.
pub struct Store<D> {
    pub directory: PathBuf,
    document: std::marker::PhantomData<D>,
}

impl<D: Document> Store<D> {
    pub fn new(directory: impl Into<PathBuf>) -> Self {
        Self {
            directory: directory.into(),
            document: std::marker::PhantomData,
        }
    }

    /// The file a name addresses.
    ///
    /// **A name is a file name, so it may not climb out of the directory.**
    /// This is reachable from a request parameter, which is exactly the place
    /// not to trust one.
    fn path_for(&self, name: &str) -> Stored<PathBuf> {
        if name.is_empty()
            || name.contains('/')
            || name.contains('\\')
            || name.starts_with('.')
            || name.contains("..")
        {
            return Err(StoreProblem::BadName(format!(
                "'{name}' is not a usable {} name",
                D::WHAT
            )));
        }
        Ok(self.directory.join(format!("{name}{}", D::SUFFIX)))
    }

    pub fn stored_names(&self) -> Vec<String> {
        let Ok(entries) = std::fs::read_dir(&self.directory) else {
            return Vec::new();
        };
        // By length off the whole file name, never `file_stem` -- see SUFFIX.
        let mut names: Vec<String> = entries
            .filter_map(Result::ok)
            .filter_map(|entry| entry.file_name().into_string().ok())
            .filter_map(|file_name| {
                file_name
                    .strip_suffix(D::SUFFIX)
                    .filter(|name| !name.is_empty())
                    .map(str::to_owned)
            })
            .collect();
        names.sort();
        names
    }

    fn not_stored(&self, name: &str) -> StoreProblem {
        let stored = self.stored_names();
        StoreProblem::NotStored(format!(
            "no {} called '{name}' is stored. Stored: {}",
            D::WHAT,
            if stored.is_empty() {
                "(none)".to_string()
            } else {
                stored.join(", ")
            }
        ))
    }

    /// The file's text, unparsed — what an editor asks for.
    pub fn read_text(&self, name: &str) -> Stored<String> {
        let path = self.path_for(name)?;
        std::fs::read_to_string(&path).map_err(|_| self.not_stored(name))
    }

    pub fn load(&self, name: &str) -> Stored<D> {
        let text = self.read_text(name)?;
        let document = D::parse(&text).map_err(StoreProblem::Unreadable)?;
        if document.name() != name {
            return Err(StoreProblem::BadName(format!(
                "the file holds a {} called '{}', not '{name}'",
                D::WHAT,
                document.name()
            )));
        }
        Ok(document)
    }

    /// Write one, under its own name.
    ///
    /// Written to a temporary file and renamed, because **a half-written
    /// document on a rig's disk is a session that fails at startup with a JSON
    /// error instead of a paradigm.**
    pub fn save(&self, document: &D) -> Stored<PathBuf> {
        std::fs::create_dir_all(&self.directory)
            .map_err(|problem| StoreProblem::Io(problem.to_string()))?;
        let path = self.path_for(document.name())?;
        let partial = path.with_file_name(format!("{}.partial", document.name()));
        std::fs::write(&partial, document.to_json() + "\n")
            .map_err(|problem| StoreProblem::Io(problem.to_string()))?;
        std::fs::rename(&partial, &path)
            .map_err(|problem| StoreProblem::Io(problem.to_string()))?;
        Ok(path)
    }

    /// Check the text, then store it under the name inside it.
    ///
    /// The path `WriteGraphFile` takes: what crosses from an editor is the
    /// file's text, and it goes through this daemon's own parser — never a
    /// second, looser description of a document living in a browser.
    /// Check the text, then store it under the name inside it.
    ///
    /// `under` is the name it was sent as, when there was one. A mismatch is
    /// refused rather than silently resolved either way: storing it under the
    /// file's name would move somebody's graph, and under the request's name
    /// would leave a file whose contents disagree with where it lives.
    pub fn write_text(&self, text: &str, under: &str) -> Stored<D> {
        let document = D::parse(text).map_err(StoreProblem::Unreadable)?;
        if !under.is_empty() && document.name() != under {
            return Err(StoreProblem::BadName(format!(
                "the file calls this {} '{}' and it was stored as '{under}'",
                D::WHAT,
                document.name()
            )));
        }
        self.save(&document)?;
        Ok(document)
    }

    pub fn delete(&self, name: &str) -> Stored<()> {
        let path = self.path_for(name)?;
        if !Path::new(&path).exists() {
            return Err(self.not_stored(name));
        }
        std::fs::remove_file(&path).map_err(|problem| StoreProblem::Io(problem.to_string()))
    }
}
