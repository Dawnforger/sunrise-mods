use std::{
    collections::{BTreeMap, btree_map::Entry},
    fs,
    path::{Path, PathBuf},
    process::Command,
    rc::Rc,
};

use mod_surface::{
    Surface,
    uses::{Fit as SurfaceFit, PLAIN_SCHEMA, USES_FILE, UsesTable},
};
use sunrise_mod_package::{
    Manifest, Package, index::MAX_FILE, manifest::MAX_USES, native_route, sdk,
};
use sunrise_release_sign::{SIGNATURE, SUMS, check, sdk_digest, sha256_hex, trusted_keys_for};

use crate::{
    builds::{self, Build, SUNRISE},
    escape,
    registry::{self, Registry},
    schema::{Asset, BuildName, Commit, Fit, FitRelease, Sha256},
    signed::{self, Catalog, SMALL},
};

const SURFACE: &str = "surface.json";
type Cells = BTreeMap<(Sha256, BuildName, Commit), bool>;

trait Judge {
    fn judge(&mut self, asset: &Asset, build: &Build) -> Result<bool, String>;
}

fn cache(index: Catalog) -> Result<Cells, String> {
    let builds: BTreeMap<_, _> = index
        .builds
        .into_iter()
        .map(|row| (row.id, row.commit))
        .collect();
    let mut cells = Cells::new();
    for release in index.projects.into_iter().flat_map(|p| p.releases) {
        for (verdict, ids) in [(true, release.fits), (false, release.does_not_fit)] {
            for id in ids {
                // Older catalogues need not have a build table. They carry no reusable cells.
                let Some(commit) = builds.get(&id) else {
                    continue;
                };
                let key = (release.file.sha256.clone(), id, commit.clone());
                if cells.insert(key, verdict).is_some_and(|old| old != verdict) {
                    return Err("contradictory fit verdicts in the signed cache".into());
                }
            }
        }
    }
    Ok(cells)
}

fn write_rows(
    registry: &Registry,
    builds: &[Build],
    mut cells: Cells,
    judge: &mut impl Judge,
    out: &Path,
) -> Result<(), String> {
    // The output is a fresh CI scratch directory. Refuse an existing path rather than mixing runs.
    if let Some(parent) = out.parent().filter(|p| !p.as_os_str().is_empty()) {
        fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    }
    fs::create_dir(out).map_err(|e| format!("{}: {e}", out.display()))?;
    builds::write(&out.join("builds.json"), builds)?;
    for (id, project) in &registry.projects {
        if project.releases.is_empty() {
            continue;
        }
        let mut results = Fit {
            release: Vec::new(),
        };
        for release in project.releases.values() {
            let mut row = FitRelease {
                version: release.version.clone(),
                fits: Vec::new(),
                does_not_fit: Vec::new(),
            };
            for build in builds {
                let key = (
                    release.file.sha256.clone(),
                    build.id.clone(),
                    build.commit.clone(),
                );
                let verdict = match cells.entry(key) {
                    Entry::Occupied(previous) => *previous.get(),
                    Entry::Vacant(slot) => match judge.judge(&release.file, build) {
                        Ok(verdict) => *slot.insert(verdict),
                        Err(why) => {
                            eprintln!(
                                "fit unavailable: {} {} on {}: {}",
                                id.as_str(),
                                release.version,
                                build.id.as_str(),
                                escape(&why)
                            );
                            continue;
                        }
                    },
                };
                if verdict {
                    &mut row.fits
                } else {
                    &mut row.does_not_fit
                }
                .push(build.id.clone());
            }
            results.release.push(row);
        }
        let text = toml::to_string(&results).map_err(|e| e.to_string())?;
        fs::write(out.join(format!("{}.toml", id.as_str())), text).map_err(|e| e.to_string())?;
    }
    Ok(())
}

