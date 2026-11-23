use super::*;
use std::{io, slice::from_ref};

fn scratch() -> tempfile::TempDir {
    let target = Path::new(env!("CARGO_MANIFEST_DIR")).join("../target");
    fs::create_dir_all(&target).unwrap();
    tempfile::tempdir_in(target).unwrap()
}

fn fixture() -> &'static Path {
    Path::new(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/tests/fixtures/two-ports"
    ))
}

fn generated(tree: &Path, id: &str) -> Fit {
    toml::from_str(&fs::read_to_string(tree.join(format!("{id}.toml"))).unwrap()).unwrap()
}

#[derive(Default)]
struct Recorded {
    calls: Vec<(Sha256, BuildName)>,
    responses: BTreeMap<(Sha256, BuildName), Result<bool, String>>,
}

impl Judge for Recorded {
    fn judge(&mut self, asset: &Asset, build: &Build) -> Result<bool, String> {
        let key = (asset.sha256.clone(), build.id.clone());
        self.calls.push(key.clone());
        self.responses.get(&key).cloned().unwrap_or(Ok(false))
    }
}

#[test]
fn cache_reuses_only_known_cells_and_recomputes_a_replaced_build() {
    let old = registry::load(fixture()).unwrap();
    let bytes = fs::read(fixture().join("index.json")).unwrap();
    let known = cache(serde_json::from_slice(&bytes).unwrap()).unwrap();
    let first = old
        .projects
        .values()
        .next()
        .unwrap()
        .releases
        .values()
        .next()
        .unwrap();
    let hash = first.file.sha256.clone();
    let mut builds = builds::read(&fixture().join("generated-fit/builds.json")).unwrap();
    let name = builds[1].id.clone();
    builds[1].commit = Commit::parse("f".repeat(40)).unwrap();
    let target = scratch();
    let output = target.path().join("fit");
    let mut judge = Recorded::default();
    write_rows(&old, &builds, known, &mut judge, &output).unwrap();
    let row = &generated(&output, "Ab3dE9xQ").release[0];
    assert_eq!(row.fits.as_slice(), from_ref(&builds[0].id));
    assert_eq!(
        row.does_not_fit,
        [builds[1].id.clone(), builds[2].id.clone()]
    );
    assert_eq!(judge.calls.len(), old.projects.len() * builds.len() - 1);
    assert!(!judge.calls.contains(&(hash.clone(), builds[0].id.clone())));
    assert!(judge.calls.contains(&(hash, name)));
}

#[test]
fn failed_cell_is_omitted_and_retried_while_known_negative_is_cached() {
    let old = registry::load(fixture()).unwrap();
    let builds = builds::read(&fixture().join("generated-fit/builds.json")).unwrap();
    let build = &builds[0];
    let hash = old
        .projects
        .values()
        .next()
        .unwrap()
        .releases
        .values()
        .next()
        .unwrap()
        .file
        .sha256
        .clone();
    let failed = (hash, build.id.clone());
    let target = scratch();
    let output = target.path().join("first");
    let mut judge = Recorded::default();
    judge
        .responses
        .insert(failed.clone(), Err("recorded missing surface".into()));
    write_rows(&old, from_ref(build), Cells::new(), &mut judge, &output).unwrap();
    let row = &generated(&output, "Ab3dE9xQ").release[0];
    assert!(row.fits.is_empty() && row.does_not_fit.is_empty());
    let row = &generated(&output, "Zz91kLmN").release[0];
    assert_eq!(row.does_not_fit.as_slice(), from_ref(&build.id));

    let second_project = old
        .projects
        .values()
        .nth(1)
        .unwrap()
        .releases
        .values()
        .next()
        .unwrap();
    let index = serde_json::json!({"schema":2,"serial":42,"builds":[build], "projects":[
        {"releases":[{"file":{"sha256":failed.0}, "fits":[], "does_not_fit":[]}]},
        {"releases":[{"file":{"sha256":second_project.file.sha256}, "fits":[], "does_not_fit":row.does_not_fit}]}
    ]});
    judge.calls.clear();
    judge.responses.insert(failed.clone(), Ok(true));
    let second = target.path().join("second");
    write_rows(
        &old,
        from_ref(build),
        cache(serde_json::from_value(index).unwrap()).unwrap(),
        &mut judge,
        &second,
    )
    .unwrap();
    assert_eq!(judge.calls, [failed]);
    assert_eq!(
        generated(&second, "Ab3dE9xQ").release[0].fits.as_slice(),
        from_ref(&build.id)
    );
    assert_eq!(
        generated(&second, "Zz91kLmN").release[0]
            .does_not_fit
            .as_slice(),
        from_ref(&build.id)
    );
}

