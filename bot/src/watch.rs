use std::{collections::BTreeSet, fs, path::Path};

use semver::Version;
use sunrise_mod_package::{
    Package,
    index::{MAX_FILE, sha256_file},
    page,
};
use time::OffsetDateTime;

use crate::{
    game_build::Builds,
    github::{self, Attestation, GitHub},
    registry::{self, ProjectEntry, Registry},
    release_payload::{self, Payload},
    schema::{
        self, Asset, CalendarDate, HttpsUrl, PackagePath, ProjectId, Release, RepositoryName,
        Sha256, SignerWorkflowRef, Text,
    },
};

struct Candidate<'a> {
    release: &'a github::Release,
    version: Version,
    published: OffsetDateTime,
}

fn tag_version(project: &schema::Project, tag: &str) -> Option<Version> {
    let tail = match project.path {
        PackagePath::Root(_) => tag,
        PackagePath::Folder(_) => tag
            .strip_prefix(project.mod_id.as_str())?
            .strip_prefix('/')?,
    };
    Version::parse(tail.strip_prefix('v')?).ok()
}

fn candidates<'a>(entry: &ProjectEntry, releases: &'a [github::Release]) -> Vec<Candidate<'a>> {
    let approved = entry
        .releases
        .values()
        .max_by_key(|row| (&row.published, &row.version));
    let approved_time = approved
        .and_then(|row| releases.iter().find(|r| r.tag_name == row.tag.as_str()))
        .and_then(github::Release::published);
    let mut candidates = releases
        .iter()
        .filter_map(|release| {
            let published = release.published()?;
            let version = tag_version(&entry.project, &release.tag_name)?;
            if entry.releases.contains_key(&version) {
                return None;
            }
            if let Some(row) = approved {
                let new = approved_time
                    .filter(|at| at.date().to_string() == row.published.as_str())
                    .map_or_else(
                        || published.date().to_string().as_str() > row.published.as_str(),
                        |at| (published, &version) > (at, &row.version),
                    );
                if !new {
                    return None;
                }
            }
            Some(Candidate {
                release,
                version,
                published,
            })
        })
        .collect::<Vec<_>>();
    candidates.sort_by(|a, b| (a.published, &a.version).cmp(&(b.published, &b.version)));
    if approved.is_none() {
        // No backlog: look for the newest correctly signed release, not the highest version number.
        candidates.reverse();
    }
    candidates
}

