use std::path::{Path, PathBuf};

use serde::Deserialize;

use crate::{
    builds::{self, Build},
    index_signature,
    schema::{BuildName, Sha256},
};

pub const KEY: &str = "registry/index-key.pub";
// Existing Sunrise mod tool's sdk_fetch::SMALL.
pub const SMALL: u64 = 16 << 20;

#[derive(Deserialize)]
pub struct Catalog {
    schema: u8,
    pub serial: u64,
    #[serde(default)]
    pub builds: Vec<Build>,
    pub projects: Vec<Project>,
}

#[derive(Deserialize)]
pub struct Project {
    pub releases: Vec<Release>,
}

#[derive(Deserialize)]
pub struct Release {
    pub file: FileDigest,
    pub fits: Vec<BuildName>,
    #[serde(default)]
    pub does_not_fit: Vec<BuildName>,
}

#[derive(Deserialize)]
pub struct FileDigest {
    pub sha256: Sha256,
}

pub fn verify(path: &Path, sig: &Path, key: &Path) -> Result<Catalog, String> {
    let bytes = index_signature::verified_bytes(path, sig, key)?;
    let index: Catalog =
        serde_json::from_slice(&bytes).map_err(|e| format!("verified index: {e}"))?;
    if index.schema != 2 {
        return Err("verified index is not schema 2".into());
    }
    builds::validate(&index.builds)?;
    Ok(index)
}

pub fn sibling_signature(path: &Path) -> PathBuf {
    let mut name = path.as_os_str().to_owned();
    name.push(".minisig");
    PathBuf::from(name)
}

pub fn require_new_serial(serial: u64, previous: u64) -> Result<(), String> {
    if serial <= previous {
        return Err(format!(
            "publish serial {serial} must be greater than live serial {previous}"
        ));
    }
    Ok(())
}

#[cfg(test)]
mod tests;