struct RecordedDownload(&'static [u8]);
impl Download for RecordedDownload {
    fn get(&self, _url: &str, file: &Path, _limit: u64) -> Result<(), String> {
        fs::write(file, self.0).map_err(|e: io::Error| e.to_string())
    }
}

#[test]
fn package_download_checks_hash_before_reading_the_archive() {
    let old = registry::load(fixture()).unwrap();
    let original = &old
        .projects
        .values()
        .next()
        .unwrap()
        .releases
        .values()
        .next()
        .unwrap()
        .file;
    let body = b"a recorded package response that is not really a zip";
    let mut asset: Asset = serde_json::from_value(serde_json::to_value(original).unwrap()).unwrap();
    asset.size = body.len() as u64;
    let target = scratch();
    let error = package(&RecordedDownload(body), target.path(), &asset)
        .err()
        .unwrap();
    assert!(error.contains("SHA-256"), "{error}");
}

#[test]
fn legacy_missing_surface_record_is_omitted_and_retried() {
    use std::cell::Cell;

    struct PackageDownload<'a> {
        body: &'a [u8],
        requests: &'a Cell<usize>,
    }
    impl Download for PackageDownload<'_> {
        fn get(&self, url: &str, file: &Path, _limit: u64) -> Result<(), String> {
            assert!(url.ends_with(".smod"), "unexpected SDK download: {url}");
            self.requests.set(self.requests.get() + 1);
            fs::write(file, self.body).map_err(|e| e.to_string())
        }
    }

    let uses = include_bytes!("../../tests/fixtures/deferred-fit/uses.json");
    let table = UsesTable::read(uses).unwrap();
    let surface = || {
        Surface::new(
            vec![],
            vec![],
            vec![],
            vec![],
            vec![],
            vec![],
            vec![],
            vec![],
        )
    };
    let report = table.fits(table.entries.iter(), &surface());
    assert!(matches!(report.verdict, SurfaceFit::Fits));
    assert!(report.deferred.is_some());
    let pin = table.made_for;
    let manifest = format!(
        "id='wayfinder'\nname='Wayfinder'\nversion='1.2.0'\n[native]\ngame='26.3'\nsunrise='{pin}'\n"
    );
    let body = Package::write_files(
        &manifest,
        &[
            ("Cargo.toml", b"[package]\nname='wayfinder'\n"),
            ("src/lib.rs", b"pub fn read_map() {}\n"),
            (USES_FILE, uses),
        ],
    )
    .unwrap();
    let mut registry = registry::load(fixture()).unwrap();
    registry.projects.retain(|id, _| id.as_str() == "Ab3dE9xQ");
    let release = registry
        .projects
        .values_mut()
        .next()
        .unwrap()
        .releases
        .values_mut()
        .next()
        .unwrap();
    let hash = Sha256::parse(sha256_hex(&body)).unwrap();
    release.file.size = body.len() as u64;
    release.file.sha256 = hash.clone();
    let builds = builds::read(&fixture().join("generated-fit/builds.json")).unwrap();
    let build = &builds[0];
    let target = scratch();
    let requests = Cell::new(0);
    let mut cells = Cells::new();
    for attempt in 1..=2 {
        let mut tool = Tool {
            root: target.path(),
            downloads: PackageDownload {
                body: &body,
                requests: &requests,
            },
            packages: BTreeMap::new(),
            sdks: BTreeMap::from([(
                build.commit.clone(),
                Ok(Rc::new(Sdk {
                    content_api: Manifest::parse(&manifest).unwrap().api,
                    game: "26.3".into(),
                    surface_bytes: vec![],
                    surface: Ok(surface()),
                })),
            )]),
        };
        let output = target.path().join(format!("attempt-{attempt}"));
        write_rows(&registry, from_ref(build), cells, &mut tool, &output).unwrap();
        let generated = generated(&output, "Ab3dE9xQ");
        let row = &generated.release[0];
        assert!(row.fits.is_empty() && row.does_not_fit.is_empty());
        assert_eq!(requests.get(), attempt);
        let index = serde_json::json!({"schema":2,"serial":41,"builds":[build], "projects":[
            {"releases":[{"file":{"sha256":hash}, "fits":row.fits, "does_not_fit":row.does_not_fit}]}
        ]});
        cells = cache(serde_json::from_value(index).unwrap()).unwrap();
        assert!(cells.is_empty());
    }
}
