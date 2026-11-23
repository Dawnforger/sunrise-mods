use std::{
    collections::{BTreeMap, BTreeSet},
    ffi::OsStr,
    path::{Path, PathBuf},
};

use percent_encoding::percent_decode_str;
use semver::Version;

use crate::{
    schema::*,
    tree::{Entry, Tree},
};

pub struct Registry {
    pub categories: Vec<Category>,
    pub projects: BTreeMap<ProjectId, ProjectEntry>,
    pub fits: BTreeMap<ProjectId, BTreeMap<Version, Vec<BuildName>>>,
    pub revoked: Vec<Revocation>,
}

pub struct ProjectEntry {
    pub project: Project,
    pub repository_name: RepositoryName,
    pub page: Option<Page>,
    pub releases: BTreeMap<Version, Release>,
}

pub fn load(checkout: &Path) -> Result<Registry, Vec<String>> {
    let mut tree = Tree::new(checkout);
    let root = Path::new("registry");
    let mut top = match tree.entry(root) {
        Some(Entry::Directory) => tree.list(root),
        Some(Entry::File) => return Err(vec!["registry: expected a directory".to_owned()]),
        None => return Err(tree.finish()),
    };
    let watched_path = root.join("watched.toml");
    let watched = tree.toml::<Watched>(&watched_path, top.remove(OsStr::new("watched.toml")));
    let categories = tree
        .toml::<Categories>(
            &root.join("categories.toml"),
            top.remove(OsStr::new("categories.toml")),
        )
        .map_or_else(Vec::new, |value| value.category);
    let revoked_path = root.join("revoked.toml");
    let revoked = tree
        .toml::<Revocations>(&revoked_path, top.remove(OsStr::new("revoked.toml")))
        .map_or_else(Vec::new, |value| value.revoked);
    tree.toml::<ReleaseWorkflow>(
        &root.join("release-workflow.toml"),
        top.remove(OsStr::new("release-workflow.toml")),
    );
    let projects_root = root.join("projects");
    let mut project_dirs = match top.remove(OsStr::new("projects")) {
        Some(Entry::Directory) => tree.list(&projects_root),
        _ => {
            tree.problem(&projects_root, "expected a projects directory");
            Default::default()
        }
    };
    if project_dirs.remove(OsStr::new(".gitkeep")) == Some(Entry::Directory) {
        tree.problem(&projects_root.join(".gitkeep"), "expected a regular file");
    }
    let fit_dir = top.remove(OsStr::new("fit"));
    tree.unknown(root, top);

    let mut by_category = BTreeMap::new();
    for category in &categories {
        if by_category.insert(&category.id, category).is_some() {
            tree.problem(
                &root.join("categories.toml"),
                format!("duplicate category {}", category.id.as_str()),
            );
        }
    }
    let mut registrations = BTreeMap::new();
    let mut repository_ids = BTreeSet::new();
    let mut package_paths = BTreeSet::new();
    if let Some(watched) = &watched {
        for repo in &watched.repository {
            if !repository_ids.insert(repo.id) {
                tree.problem(
                    &watched_path,
                    format!("duplicate repository ID for {}", repo.name.as_str()),
                );
            }
            for package in &repo.package {
                if !package_paths.insert((repo.id, &package.path)) {
                    tree.problem(
                        &watched_path,
                        format!(
                            "duplicate package path in {} for project {}",
                            repo.name.as_str(),
                            package.project.as_str()
                        ),
                    );
                }
                if registrations
                    .insert(&package.project, (repo, package))
                    .is_some()
                {
                    tree.problem(
                        &watched_path,
                        format!(
                            "duplicate registered project ID {}",
                            package.project.as_str()
                        ),
                    );
                }
            }
        }
    }

    let mut projects = BTreeMap::new();
    let mut slugs = BTreeSet::new();
    let mut subfolder_mods = BTreeSet::new();
    for (name, entry) in project_dirs {
        let dir = projects_root.join(&name);
        let Ok(id) = ProjectId::parse(name.to_string_lossy().into_owned()) else {
            tree.problem(
                &dir,
                "expected a directory named by an 8-character base62 project ID",
            );
            continue;
        };
        if entry != Entry::Directory {
            tree.problem(&dir, "expected a project directory");
            continue;
        }
        let project_path = dir.join("project.toml");
        let mut entries = tree.list(&dir);
        let project =
            tree.toml::<Project>(&project_path, entries.remove(OsStr::new("project.toml")));
        let releases_dir = dir.join("releases");
        let releases = match entries.remove(OsStr::new("releases")) {
            Some(Entry::Directory) => read_releases(&mut tree, &releases_dir, project.as_ref()),
            Some(Entry::File) => {
                tree.problem(&releases_dir, "expected a releases directory");
                BTreeMap::new()
            }
            None => BTreeMap::new(),
        };
        let page_dir = dir.join("page");
        let page = match entries.remove(OsStr::new("page")) {
            Some(Entry::Directory) => read_page(
                &mut tree,
                &page_dir,
                &by_category,
                project.as_ref().map(|p| p.kind),
            ),
            entry => {
                if entry.is_some() || !releases.is_empty() {
                    tree.problem(
                        &page_dir,
                        "expected a page directory with page.toml and images",
                    );
                }
                None
            }
        };
        tree.unknown(&dir, entries);
        let registration = registrations.remove(&id);
        if let Some(project) = project {
            if project.id != id {
                tree.problem(&project_path, "project ID must match the directory name");
            }
            if !slugs.insert(project.slug.clone()) {
                tree.problem(
                    &project_path,
                    format!("duplicate slug {}", project.slug.as_str()),
                );
            }
            if matches!(project.path, PackagePath::Folder(_))
                && !subfolder_mods.insert((project.repository, project.mod_id.clone()))
            {
                tree.problem(&project_path, "another subfolder package in this repository has the same in-game ID (shared tag and caller workflow names)");
            }
            if project.workflow.as_str() != RELEASE_WORKFLOW {
                tree.problem(
                    &project_path,
                    "workflow must match the path in release-workflow.toml before '@'",
                );
            }
            match registration {
                Some((repo, package)) => {
                    if repo.id != project.repository {
                        tree.problem(&project_path, "repository ID does not match watched.toml");
                    }
                    if package.path != project.path {
                        tree.problem(&project_path, "package path does not match watched.toml");
                    }
                    projects.insert(
                        id,
                        ProjectEntry {
                            project,
                            repository_name: repo.name.clone(),
                            page,
                            releases,
                        },
                    );
                }
                None if watched.is_some() => {
                    tree.problem(&project_path, "project is not registered in watched.toml")
                }
                None => {}
            }
        }
    }
    for id in registrations.keys() {
        tree.problem(
            &watched_path,
            format!("project {} has no project.toml", id.as_str()),
        );
    }
    for (id, entry) in &projects {
        for release in entry.releases.values() {
            let path = projects_root
                .join(id.as_str())
                .join("releases")
                .join(format!("{}.toml", release.version));
            for dependency in &release.dependencies {
                if let Some(target) = &dependency.project {
                    match projects.get(target) {
                        None => tree.problem(
                            &path,
                            format!("dependency names unregistered project {}", target.as_str()),
                        ),
                        Some(provider) if provider.project.mod_id != dependency.mod_id => tree
                            .problem(
                                &path,
                                format!(
                                    "dependency project {} does not provide {}",
                                    target.as_str(),
                                    dependency.mod_id.as_str()
                                ),
                            ),
                        Some(_) => {}
                    }
                }
            }
        }
    }
    let mut revocations = BTreeSet::new();
    for row in &revoked {
        if !revocations.insert((&row.project, &row.sha256)) {
            tree.problem(
                &revoked_path,
                format!(
                    "duplicate takedown for project {} and hash",
                    row.project.as_str()
                ),
            );
        }
    }
    let fits = read_fits(&mut tree, &root.join("fit"), fit_dir, &projects);
    let problems = tree.finish();
    if !problems.is_empty() {
        return Err(problems);
    }
    Ok(Registry {
        categories,
        projects,
        fits,
        revoked,
    })
}