pub fn run(checkout: &Path, out: &Path, github: &dyn GitHub) -> Result<(), String> {
    let mut registry = load(checkout)?;
    fs::create_dir_all(out).map_err(|e| e.to_string())?;
    let mut summary = String::from("### Registry release watcher\n\n");
    if registry.watched.repository.is_empty() {
        summary.push_str("No repositories are registered.\n");
        return fs::write(out.join("watch.md"), summary).map_err(|e| e.to_string());
    }
    let open = github.open_branches()?;
    let mut builds = Builds::new(github)?;
    let categories = categories(&registry)?;
    let workflow = workflow(&registry)?.to_owned();
    let mut renamed = false;
    for repo in &mut registry.watched.repository {
        let live: Result<_, String> = (|| {
            let live = github.repository(repo.id)?;
            if live.id != repo.id || live.private {
                return Err("registered repository is no longer public or has the wrong ID".into());
            }
            Ok(live)
        })();
        let live = match live {
            Ok(live) => live,
            Err(reason) => {
                note(&mut summary, repo.name.as_str(), &reason);
                continue;
            }
        };
        if repo.name != live.name {
            note(
                &mut summary,
                repo.name.as_str(),
                &format!("renamed to {}", live.name.as_str()),
            );
            repo.name = live.name;
            renamed = true;
        }
        let releases = match github.releases(&repo.name) {
            Ok(releases) => releases,
            Err(reason) => {
                note(&mut summary, repo.name.as_str(), &reason);
                continue;
            }
        };
        for package in &repo.package {
            let id = &package.project;
            if has_open_release(&open, id) {
                continue;
            }
            let entry = registry
                .projects
                .get(id)
                .ok_or("registered project is missing")?;
            for candidate in candidates(entry, &releases) {
                let label = format!("{} {}", id.as_str(), candidate.version);
                let work = tempfile::tempdir().map_err(|e| e.to_string())?;
                let request = Request {
                    github,
                    entry,
                    repository: &repo.name,
                    workflow: &workflow,
                    release: candidate.release,
                    version: &candidate.version,
                };
                let assets = release_assets(&request, work.path(), None);
                let assets = match assets {
                    Ok(assets) => assets,
                    Err(reason) => {
                        note(&mut summary, &label, &reason);
                        continue;
                    }
                };
                let result = (|| {
                    let (row, payload) = prepared(
                        &request,
                        candidate.published,
                        assets,
                        work.path(),
                        &categories,
                        &mut builds,
                        None,
                    )?;
                    release_payload::source(
                        &work.path().join("package.smod"),
                        &work.path().join("source"),
                        payload.package.is_native(),
                    )?;
                    let root = out.join("releases").join(id.as_str());
                    fs::create_dir_all(&root).map_err(|e| e.to_string())?;
                    let stage = tempfile::tempdir_in(&root).map_err(|e| e.to_string())?;
                    write_release(&row, &payload, &stage.path().join(project_path(id)))?;
                    fs::rename(stage.path(), root.join(row.version.to_string()))
                        .map_err(|e| e.to_string())
                })();
                match result {
                    Ok(()) => {
                        note(&mut summary, &label, "verified; ready to open a release PR");
                        break;
                    }
                    Err(reason) => {
                        note(&mut summary, &label, &reason);
                        // A signed but malformed first release must be fixed, not replaced by older content.
                        if entry.releases.is_empty() {
                            break;
                        }
                    }
                }
            }
        }
    }
    if renamed {
        let path = out.join("rename/registry");
        fs::create_dir_all(&path).map_err(|e| e.to_string())?;
        let text = toml::to_string_pretty(&registry.watched).map_err(|e| e.to_string())?;
        fs::write(path.join("watched.toml"), text).map_err(|e| e.to_string())?;
    }
    if summary == "### Registry release watcher\n\n" {
        summary.push_str("No new releases to propose.\n");
    }
    fs::write(out.join("watch.md"), summary).map_err(|e| e.to_string())
}

fn has_open_release(open: &BTreeSet<String>, id: &ProjectId) -> bool {
    let prefix = format!("release/{}/", id.as_str());
    open.range(prefix.clone()..)
        .next()
        .is_some_and(|branch| branch.starts_with(&prefix))
}

pub fn check(
    checkout: &Path,
    id: &ProjectId,
    claim: &Path,
    out: &Path,
    github: &dyn GitHub,
) -> Result<(), String> {
    let registry = load(checkout)?;
    let entry = registry
        .projects
        .get(id)
        .ok_or("project is not registered on main")?;
    let raw = fs::read_to_string(claim).map_err(|e| e.to_string())?;
    let claim = toml::from_str::<Release>(&raw).map_err(|e| e.to_string())?;
    let live = github.repository(entry.project.repository)?;
    if live.id != entry.project.repository || live.private {
        return Err("registered repository is no longer public or has the wrong ID".into());
    }
    let releases = github.releases(&live.name)?;
    let candidate = candidates(entry, &releases)
        .into_iter()
        .find(|c| c.version == claim.version && c.release.tag_name == claim.tag.as_str())
        .ok_or(
            "the proposed release does not match a new, published tag of this registered package",
        )?;
    let workflow = workflow(&registry)?;
    let request = Request {
        github,
        entry,
        repository: &live.name,
        workflow,
        release: candidate.release,
        version: &candidate.version,
    };
    fs::create_dir_all(out).map_err(|e| e.to_string())?;
    let work = tempfile::tempdir().map_err(|e| e.to_string())?;
    let assets = release_assets(&request, work.path(), Some(&claim))?;
    let (row, payload) = prepared(
        &request,
        candidate.published,
        assets,
        work.path(),
        &categories(&registry)?,
        &mut Builds::new(github)?,
        Some(&claim),
    )?;
    if row != claim {
        return Err("release row does not match the attested assets and published metadata".into());
    }
    write_release(&row, &payload, &out.join("expected").join(project_path(id)))?;
    release_payload::source(
        &work.path().join("package.smod"),
        &out.join("source"),
        payload.package.is_native(),
    )?;
    if let Some(previous) = entry
        .releases
        .values()
        .max_by_key(|row| (&row.published, &row.version))
    {
        let release = releases
            .iter()
            .find(|r| r.tag_name == previous.tag.as_str() && r.published().is_some())
            .ok_or("last approved release is unavailable for the scanner diff")?;
        let request = Request {
            release,
            version: &previous.version,
            ..request
        };
        let previous_dir = tempfile::tempdir().map_err(|e| e.to_string())?;
        let (file, workflow) = asset(
            &request,
            "smod",
            "package.smod",
            previous_dir.path(),
            Some(&previous.file),
        )?;
        if file != previous.file || workflow != previous.workflow {
            return Err(
                "last approved package no longer matches its registry row and attestation".into(),
            );
        }
        let smod = previous_dir.path().join("package.smod");
        let package = Package::read(&smod)?;
        release_payload::source(&smod, &out.join("previous"), package.is_native())?;
    }
    Ok(())
}

