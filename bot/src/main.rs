mod index;
mod registry;
mod schema;
mod tree;

use std::{ffi::OsStr, path::Path, process::ExitCode};

use time::{OffsetDateTime, format_description::well_known::Rfc3339};

const USAGE: &str = "usage: registry-bot check <checkout>\n       registry-bot index <checkout> --serial <n> --out <dir>";

fn main() -> ExitCode {
    let args: Vec<_> = std::env::args_os().skip(1).collect();
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
