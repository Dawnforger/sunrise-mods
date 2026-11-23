use std::io::{self, Write};
use std::path::Path;

use percent_encoding::{AsciiSet, CONTROLS, utf8_percent_encode};
use semver::Version;
use serde::Serialize;

use crate::{registry::Registry, schema::*};

// Keep URL separators and unreserved characters; encode each path segment's
// other bytes (including raw %, ?, and #) rather than treating them as URL syntax.
const PATH_SEGMENT: &AsciiSet = &CONTROLS
    .add(b' ')
    .add(b'!')
    .add(b'"')
    .add(b'#')
    .add(b'$')
    .add(b'%')
    .add(b'&')
    .add(b'\'')
    .add(b'(')
    .add(b')')
    .add(b'*')
    .add(b'+')
    .add(b',')
    .add(b':')
    .add(b';')
    .add(b'<')
    .add(b'=')
    .add(b'>')
    .add(b'?')
    .add(b'@')
    .add(b'[')
    .add(b'\\')
    .add(b']')
    .add(b'^')
    .add(b'`')
    .add(b'{')
    .add(b'|')
    .add(b'}');

#[derive(Serialize)]
struct Index<'a> {
    schema: u8,
    serial: u64,
    generated: &'a str,
    categories: &'a [Category],
    projects: Vec<IndexProject<'a>>,
    revoked: &'a [Revocation],
}

#[derive(Serialize)]
struct IndexProject<'a> {
    id: &'a ProjectId,
    slug: &'a Slug,
    #[serde(rename = "mod")]
    mod_id: &'a ModId,
    kind: Kind,
    name: &'a Text,
    summary: &'a Text,
    description: &'a Text,
    authors: &'a [Text],
    owners: &'a [Login],
    license: &'a Text,
    homepage: Option<&'a Link>,
    issues: Option<&'a Link>,
    source: Option<&'a Link>,
    icon: String,
    screenshots: Vec<IndexScreenshot<'a>>,
    categories: &'a [Slug],
    repository: IndexRepository<'a>,
    workflow: &'a Text,
    releases: Vec<IndexRelease<'a>>,
}

#[derive(Serialize)]
struct IndexScreenshot<'a> {
    path: String,
    caption: &'a str,
}

#[derive(Serialize)]
struct IndexRepository<'a> {
    id: RepositoryId,
    name: &'a RepositoryName,
    path: &'a PackagePath,
}

#[derive(Serialize)]
struct IndexRelease<'a> {
    version: &'a Version,
    published: &'a CalendarDate,
    sunrise: &'a BuildName,
    workflow: &'a SignerWorkflowRef,
    fits: &'a [BuildName],
    file: &'a Asset,
    dependencies: &'a [Dependency],
}

pub fn write(registry: &Registry, serial: u64, generated: &str, dest: &Path) -> io::Result<()> {
    let projects = registry
        .projects
        .values()
        .filter_map(|entry| {
            if entry.releases.is_empty() {
                return None;
            }
            let project = &entry.project;
            let page = entry.page.as_ref()?;
            let image_url = |path: &RelativePath| {
                format!(
                    "pages/{}/{}",
                    project.id.as_str(),
                    utf8_percent_encode(path.as_str(), PATH_SEGMENT)
                )
            };
            let fit = registry.fits.get(&project.id);
            Some(IndexProject {
                id: &project.id,
                slug: &project.slug,
                mod_id: &project.mod_id,
                kind: project.kind,
                name: &page.name,
                summary: &page.summary,
                description: &page.description,
                authors: &page.authors,
                owners: &project.owners,
                license: &page.license,
                homepage: page.homepage.as_ref(),
                issues: page.issues.as_ref(),
                source: page.source.as_ref(),
                icon: image_url(&page.icon),
                screenshots: page
                    .screenshots
                    .iter()
                    .map(|shot| IndexScreenshot {
                        path: image_url(&shot.path),
                        caption: &shot.caption,
                    })
                    .collect(),
                categories: &page.categories,
                repository: IndexRepository {
                    id: project.repository,
                    name: &entry.repository_name,
                    path: &project.path,
                },
                workflow: &project.workflow,
                releases: entry
                    .releases
                    .values()
                    .map(|release| IndexRelease {
                        version: &release.version,
                        published: &release.published,
                        sunrise: &release.sunrise,
                        workflow: &release.workflow,
                        fits: fit
                            .and_then(|rows| rows.get(&release.version))
                            .map_or(&[], Vec::as_slice),
                        file: &release.file,
                        dependencies: &release.dependencies,
                    })
                    .collect(),
            })
        })
        .collect();
    let index = Index {
        schema: 2,
        serial,
        generated,
        categories: &registry.categories,
        projects,
        revoked: &registry.revoked,
    };
    std::fs::create_dir_all(dest)?;
    // Rename over the destination so an existing index.json symlink is never followed.
    let mut output = tempfile::NamedTempFile::new_in(dest)?;
    let mut writer = io::BufWriter::new(output.as_file_mut());
    serde_json::to_writer_pretty(&mut writer, &index)?;
    writer.write_all(b"\n")?;
    writer.flush()?;
    drop(writer);
    output.persist(dest.join("index.json"))?;
    Ok(())
}