pub fn run(
    checkout: &Path,
    builds_path: &Path,
    previous: Option<&Path>,
    out: &Path,
) -> Result<(), String> {
    let registry = registry::load(checkout).map_err(|errors| errors.join("\n"))?;
    let builds = builds::read(builds_path)?;
    let cells = match previous {
        Some(path) => cache(signed::verify(
            path,
            &signed::sibling_signature(path),
            &checkout.join(signed::KEY),
        )?)?,
        None => Cells::new(),
    };
    let temp = tempfile::tempdir_in(checkout).map_err(|e| e.to_string())?;
    let scratch = temp.path().canonicalize().map_err(|e| e.to_string())?;
    write_rows(
        &registry,
        &builds,
        cells,
        &mut Tool {
            root: &scratch,
            downloads: Https,
            packages: BTreeMap::new(),
            sdks: BTreeMap::new(),
        },
        out,
    )
}

trait Download {
    fn get(&self, url: &str, file: &Path, limit: u64) -> Result<(), String>;
}

struct Https;
impl Download for Https {
    fn get(&self, url: &str, file: &Path, limit: u64) -> Result<(), String> {
        let parent = file
            .parent()
            .filter(|p| !p.as_os_str().is_empty())
            .unwrap_or(Path::new("."));
        fs::create_dir_all(parent).map_err(|e| e.to_string())?;
        let temp = tempfile::NamedTempFile::new_in(parent).map_err(|e| e.to_string())?;
        let response = Command::new("curl")
            .args([
                "--disable",
                "--silent",
                "--fail",
                "--location",
                "--globoff",
                "--proto",
                "=https",
                "--proto-redir",
                "=https",
                "--write-out",
                "%{http_code}",
                "--max-filesize",
            ])
            .arg(limit.to_string())
            .arg("--output")
            .arg(temp.path())
            .arg("--")
            .arg(url)
            .output()
            .map_err(|e| format!("cannot start curl: {e}"))?;
        if response.status.code() == Some(22) && response.stdout == b"404" {
            return Err("download not found (HTTP 404)".into());
        }
        if !response.status.success() || response.stdout != b"200" {
            return Err(format!(
                "download failed: curl {}, HTTP {}",
                response.status,
                String::from_utf8_lossy(&response.stdout)
            ));
        }
        if temp.as_file().metadata().map_err(|e| e.to_string())?.len() > limit {
            return Err("download exceeds the size limit".into());
        }
        temp.persist(file).map_err(|e| e.to_string())?;
        Ok(())
    }
}

struct Tool<'a, D> {
    root: &'a Path,
    downloads: D,
    packages: BTreeMap<Sha256, (PathBuf, Manifest)>,
    sdks: BTreeMap<Commit, Result<Rc<Sdk>, String>>,
}

fn package(
    downloads: &impl Download,
    root: &Path,
    asset: &Asset,
) -> Result<(PathBuf, Manifest), String> {
    if asset.size == 0 || asset.size > MAX_FILE {
        return Err("package size is outside the registry's package limit".into());
    }
    let file = root.join(format!("{}.smod", asset.sha256.as_str()));
    downloads.get(asset.url.as_str(), &file, asset.size)?;
    let bytes = fs::read(&file).map_err(|e| e.to_string())?;
    if bytes.len() as u64 != asset.size {
        return Err("package size does not match its release record".into());
    }
    if sha256_hex(&bytes) != asset.sha256.as_str() {
        return Err("package does not match its release record's SHA-256".into());
    }
    let manifest = Package::read(&file)?.manifest;
    Ok((file, manifest))
}

fn sdk_file(
    downloads: &impl Download,
    mirror: &Path,
    commit: &Commit,
    name: &str,
    limit: u64,
) -> Result<PathBuf, String> {
    let tag = format!("sdk-{}", commit.as_str());
    let path = mirror.join(&tag).join(name);
    downloads.get(
        &format!("https://github.com/{SUNRISE}/releases/download/{tag}/{name}"),
        &path,
        limit,
    )?;
    Ok(path)
}

struct Sdk {
    content_api: String,
    game: String,
    surface_bytes: Vec<u8>,
    surface: Result<Surface, String>,
}