fn project_path(id: &ProjectId) -> std::path::PathBuf {
    Path::new("registry/projects").join(id.as_str())
}

fn load(checkout: &Path) -> Result<Registry, String> {
    registry::load(checkout).map_err(|errors| errors.join("; "))
}

fn workflow(registry: &Registry) -> Result<&str, String> {
    registry
        .release_workflow
        .as_str()
        .split_once('@')
        .map(|(path, _)| path)
        .ok_or_else(|| "missing shared release workflow".into())
}

fn categories(registry: &Registry) -> Result<page::Categories, String> {
    let mut values = toml::Table::new();
    values.insert(
        "category".into(),
        toml::Value::try_from(&registry.categories).map_err(|e| e.to_string())?,
    );
    page::Categories::parse(&toml::to_string(&values).map_err(|e| e.to_string())?)
}

fn write_release(row: &Release, payload: &Payload, project: &Path) -> Result<(), String> {
    let releases = project.join("releases");
    fs::create_dir_all(&releases).map_err(|e| e.to_string())?;
    let text = toml::to_string_pretty(row).map_err(|e| e.to_string())?;
    fs::write(releases.join(format!("{}.toml", row.version)), text).map_err(|e| e.to_string())?;
    payload.write_page(&project.join("page"))
}

struct Request<'a> {
    github: &'a dyn GitHub,
    entry: &'a ProjectEntry,
    repository: &'a RepositoryName,
    workflow: &'a str,
    release: &'a github::Release,
    version: &'a Version,
}

struct Assets {
    package: Asset,
    page: Asset,
    workflow: SignerWorkflowRef,
}

fn release_assets(
    request: &Request<'_>,
    dir: &Path,
    claim: Option<&Release>,
) -> Result<Assets, String> {
    let (package, package_ref) =
        asset(request, "smod", "package.smod", dir, claim.map(|r| &r.file))?;
    let (page, page_ref) = asset(request, "page.tar", "page.tar", dir, claim.map(|r| &r.page))?;
    if package_ref != page_ref {
        return Err("package and page attestations name different signer workflow refs".into());
    }
    Ok(Assets {
        package,
        page,
        workflow: package_ref,
    })
}