fn read_releases(
    tree: &mut Tree<'_>,
    dir: &Path,
    project: Option<&Project>,
) -> BTreeMap<Version, Release> {
    let mut releases = BTreeMap::new();
    for (name, entry) in tree.list(dir) {
        let file = dir.join(name);
        let version = file
            .file_stem()
            .and_then(OsStr::to_str)
            .and_then(|stem| Version::parse(stem).ok());
        let Some(version) = version.filter(|_| file.extension() == Some(OsStr::new("toml"))) else {
            tree.problem(
                &file,
                "expected a <version>.toml release file named by a semantic version",
            );
            continue;
        };
        let Some(row) = tree.toml::<Release>(&file, Some(entry)) else {
            continue;
        };
        if version != row.version {
            tree.problem(&file, "release version must match its filename");
        }
        if let Some(project) = project {
            let expected = match project.path {
                PackagePath::Root(_) => format!("v{}", row.version),
                PackagePath::Folder(_) => format!("{}/v{}", project.mod_id.as_str(), row.version),
            };
            if row.tag.as_str() != expected {
                tree.problem(&file, format!("tag must be {expected}"));
            }
            if row.workflow.as_str().split_once('@').map(|(path, _)| path)
                != Some(project.workflow.as_str())
            {
                tree.problem(&file, "signer workflow path must match project.toml");
            }
            for (field, asset, suffix) in
                [("file", &row.file, "smod"), ("page", &row.page, "page.tar")]
            {
                let name = format!("{}-{}.{suffix}", project.mod_id.as_str(), row.version);
                let bundle = format!("{name}.sigstore.json");
                for (key, url, name) in [
                    ("url", &asset.url, &name),
                    ("attestation", &asset.attestation, &bundle),
                ] {
                    if !release_download(url.as_str(), row.tag.as_str(), name) {
                        tree.problem(&file, format!("{field}.{key} must be a GitHub release download HTTPS URL for {name} with the release tag"));
                    }
                }
            }
        }
        releases.insert(version, row);
    }
    releases
}

