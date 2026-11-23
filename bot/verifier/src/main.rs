#[path = "../../src/index_signature.rs"]
mod index_signature;

use std::{path::Path, process::ExitCode};

fn main() -> ExitCode {
    let args: Vec<_> = std::env::args_os().skip(1).collect();
    match args.as_slice() {
        [cmd, index, sig, key_flag, key] if cmd == "verify-index" && key_flag == "--key" => {
            match index_signature::verified_bytes(Path::new(index), Path::new(sig), Path::new(key))
            {
                Ok(_) => ExitCode::SUCCESS,
                Err(reason) => {
                    let reason = reason
                        .escape_debug()
                        .to_string()
                        .replace("::", "%3A%3A")
                        .replace("##[", "%23%23%5B");
                    eprintln!("{reason}");
                    ExitCode::FAILURE
                }
            }
        }
        _ => {
            eprintln!("usage: registry-bot verify-index <index> <sig> --key <pub>");
            ExitCode::from(2)
        }
    }
}
