mod builds;
mod fit;
mod index;
mod registry;
mod schema;
mod signed;
mod tree;

use std::{
    ffi::{OsStr, OsString},
    path::Path,
    process::{Command, ExitCode},
};

use time::{OffsetDateTime, format_description::well_known::Rfc3339};

const USAGE: &str = "usage: registry-bot check <checkout>
       registry-bot index <checkout> --serial <n> --out <dir> [--generated-fit] [--after <previous index.json>]
       registry-bot builds --out <file>
       registry-bot fit <checkout> --builds <file> [--cache <previous index.json>] --out <dir>
       registry-bot verify-index <index> <sig> --key <pub>
       registry-bot new-builds <builds file> <live index> --key <pub>";

fn main() -> ExitCode {
    let args: Vec<_> = std::env::args_os().skip(1).collect();
    let result = match args.as_slice() {
        [command, checkout] if command == "check" => registry::load(Path::new(checkout))
            .map(|_| ())
            .map_err(|e| e.join("\n")),
        [
            command,
            checkout,
            serial_flag,
            serial,
            out_flag,
            out,
            tail @ ..,
        ] if command == "index" && serial_flag == "--serial" && out_flag == "--out" => {
            let Some(serial) = serial.to_str().and_then(|s| s.parse::<u64>().ok()) else {
                eprintln!("--serial expects a nonnegative integer");
                return ExitCode::from(2);
            };
            let (generated_fit, previous) = match tail {
                [] => (false, None),
                [flag] if flag == "--generated-fit" => (true, None),
                [flag, after, previous] if flag == "--generated-fit" && after == "--after" => {
                    (true, Some(Path::new(previous)))
                }
                _ => return usage(),
            };
            write_index(
                Path::new(checkout),
                serial,
                Path::new(out),
                generated_fit,
                previous,
            )
        }
        [command, out_flag, out] if command == "builds" && out_flag == "--out" => {
            builds::discover(Path::new(out))
        }
        [command, checkout, builds_flag, builds, tail @ ..]
            if command == "fit" && builds_flag == "--builds" =>
        {
            let (previous, out) = match tail {
                [flag, out] if flag == "--out" => (None, out),
                [cache, previous, flag, out] if cache == "--cache" && flag == "--out" => {
                    (Some(Path::new(previous)), out)
                }
                _ => return usage(),
            };
            fit::run(
                Path::new(checkout),
                Path::new(builds),
                previous,
                Path::new(out),
            )
        }
        [command, index, sig, flag, pubkey] if command == "verify-index" && flag == "--key" => {
            signed::verify(Path::new(index), Path::new(sig), Path::new(pubkey)).map(|_| ())
        }
        [command, builds, index, flag, pubkey] if command == "new-builds" && flag == "--key" => {
            new_builds(Path::new(builds), Path::new(index), Path::new(pubkey))
        }
        [help] if help == OsStr::new("--help") || help == OsStr::new("-h") => {
            println!("{USAGE}");
            return ExitCode::SUCCESS;
        }
        _ => return usage(),
    };
    match result {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            if args.first().is_some_and(|command| command == "check") {
                // Layout diagnostics have already been escaped and arranged one per line.
                eprintln!("{error}");
            } else {
                eprintln!("{}", escape(&error));
            }
            ExitCode::FAILURE
        }
    }
}

fn usage() -> ExitCode {
    eprintln!("{USAGE}");
    ExitCode::from(2)
}

fn write_index(
    checkout: &Path,
    serial: u64,
    out: &Path,
    generated_fit: bool,
    previous: Option<&Path>,
) -> Result<(), String> {
    if let Some(path) = previous {
        let live = signed::verify(
            path,
            &signed::sibling_signature(path),
            &checkout.join(signed::KEY),
        )?;
        signed::require_new_serial(serial, live.serial)?;
    }
    let load = if generated_fit {
        registry::load_generated
    } else {
        registry::load
    };
    let registry = load(checkout).map_err(|e| e.join("; "))?;
    let generated = OffsetDateTime::now_utc()
        .format(&Rfc3339)
        .map_err(|e| e.to_string())?;
    index::write(&registry, checkout, serial, &generated, out)
        .map_err(|e| format!("{}: {e}", out.join("index.json").display()))
}

fn new_builds(path: &Path, previous: &Path, key: &Path) -> Result<(), String> {
    let builds = builds::read(path)?;
    let live = signed::verify(previous, &signed::sibling_signature(previous), key)?;
    println!(
        "{}",
        builds.iter().any(|build| !live.builds.contains(build))
    );
    Ok(())
}

fn escape(reason: &str) -> String {
    reason
        .escape_debug()
        .to_string()
        .replace("::", "%3A%3A")
        .replace("##[", "%23%23%5B")
}

fn command_output(cmd: &mut Command) -> Result<Vec<u8>, String> {
    let name = OsString::from(cmd.get_program());
    let output = cmd
        .output()
        .map_err(|e| format!("{}: {e}", name.to_string_lossy()))?;
    if !output.status.success() {
        return Err(format!(
            "{} failed ({}): {}",
            name.to_string_lossy(),
            output.status,
            String::from_utf8_lossy(&output.stderr)
        ));
    }
    Ok(output.stdout)
}
