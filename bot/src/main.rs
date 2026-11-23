mod game_build;
mod github;
mod index;
mod registry;
mod release_payload;
mod schema;
mod tree;
mod watch;

use std::{ffi::OsStr, path::Path, process::ExitCode};

use time::{OffsetDateTime, format_description::well_known::Rfc3339};

const USAGE: &str = "usage: registry-bot check <checkout>\n       registry-bot index <checkout> --serial <n> --out <dir>\n       registry-bot watch <checkout> <registry-owner/repo> <out>\n       registry-bot verify-release <checkout> <registry-owner/repo> <project> <release.toml> <out>";

fn main() -> ExitCode {
    let args: Vec<_> = std::env::args_os().skip(1).collect();
    match args.as_slice() {
        [command, checkout, repo, out] if command == "watch" => {
            return finished(with_github(repo, |gh| {
                watch::run(Path::new(checkout), Path::new(out), gh)
            }));
        }
        [command, checkout, repo, id, row, out] if command == "verify-release" => {
            return finished(with_github(repo, |gh| {
                let id = schema::ProjectId::parse(
                    id.to_str().ok_or("project ID is not UTF-8")?.to_owned(),
                )?;
                watch::check(Path::new(checkout), &id, Path::new(row), Path::new(out), gh)
            }));
        }
        _ => (),
    }
    let (checkout, output) = match args.as_slice() {
        [command, checkout] if command == "check" => (checkout, None),
        [command, checkout, serial_flag, serial, out_flag, out]
            if command == "index" && serial_flag == "--serial" && out_flag == "--out" =>
        {
            let Some(serial) = serial.to_str().and_then(|s| s.parse::<u64>().ok()) else {
                eprintln!("--serial expects a nonnegative integer");
                return ExitCode::from(2);
            };
            (checkout, Some((serial, Path::new(out))))
        }
        [help] if help == OsStr::new("--help") || help == OsStr::new("-h") => {
            println!("{USAGE}");
            return ExitCode::SUCCESS;
        }
        _ => {
            eprintln!("{USAGE}");
            return ExitCode::from(2);
        }
    };
    let registry = match registry::load(Path::new(checkout)) {
        Ok(registry) => registry,
        Err(problems) => {
            for problem in problems {
                eprintln!("{problem}");
            }
            return ExitCode::FAILURE;
        }
    };
    if let Some((serial, dest)) = output {
        let result = OffsetDateTime::now_utc()
            .format(&Rfc3339)
            .map_err(std::io::Error::other)
            .and_then(|generated| index::write(&registry, serial, &generated, dest));
        if let Err(err) = result {
            eprintln!("{}: {err}", dest.join("index.json").display());
            return ExitCode::FAILURE;
        }
    }
    ExitCode::SUCCESS
}

fn with_github(
    repo: &OsStr,
    run: impl FnOnce(&github::Cli<'_>) -> Result<(), String>,
) -> Result<(), String> {
    let name = schema::RepositoryName::parse(
        repo.to_str()
            .ok_or("repository name is not UTF-8")?
            .to_owned(),
    )?;
    run(&github::Cli { registry: &name })
}

fn finished(result: Result<(), String>) -> ExitCode {
    match result {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("{}", watch::diagnostic(&error));
            ExitCode::FAILURE
        }
    }
}