fn sdk(downloader: &impl Download, mirror: &Path, commit: &Commit) -> Result<Rc<Sdk>, String> {
    let bytes = |name| {
        fs::read(sdk_file(downloader, mirror, commit, name, SMALL)?).map_err(|e| e.to_string())
    };
    let sums = bytes(SUMS)?;
    let signature = String::from_utf8(bytes(SIGNATURE)?).map_err(|e| e.to_string())?;
    let expected = sdk_digest(
        commit.as_str(),
        sdk::INDEX,
        Some(&sums),
        Some(&signature),
        &trusted_keys_for(SUNRISE),
    )?;
    let index = bytes(sdk::INDEX)?;
    check(sdk::INDEX, &expected, &sha256_hex(&index))?;
    let index = sdk::Index::read_for_fit(&index, commit.as_str()).map_err(|e| e.to_string())?;
    let surface_bytes = bytes(SURFACE)?;
    index
        .verify(SURFACE, surface_bytes.as_slice())
        .map_err(|e| e.to_string())?;
    Ok(Rc::new(Sdk {
        content_api: index.content_api,
        game: index.build.game,
        surface: Surface::read(&surface_bytes).map_err(|e| e.to_string()),
        surface_bytes,
    }))
}

impl<D: Download> Judge for Tool<'_, D> {
    fn judge(&mut self, asset: &Asset, build: &Build) -> Result<bool, String> {
        let mirror = self.root.join("sdk-releases");
        let target = self
            .sdks
            .entry(build.commit.clone())
            .or_insert_with(|| sdk(&self.downloads, &mirror, &build.commit))
            .as_ref()
            .map(Rc::clone)
            .map_err(Clone::clone)?;
        let (file, manifest) = match self.packages.entry(asset.sha256.clone()) {
            Entry::Occupied(entry) => entry.into_mut(),
            Entry::Vacant(entry) => entry.insert(package(&self.downloads, self.root, asset)?),
        };
        if target.content_api != manifest.api {
            return Ok(false);
        }
        let Some(native) = &manifest.native else {
            return Ok(true);
        };
        if native.game != target.game {
            return Ok(false);
        }
        if native.sunrise == build.commit.as_str() {
            return Ok(true);
        }
        if let Some(table) = native_route::manifest_game_code(manifest) {
            return Err(format!(
                "cross-build comparison for {table} needs the shared package fit API"
            ));
        }
        let bytes = Package::entry(file, USES_FILE, MAX_USES)?.ok_or("package has no uses list")?;
        let mut table = UsesTable::read(&bytes).map_err(|e| format!("{USES_FILE}: {e}"))?;
        if table.made_for != native.sunrise {
            return Err("uses list is not for the manifest's pin".into());
        }
        // The manifest has no game code. Unexpected game entries in the package are
        // not part of the typed API comparison and cannot produce either verdict.
        if table
            .entries
            .iter()
            .any(|e| e.game_code.is_some() || e.id.starts_with("game:"))
        {
            return Err("uses list includes game code outside the API comparison".into());
        }
        let surface = target.surface.as_ref().map_err(Clone::clone)?;
        // FitReport prefers known changes to Unversioned. Complete the hook evidence
        // before comparing so a known change cannot hide an incomplete comparison.
        let missing_hook_versions = |table: &UsesTable| {
            surface.hooks.iter().any(|hook| {
                table
                    .entries
                    .binary_search_by(|entry| entry.id.cmp(&hook.id))
                    .is_ok_and(|i| table.entries[i].version.is_none())
            })
        };
        if missing_hook_versions(&table) {
            if table.schema < PLAIN_SCHEMA {
                let pin = Commit::parse(native.sunrise.clone())?;
                let baseline = self
                    .sdks
                    .entry(pin.clone())
                    .or_insert_with(|| sdk(&self.downloads, &mirror, &pin))
                    .as_ref()
                    .map_err(Clone::clone)?;
                table
                    .pin_hook_versions(&baseline.surface_bytes)
                    .map_err(|e| e.to_string())?;
            }
            if missing_hook_versions(&table) {
                return Err("uses list has no version for a used hook".into());
            }
        }
        let report = table.fits(table.entries.iter(), surface);
        if let Some(note) = report.deferred {
            return Err(note);
        }
        match report.verdict {
            SurfaceFit::Fits => Ok(true),
            SurfaceFit::DoesNotFit(_) => Ok(false),
            SurfaceFit::Unversioned(_) => Err("uses list has no version for a used hook".into()),
        }
    }
}

#[cfg(test)]
mod tests;