fn release_download(url: &str, tag: &str, asset: &str) -> bool {
    // Check the raw authority and repository path, then decode only the release tag and asset.
    // GitHub's browser_download_url can percent-encode a tag such as wayfinder/v1.2.0.
    let Some((repository, download)) = url
        .strip_prefix("https://github.com/")
        .and_then(|path| path.split_once("/releases/download/"))
    else {
        return false;
    };
    github_repository(repository)
        && !download.contains(['?', '#'])
        && download
            .rsplit_once('/')
            .is_some_and(|(tag_part, asset_part)| {
                percent_decode_str(tag_part).eq(tag.bytes())
                    && percent_decode_str(asset_part).eq(asset.bytes())
            })
}

fn read_page(
    tree: &mut Tree<'_>,
    dir: &Path,
    categories: &BTreeMap<&Slug, &Category>,
    kind: Option<Kind>,
) -> Option<Page> {
    let mut entries = tree.list(dir);
    let file = dir.join("page.toml");
    let page = tree.toml::<Page>(&file, entries.remove(OsStr::new("page.toml")));
    let mut assets = BTreeSet::<PathBuf>::new();
    let mut pending = vec![(dir.to_owned(), entries)];
    while let Some((dir, entries)) = pending.pop() {
        for (name, entry) in entries {
            let path = dir.join(name);
            match entry {
                Entry::Directory => pending.push((path.clone(), tree.list(&path))),
                Entry::File => {
                    assets.insert(path);
                }
            }
        }
    }
    let page = page?;
    // The thresholds are from Modrinth validate/project/{summary.rs:13,description.rs:17}.
    // Counting trimmed Unicode scalar values matches the author-side release check.
    if page.summary.as_str().trim().chars().count() < 25 {
        tree.problem(&file, "summary needs at least 25 characters");
    }
    if page.description.as_str().trim().chars().count() < 125 {
        tree.problem(&file, "description needs at least 125 characters");
    }
    // Modrinth validate/project/tags.rs:10.
    if page.categories.len() > 8 {
        tree.problem(&file, "at most 8 categories are allowed");
    }
    let mut selected = BTreeSet::new();
    for id in &page.categories {
        if !selected.insert(id) {
            tree.problem(&file, format!("duplicate category {}", id.as_str()));
        }
        match categories.get(id) {
            None => tree.problem(&file, format!("unknown category {}", id.as_str())),
            Some(category) if kind.is_some_and(|kind| !category.kinds.contains(&kind)) => {
                tree.problem(
                    &file,
                    format!(
                        "category {} does not apply to this project kind",
                        id.as_str()
                    ),
                );
            }
            Some(_) => {}
        }
    }
    if page.screenshots.is_empty() {
        tree.problem(&file, "at least one screenshot is required");
    }
    let images: BTreeSet<_> = std::iter::once(&page.icon)
        .chain(page.screenshots.iter().map(|s| &s.path))
        .collect();
    for image in images {
        let path = dir.join(image.as_str());
        if assets.remove(&path) {
            if image == &page.icon {
                tree.icon(&path);
            } else {
                tree.screenshot(&path);
            }
        } else {
            tree.problem(
                &file,
                format!(
                    "image {:?} is not a regular file in the page directory",
                    image.as_str()
                ),
            );
        }
    }
    for asset in assets {
        tree.problem(&asset, "page file is not referenced by page.toml");
    }
    Some(page)
}

fn read_fits(
    tree: &mut Tree<'_>,
    dir: &Path,
    entry: Option<Entry>,
    projects: &BTreeMap<ProjectId, ProjectEntry>,
) -> BTreeMap<ProjectId, BTreeMap<Version, Vec<BuildName>>> {
    let mut fits = BTreeMap::new();
    match entry {
        Some(Entry::Directory) => {}
        Some(Entry::File) => {
            tree.problem(dir, "expected a fit directory");
            return fits;
        }
        None => return fits,
    }
    for (name, entry) in tree.list(dir) {
        let file = dir.join(name);
        let id = file
            .file_stem()
            .and_then(OsStr::to_str)
            .and_then(|stem| ProjectId::parse(stem.to_owned()).ok());
        let Some(id) = id.filter(|_| file.extension() == Some(OsStr::new("toml"))) else {
            tree.problem(
                &file,
                "expected a <project>.toml fit file named by a base62 project ID",
            );
            continue;
        };
        let Some(fit) = tree.toml::<Fit>(&file, Some(entry)) else {
            continue;
        };
        let Some(project) = projects.get(&id) else {
            tree.problem(&file, "fit file names an unregistered project");
            continue;
        };
        let mut releases = BTreeMap::new();
        for row in fit.release {
            if !project.releases.contains_key(&row.version) {
                tree.problem(
                    &file,
                    format!("fit row names an unknown release {}", row.version),
                );
            }
            let builds: BTreeSet<_> = row.fits.iter().collect();
            if builds.len() != row.fits.len() {
                tree.problem(
                    &file,
                    format!("duplicate build in fits for {}", row.version),
                );
            }
            if releases.insert(row.version.clone(), row.fits).is_some() {
                tree.problem(&file, format!("duplicate fit row for {}", row.version));
            }
        }
        fits.insert(id, releases);
    }
    fits
}