fn asset(
    request: &Request<'_>,
    suffix: &str,
    local: &str,
    dir: &Path,
    claim: Option<&Asset>,
) -> Result<(Asset, SignerWorkflowRef), String> {
    let tag = &request.release.tag_name;
    let name = format!(
        "{}-{}.{suffix}",
        request.entry.project.mod_id.as_str(),
        request.version
    );
    let bundle_name = format!("{name}.sigstore.json");
    let file = request.release.asset(&name)?;
    let bundle = request.release.asset(&bundle_name)?;
    let (url, bundle_url) = claim.map_or(
        (
            file.browser_download_url.as_str(),
            bundle.browser_download_url.as_str(),
        ),
        |a| (a.url.as_str(), a.attestation.as_str()),
    );
    if !registry::release_download(url, tag, &name)
        || !registry::release_download(bundle_url, tag, &bundle_name)
    {
        return Err(
            "release asset or bundle does not have its canonical GitHub download URL".into(),
        );
    }
    let path = dir.join(local);
    let attestation = dir.join(format!("{local}.sigstore.json"));
    // The existing registry package download cap (sunrise_mod_package::index::MAX_FILE).
    github::download(url, &path, file.size.min(MAX_FILE))?;
    github::download(bundle_url, &attestation, bundle.size.min(MAX_FILE))?;
    let size = fs::metadata(&path).map_err(|e| e.to_string())?.len();
    if size != file.size
        || fs::metadata(&attestation).map_err(|e| e.to_string())?.len() != bundle.size
    {
        return Err("download size differs from the GitHub release asset".into());
    }
    let sha256 = sha256_file(&path)?;
    if file
        .digest
        .as_ref()
        .is_some_and(|digest| digest != &format!("sha256:{sha256}"))
    {
        return Err("download hash differs from the GitHub release asset".into());
    }
    if let Some(digest) = &bundle.digest
        && digest != &format!("sha256:{}", sha256_file(&attestation)?)
    {
        return Err("download hash differs from the GitHub attestation bundle asset".into());
    }
    if claim.is_some_and(|a| a.size != size || a.sha256.as_str() != sha256) {
        return Err("download size or hash differs from the approved/proposed release row".into());
    }
    let commit = request.github.tag_commit(request.repository, tag)?;
    let workflow = Attestation {
        repository_id: request.entry.project.repository,
        repository_name: request.repository,
        workflow_path: request.workflow,
        tag,
        commit: &commit,
    }
    .verify(&path, &attestation)?;
    Ok((
        Asset {
            url: HttpsUrl::parse(url.into())?,
            size,
            sha256: Sha256::parse(sha256)?,
            attestation: HttpsUrl::parse(bundle_url.into())?,
        },
        workflow,
    ))
}

fn prepared(
    request: &Request<'_>,
    published: OffsetDateTime,
    assets: Assets,
    dir: &Path,
    categories: &page::Categories,
    builds: &mut Builds<'_>,
    claim: Option<&Release>,
) -> Result<(Release, Payload), String> {
    let payload = release_payload::read(
        &dir.join("package.smod"),
        &dir.join("page.tar"),
        &request.entry.project,
        request.version,
        categories,
    )?;
    let sunrise = builds.resolve(
        payload.package.manifest.native.as_ref(),
        claim.map(|r| &r.sunrise),
    )?;
    let row = Release {
        version: request.version.clone(),
        tag: Text::parse(request.release.tag_name.clone())?,
        published: CalendarDate::parse(
            published.to_offset(time::UtcOffset::UTC).date().to_string(),
        )?,
        sunrise,
        workflow: assets.workflow,
        file: assets.package,
        page: assets.page,
        dependencies: payload.dependencies.clone(),
    };
    Ok((row, payload))
}

pub fn diagnostic(text: &str) -> String {
    text.escape_default()
        .to_string()
        .replace("::", "%3A%3A")
        .replace("##[", "%23%23%5B")
}

fn note(summary: &mut String, label: &str, reason: &str) {
    let literal = |s: &str| {
        diagnostic(s)
            .replace('&', "&amp;")
            .replace('<', "&lt;")
            .replace('>', "&gt;")
            .replace('@', "&#64;")
            .replace('[', "&#91;")
            .replace(']', "&#93;")
            .replace('`', "&#96;")
    };
    summary.push_str(&format!(
        "- <code>{}</code>: <code>{}</code>\n",
        literal(label),
        literal(reason)
    ));
}
#[cfg(test)]
mod tests {
    use super::*;
    use crate::schema::RepositoryId;
    use std::collections::BTreeMap;

    const FIXTURE: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/tests/fixtures/two-ports");
    const RELEASES: &str = include_str!("../tests/fixtures/watcher/releases.json");

    #[test]
    fn registration_prioritizes_newest_publication_with_the_exact_tag_shape() {
        let mut registry = load(Path::new(FIXTURE)).unwrap();
        let releases: Vec<github::Release> = serde_json::from_str(RELEASES).unwrap();
        let root = ProjectId::parse("Ab3dE9xQ".into()).unwrap();
        let folder = ProjectId::parse("Zz91kLmN".into()).unwrap();
        let root = registry.projects.get_mut(&root).unwrap();
        root.releases.clear();
        let initial = candidates(root, &releases);
        assert_eq!(initial.first().unwrap().release.tag_name, "v1.1.0");
        assert_eq!(initial.len(), 3);
        let folder = registry.projects.get_mut(&folder).unwrap();
        folder.releases.clear();
        let initial = candidates(folder, &releases);
        assert_eq!(initial.len(), 1);
        assert_eq!(initial[0].release.tag_name, "wayfinder/v2.0.0-beta.1");
    }

