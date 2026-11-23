//! Signature verification shared by the catalogue bot and the independent Pages verifier.
use std::{fs, path::Path};

use minisign_verify::{PublicKey, Signature};

pub fn verified_bytes(path: &Path, sig: &Path, key: &Path) -> Result<Vec<u8>, String> {
    let bytes = fs::read(path).map_err(|e| format!("{}: {e}", path.display()))?;
    let signature =
        Signature::from_file(sig).map_err(|e| format!("invalid index signature file: {e}"))?;
    let key = PublicKey::from_file(key).map_err(|e| format!("invalid index public key: {e}"))?;
    key.verify(&bytes, &signature, false)
        .map_err(|e| format!("index signature verification failed: {e}"))?;
    Ok(bytes)
}
