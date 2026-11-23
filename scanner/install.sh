#!/usr/bin/env bash
set -euo pipefail

# Only the trusted tool step invokes this script. Run it before downloading any release.
tools=$1
mkdir -p "$tools/bin"
cd "$tools"

cargo=cargo-1.99.1-x86_64-unknown-linux-gnu
curl --fail --silent --show-error --location "https://static.rust-lang.org/dist/$cargo.tar.xz" -o cargo.tar.xz
printf '%s  %s\n' 53f3521949404d595efb06a55c12c5658d4ed1c75566f33d3d514eaebe70bd8a cargo.tar.xz | sha256sum --check
tar -xJf cargo.tar.xz -C bin --strip-components=3 "$cargo/cargo/bin/cargo"

rustc=rustc-1.99.1-x86_64-unknown-linux-gnu
curl --fail --silent --show-error --location "https://static.rust-lang.org/dist/$rustc.tar.xz" -o rustc.tar.xz
printf '%s  %s\n' 2ff05ff9c08a90861e7f6e4065b40a8b87463b4576186fd613871c3e37f2091f rustc.tar.xz | sha256sum --check
tar -xJf rustc.tar.xz --strip-components=2 "$rustc/rustc/bin/rustc" "$rustc/rustc/lib"

deny=cargo-deny-0.22.0-x86_64-unknown-linux-musl
curl --fail --silent --show-error --location "https://github.com/EmbarkStudios/cargo-deny/releases/download/0.22.0/$deny.tar.gz" -o deny.tar.gz
printf '%s  %s\n' 98c56e6a408bfa5c3c60744b6015aaa6aa08f743fdeb88386d06fca035bb1786 deny.tar.gz | sha256sum --check
tar -xzf deny.tar.gz -C bin --strip-components=1 "$deny/cargo-deny"

image=docker.io/semgrep/semgrep@sha256:6e1b04340e60e910fbb4ddbf3c857ab0918f064417ad5ac530014ccb3cf8c8f1 # 1.186.0
docker pull "$image"
printf 'SCANNER_SEMGREP_IMAGE=%s\n' "$image" >> "$GITHUB_ENV"
printf '%s/bin\n' "$tools" >> "$GITHUB_PATH"
