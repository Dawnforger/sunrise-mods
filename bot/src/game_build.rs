//! Resolve an attested package's SDK commit to a published, signed Sunrise build.
use std::fs;

use serde::Deserialize;
use sunrise_mod_package::{index, manifest::Native};
use sunrise_release_sign::{self as signing, Channel};

use crate::{
    github::{self, GitHub, Repo},
    schema::{BuildName, RepositoryName},
};

pub struct Builds<'a> {
    github: &'a dyn GitHub,
    repository: RepositoryName,
    releases: Option<Vec<github::Release>>,
}

impl<'a> Builds<'a> {
    pub fn new(github: &'a dyn GitHub) -> Result<Self, String> {
        Ok(Self {
            github,
            repository: RepositoryName::parse(signing::REPOSITORY.to_owned())?,
            releases: None,
        })
    }

    pub fn resolve(
        &mut self,
        pin: Option<&Native>,
        expected: Option<&BuildName>,
    ) -> Result<BuildName, String> {
        if self.releases.is_none() {
            self.releases = Some(self.github.releases(Repo::Named(&self.repository))?);
        }
        let releases = self
            .releases
            .as_deref()
            .ok_or("missing Sunrise release list")?;
        let mut candidates = Vec::new();
        for release in releases.iter().filter(|r| r.published().is_some()) {
            let tag = &release.tag_name;
            let (build, channel) = if let Some(channel) = Channel::of(tag) {
                if channel == Channel::Nightly
                    && tag != signing::NIGHTLY
                    && signing::nightly_date(tag).is_none_or(|date| tag.len() != 8 + date.len())
                {
                    continue;
                }
                // The exact title supplies the build ID for the Nightly alias and its dated archive.
                let Some(version) = release
                    .name
                    .as_deref()
                    .and_then(|title| signing::build_title_version(channel, title))
                else {
                    continue;
                };
                if channel == Channel::Dev && version != *tag {
                    continue;
                }
                if pin.is_some_and(|n| {
                    !version
                        .rsplit_once('-')
                        .is_some_and(|(_, short)| n.sunrise.starts_with(short))
                }) {
                    continue;
                }
                (version, Some(channel))
            } else if index::stable_tag(tag)
                .is_some_and(|(game, _)| pin.is_none_or(|n| n.game == game))
            {
                if let Some(native) = pin
                    && self.github.tag_commit(Repo::Named(&self.repository), tag)? != native.sunrise
                {
                    continue;
                }
                (tag.clone(), None)
            } else {
                continue;
            };
            if let Some(row) = expected {
                if row.as_str() != build {
                    continue;
                }
            } else if pin.is_none() && tag != signing::NIGHTLY {
                // Content-only manifests have no SDK pin. Record the signed current Nightly once;
                // release-PR checks use that exact build, not the moving alias.
                continue;
            }
            candidates.push((channel.is_some(), build, channel, release));
        }
        // Prefer a Stable release if the same commit was also published on a moving channel.
        candidates.sort_by(|a, b| a.0.cmp(&b.0).then_with(|| b.1.cmp(&a.1)));
        for (_, build, channel, release) in candidates {
            let sdk = signed_sdk(release, &self.repository, &build, channel)?;
            if !sunrise_mod_package::manifest::revision(&sdk.build.commit) {
                return Err("signed SDK descriptor has no full commit".into());
            }
            if channel.is_some()
                && !build
                    .rsplit_once('-')
                    .is_some_and(|(_, short)| sdk.build.commit.starts_with(short))
            {
                return Err("signed SDK descriptor does not match the exact build name".into());
            }
            if pin.is_none_or(|n| n.sunrise == sdk.build.commit && n.game == sdk.build.game) {
                return Ok(BuildName::parse(build)?);
            }
        }
        Err(
            "no signed Sunrise release matches the package's SDK pin and the exact requested build"
                .into(),
        )
    }
}

#[derive(Deserialize)]
struct Sdk {
    schema: u32,
    build: SdkBuild,
}

#[derive(Deserialize)]
struct SdkBuild {
    commit: String,
    game: String,
}

fn signed_sdk(
    release: &github::Release,
    repository: &RepositoryName,
    build: &str,
    channel: Option<Channel>,
) -> Result<Sdk, String> {
    const INDEX: &str = "sdk.json";
    // Same metadata limit as the existing registry's scripts/fetch_releases.py.
    const MAX_METADATA: u64 = 16 << 20;
    let dir = tempfile::tempdir().map_err(|e| e.to_string())?;
    let read = |name: &str| -> Result<Vec<u8>, String> {
        let asset = release.asset(name)?;
        let url = format!(
            "https://github.com/{}/releases/download/{}/{}",
            repository.as_str(),
            github::encode(&release.tag_name),
            name
        );
        let dest = dir.path().join(name);
        github::download(&url, &dest, asset.size.min(MAX_METADATA))?;
        let bytes = fs::read(dest).map_err(|e| e.to_string())?;
        if bytes.len() as u64 != asset.size {
            return Err(format!("{name}: size does not match the Sunrise release"));
        }
        Ok(bytes)
    };
    let sums = read(signing::SUMS)?;
    let sig = read(if channel.is_some() {
        signing::SIGNATURE_V2
    } else {
        signing::SIGNATURE
    })?;
    let sig = std::str::from_utf8(&sig).map_err(|e| e.to_string())?;
    let keys = signing::trusted_keys();
    let digest = match channel {
        Some(channel) => {
            signing::build_digest(channel, build, INDEX, Some(&sums), Some(sig), &keys)
        }
        None => signing::signed_digest(build, INDEX, Some(&sums), Some(sig), &keys),
    }?;
    let bytes = read(INDEX)?;
    signing::check(INDEX, &digest, &index::sha256(&bytes))?;
    let sdk: Sdk = serde_json::from_slice(&bytes).map_err(|e| format!("{INDEX}: {e}"))?;
    if sdk.schema != 1 {
        return Err("unsupported Sunrise SDK schema".into());
    }
    Ok(sdk)
}
