use std::{
    collections::BTreeMap,
    ffi::OsString,
    fs,
    io::Read,
    path::{Path, PathBuf},
};

use serde::de::DeserializeOwned;

#[derive(Clone, Copy, PartialEq, Eq)]
pub enum Entry {
    File,
    Directory,
}

pub type Entries = BTreeMap<OsString, Entry>;

pub struct Tree<'a> {
    checkout: &'a Path,
    problems: Vec<(PathBuf, String)>,
}

impl<'a> Tree<'a> {
    pub fn new(checkout: &'a Path) -> Self {
        Self {
            checkout,
            problems: Vec::new(),
        }
    }

    pub fn problem(&mut self, path: &Path, reason: impl ToString) {
        self.problems.push((path.to_owned(), reason.to_string()));
    }

    pub fn finish(mut self) -> Vec<String> {
        self.problems.sort();
        self.problems
            .into_iter()
            .map(|(path, reason)| {
                // Paths and serde diagnostics may contain line breaks or terminal controls.
                format!(
                    "{}: {}",
                    path.to_string_lossy().escape_debug(),
                    reason
                        .split_whitespace()
                        .collect::<Vec<_>>()
                        .join(" ")
                        .escape_debug()
                )
            })
            .collect()
    }

    pub fn entry(&mut self, path: &Path) -> Option<Entry> {
        match fs::symlink_metadata(self.checkout.join(path)) {
            Ok(meta) if meta.is_dir() => Some(Entry::Directory),
            Ok(meta) if meta.is_file() => Some(Entry::File),
            Ok(_) => {
                self.problem(
                    path,
                    "expected a regular file or directory, not a symlink or special file",
                );
                None
            }
            Err(err) => {
                self.problem(path, err);
                None
            }
        }
    }

    // Call only on a directory already classified by entry() or list(). The
    // checker reads a checkout snapshot, not a concurrently edited directory.
    pub fn list(&mut self, dir: &Path) -> Entries {
        let mut files = Entries::new();
        match fs::read_dir(self.checkout.join(dir)) {
            Ok(iter) => {
                for item in iter {
                    match item {
                        Ok(item) => {
                            let name = item.file_name();
                            if let Some(entry) = self.entry(&dir.join(&name)) {
                                files.insert(name, entry);
                            }
                        }
                        Err(err) => self.problem(dir, err),
                    }
                }
            }
            Err(err) => self.problem(dir, err),
        }
        files
    }

    pub fn toml<T: DeserializeOwned>(&mut self, path: &Path, entry: Option<Entry>) -> Option<T> {
        if entry != Some(Entry::File) {
            self.problem(path, "expected a regular TOML file");
            return None;
        }
        match fs::read_to_string(self.checkout.join(path)) {
            Ok(text) => toml::from_str(&text)
                .map_err(|err| self.problem(path, err))
                .ok(),
            Err(err) => {
                self.problem(path, err);
                None
            }
        }
    }

    pub fn image(&mut self, path: &Path) {
        // Modrinth: apps/labrinth/src/routes/internal/globals.rs:57 (max_gallery_image_size).
        const MAX_IMAGE_SIZE: u64 = 5 * 1024 * 1024;
        let result = (|| {
            let file = fs::File::open(self.checkout.join(path))?;
            if file.metadata()?.len() > MAX_IMAGE_SIZE {
                self.problem(path, "image exceeds 5 MiB");
                return Ok(());
            }
            let mut header = Vec::new();
            file.take(8).read_to_end(&mut header)?;
            if !header.starts_with(b"\x89PNG\r\n\x1a\n") && !header.starts_with(b"\xff\xd8\xff") {
                self.problem(path, "expected a PNG or JPEG file signature");
            }
            Ok::<_, std::io::Error>(())
        })();
        if let Err(err) = result {
            self.problem(path, err);
        }
    }

    pub fn unknown(&mut self, dir: &Path, entries: Entries) {
        for name in entries.keys() {
            self.problem(&dir.join(name), "unexpected registry entry");
        }
    }
}