    #[test]
    fn latest_approval_is_the_watermark_and_open_pr_blocks_its_project() {
        let registry = load(Path::new(FIXTURE)).unwrap();
        let releases = serde_json::from_str::<Vec<github::Release>>(RELEASES).unwrap();
        let id = ProjectId::parse("Ab3dE9xQ".into()).unwrap();
        let remaining = candidates(&registry.projects[&id], &releases);
        assert_eq!(remaining.len(), 1);
        assert_eq!(remaining[0].release.tag_name, "v1.1.0");
        assert!(has_open_release(
            &BTreeSet::from(["release/Ab3dE9xQ/9.9.9".into()]),
            &id
        ));
        assert!(!has_open_release(
            &BTreeSet::from(["release/Zz91kLmN/9.9.9".into()]),
            &id
        ));
    }

    struct Recorded;

    impl GitHub for Recorded {
        fn repository(&self, id: RepositoryId) -> Result<github::Repository, String> {
            let response = match id.get() {
                123456789 => {
                    r#"{"id":123456789,"full_name":"Example/Wayfinder-Renamed","private":false}"#
                }
                987654321 => {
                    r#"{"id":987654321,"full_name":"other/port-collection","private":false}"#
                }
                _ => return Err("unexpected repository ID".into()),
            };
            serde_json::from_str(response).map_err(|e| e.to_string())
        }

        fn releases(&self, repo: &RepositoryName) -> Result<Vec<github::Release>, String> {
            assert_ne!(repo.as_str().to_ascii_lowercase(), "example/wayfinder");
            Ok(Vec::new())
        }

        fn open_branches(&self) -> Result<BTreeSet<String>, String> {
            Ok(BTreeSet::new())
        }
        fn tag_commit(&self, _: &RepositoryName, _: &str) -> Result<String, String> {
            Err("no release should be fetched".into())
        }
    }

    #[test]
    fn renames_use_numeric_identity_and_write_only_watched_names() {
        let out = tempfile::tempdir().unwrap();
        run(Path::new(FIXTURE), out.path(), &Recorded).unwrap();
        let text = fs::read_to_string(out.path().join("rename/registry/watched.toml")).unwrap();
        let rows: schema::Watched = toml::from_str(&text).unwrap();
        assert_eq!(
            rows.repository[0].name.as_str(),
            "Example/Wayfinder-Renamed"
        );
        assert_eq!(rows.repository[0].id.get(), 123456789);
        assert!(!out.path().join("releases").exists());
    }

    #[test]
    fn writes_only_the_release_row_and_resolved_page() {
        let id = ProjectId::parse("Ab3dE9xQ".into()).unwrap();
        let fixture = Path::new(FIXTURE).join(project_path(&id));
        let row: Release =
            toml::from_str(&fs::read_to_string(fixture.join("releases/1.2.0.toml")).unwrap())
                .unwrap();
        let page: schema::Page =
            toml::from_str(&fs::read_to_string(fixture.join("page/page.toml")).unwrap()).unwrap();
        let icon = fs::read(fixture.join("page").join(page.icon.as_str())).unwrap();
        let images = BTreeMap::from([(page.icon.clone(), icon.clone())]);
        let payload = Payload {
            package: Package {
                manifest: sunrise_mod_package::Manifest::parse(
                    "id='wayfinder'\nname='Wayfinder'\nversion='1.2.0'",
                )
                .unwrap(),
                icon: None,
            },
            page,
            images,
            dependencies: Vec::new(),
        };
        let out = tempfile::tempdir().unwrap();
        write_release(&row, &payload, out.path()).unwrap();
        let written = fs::read_to_string(out.path().join("releases/1.2.0.toml")).unwrap();
        assert!(toml::from_str::<Release>(&written).unwrap() == row);
        assert_eq!(fs::read(out.path().join("page/icon.png")).unwrap(), icon);
        assert!(out.path().join("page/page.toml").is_file());
        assert!(!out.path().join("project.toml").exists());
    }

    #[test]
    fn summary_escapes_actions_commands_and_line_breaks() {
        let mut out = String::new();
        note(&mut out, "bad\n::error", "##[warning]<script>");
        assert_eq!(out.lines().count(), 1);
        for bad in ["::", "##[", "<script>"] {
            assert!(!out.contains(bad));
        }
    }
}
