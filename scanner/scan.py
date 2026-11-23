#!/usr/bin/env python3
"""Read one unpacked release. Never run commands from the release or in its directory."""

import argparse
from collections import defaultdict
import hashlib
import html
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import tomllib

SCANNER = Path(__file__).resolve().parent
CRATES_IO = "registry+https://github.com/rust-lang/crates.io-index"
SPARSE_IO = "sparse+https://index.crates.io/"
DEPENDENCY_TABLES = ("dependencies", "dev-dependencies", "dev_dependencies",
                     "build-dependencies", "build_dependencies")


class ScanError(Exception):
    pass


def safe(value):
    # No Markdown, HTML, mentions, workflow commands, or control characters from source.
    quoted = json.dumps(str(value), ensure_ascii=True)[1:-1]
    return "<code>" + html.escape(quoted).replace("@", "&#64;").replace(":", "&#58;") + "</code>"


def run(args, cwd, env=None):
    return subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, errors="replace")


def require_success(result, tool):
    if result.returncode:
        raise ScanError(f"{tool} did not complete (exit {result.returncode}): {result.stderr.strip()}")


def release_files(root):
    if not root.is_dir() or root.is_symlink():
        raise ScanError("The release input must be an unpacked directory, not a link.")
    files = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not (path.is_dir() or path.is_file()):
            raise ScanError(f"Release contains a link or non-regular file: {path.relative_to(root)}")
        if path.is_file():
            files[path.relative_to(root)] = path
    return files


def read_toml(path):
    try:
        return tomllib.loads(path.read_text())
    except (UnicodeError, ValueError) as error:
        raise ScanError(f"Invalid TOML: {path.name}: {error}") from None


def lock_packages(files):
    if Path("Cargo.lock") not in files:
        return []
    packages = read_toml(files[Path("Cargo.lock")]).get("package", [])
    if not isinstance(packages, list):
        raise ScanError("Expected Cargo.lock packages to be an array of tables.")
    for p in packages:
        if (not isinstance(p, dict) or not isinstance(p.get("name"), str)
                or not re.fullmatch(r"[a-zA-Z0-9_-]+", p["name"])
                or not isinstance(p.get("version"), str)
                or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][a-zA-Z0-9.+-]+)?", p["version"])
                or not isinstance(p.get("source", ""), str)
                or not isinstance(p.get("checksum", ""), str)):
            raise ScanError("Invalid package record in Cargo.lock.")
    return packages


def lock_locations(path, packages):
    # Cargo writes a header per package. Noncanonical TOML still has a file location.
    headers = [line for line, text in enumerate(path.read_text().splitlines(), 1)
               if re.match(r"^\s*\[\[\s*package\s*\]\]", text)]
    if len(headers) != len(packages):
        headers = [1] * len(packages)
    return {(p["name"], p["version"]): f"Cargo.lock:{line}" for p, line in zip(packages, headers)}


def source_policy(root, manifests, packages, failures):
    for p in packages:
        if p.get("source", "") not in ("", CRATES_IO, SPARSE_IO):
            failures.append(("non-crates-io", f"Cargo.lock: {p['name']} {p['version']} has source {p['source']}"))
        elif p.get("source") and not re.fullmatch(r"[0-9a-f]{64}", p.get("checksum", "")):
            failures.append(("lockfile-out-of-date", f"Cargo.lock: {p['name']} {p['version']} has no valid checksum"))
    for path, manifest in manifests.items():
        workspace = manifest.get("workspace", {})
        tables = [manifest, workspace, *manifest.get("target", {}).values()]
        dependencies = [table.get(kind, {}) for table in tables for kind in DEPENDENCY_TABLES]
        dependencies.append(manifest.get("replace", {}))
        for source, patched in manifest.get("patch", {}).items():
            if source not in ("crates-io", "https://github.com/rust-lang/crates.io-index", "https://index.crates.io/"):
                failures.append(("non-crates-io", f"{path}: patch targets {source}"))
            dependencies.append(patched)
        for table in dependencies:
            for name, dep in table.items():
                if isinstance(dep, dict) and ("git" in dep or "registry-index" in dep
                        or dep.get("registry", "crates-io") != "crates-io"):
                    failures.append(("non-crates-io", f"{path}: {name} declares a non-crates.io source"))
        path_fields = [
            (name, dep["path"]) for table in dependencies for name, dep in table.items()
            if isinstance(dep, dict) and "path" in dep
        ]
        if "workspace" in manifest.get("package", {}):
            path_fields.append(("package.workspace", manifest["package"]["workspace"]))
        path_fields.extend((f"workspace.{key}", member) for key in ("members", "default-members", "exclude")
                           for member in workspace.get(key, []))
        for name, dependency_path in path_fields:
            dest = root / path.parent / dependency_path
            if Path(dependency_path).is_absolute() or not dest.resolve().is_relative_to(root):
                failures.append(("non-crates-io", f"{path}: {name} points outside the package ({dependency_path})"))


def semgrep(files, work, flags):
    src = work / "rust"
    src.mkdir()
    # A flat scan-only snapshot defeats author ignore files, hidden paths and unsafe filenames.
    paths = {}
    for name, path in files.items():
        if name.suffix == ".rs":
            key = f"{len(paths)}.rs"
            paths[key] = name
            shutil.copyfile(path, src / key)
    if not paths:
        return
    image = os.getenv("SCANNER_SEMGREP_IMAGE")
    if image:
        command = ["docker", "run", "--rm", "--pull=never", "--network=none", "--entrypoint=semgrep",
                   "-v", f"{src}:/inputs:ro", "-v", f"{SCANNER / 'rules'}:/rules:ro", image]
        rules, target = "/rules/rust.yml", "/inputs"
    else:
        command = ["semgrep"]
        rules, target = str(SCANNER / "rules/rust.yml"), str(src)
    result = run(command + ["scan", "--json", "--strict", "--metrics=off", "--disable-version-check",
                            "--disable-nosem", "--no-git-ignore", "--max-target-bytes=0", "--timeout=0",
                            "--no-rewrite-rule-ids", "--config", rules, target], work)
    require_success(result, "Semgrep")
    report = json.loads(result.stdout)
    if report["errors"] or {Path(path).name for path in report["paths"]["scanned"]} != paths.keys():
        raise ScanError("Semgrep did not scan every Rust file without errors.")
    for match in report["results"]:
        flags[match["check_id"]].append((f"{paths[Path(match['path']).name]}:{match['start']['line']}",
                                        match["extra"]["message"]))


def locked_graph(stub, packages):
    # cargo-deny can take Cargo metadata JSON. Every registry lock record is a direct
    # input to these package/version checks, including optional and target-specific crates.
    root_id = f"path+{stub.as_uri()}#scanner-root@0.0.0"
    def package(identifier, name, version, source=None):
        return {"id": identifier, "name": name, "version": version, "source": source,
                "manifest_path": str(stub / "Cargo.toml"), "targets": [], "dependencies": [], "features": {}}
    root = package(root_id, "scanner-root", "0.0.0")
    nodes, deps, entries = [], [], []
    for p in packages:
        if not p.get("source"):
            continue
        name, version = p["name"], p["version"]
        identifier = f"{SPARSE_IO}#{name}@{version}"
        entries.append(package(identifier, name, version, SPARSE_IO))
        rename = f"locked_{len(deps)}"
        root["dependencies"].append({"name": name, "source": SPARSE_IO, "req": f"={version}",
                                     "rename": rename, "optional": False, "uses_default_features": False,
                                     "features": []})
        deps.append({"name": rename, "pkg": identifier, "dep_kinds": [{"kind": None, "target": None}]})
        nodes.append({"id": identifier, "dependencies": [], "deps": [], "features": []})
    nodes.append({"id": root_id, "dependencies": [dep["pkg"] for dep in deps], "deps": deps, "features": []})
    return {"packages": [root, *entries], "workspace_members": [root_id], "workspace_default_members": [root_id],
            "workspace_root": str(stub), "target_directory": str(stub / "target"), "version": 1,
            "resolve": {"root": root_id, "nodes": nodes}}


def dependency_scan(source, packages, locations, work, failures, flags, note):
    cargo_home = work / "cargo-home"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CARGO_", "RUSTUP_", "RUSTC"))}
    env.update(CARGO_HOME=str(cargo_home), CARGO_TERM_COLOR="never", CARGO=str(shutil.which("cargo")),
               CARGO_REGISTRIES_CRATES_IO_PROTOCOL="sparse", LC_ALL="C")
    stub = work / "graph"
    (stub / "src").mkdir(parents=True)
    manifest = stub / "Cargo.toml"
    manifest.write_text('[package]\nname = "scanner-root"\nversion = "0.0.0"\nedition = "2024"\n')
    (stub / "src/lib.rs").touch()
    (stub / "Cargo.lock").write_text('version = 4\n[[package]]\nname = "scanner-root"\nversion = "0.0.0"\n')
    metadata = work / "metadata.json"
    metadata.write_text(json.dumps(locked_graph(stub, packages)))
    deny = ["cargo-deny", "--manifest-path", str(manifest), "--format", "json"]
    require_success(run(deny + ["--config", str(SCANNER / "deny.toml"), "fetch", "db"], work, env), "RustSec fetch")
    checkout, = (cargo_home / "advisory-dbs").glob("*/.git")
    db = checkout.parent
    revision = run(["git", "rev-parse", "HEAD"], db)
    require_success(revision, "RustSec revision")
    head = revision.stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        raise ScanError("RustSec checkout returned an invalid commit ID.")
    print(f"RustSec advisory database: {head}")
    note.append(f"RustSec advisory database: {safe(head)}.")
    # The first pass has no index yet. It must catch removed malware without fetching it.
    config = work / "deny-no-yanks.toml"
    config.write_text((SCANNER / "deny.toml").read_text().replace("[advisories]\n", "[advisories]\ndisable-yank-checking = true\n"))
    for with_index in (False, True):
        if with_index:
            result = run(["cargo", "fetch", "--locked", "--manifest-path", str(source / "Cargo.toml")], work, env)
            if result.returncode and re.search(r"(?:cannot update the lock file .*because --locked was passed|the lock file .* needs to be updated but --locked was passed)", result.stderr):
                failures.append(("lockfile-out-of-date", "Cargo.lock does not match the manifests; Cargo --locked refused it."))
                return
            require_success(result, "Cargo --locked")
        result = run(deny + ["--frozen", "--metadata-path", str(metadata), "--config",
                             str(SCANNER / "deny.toml" if with_index else config), "check", "--show-stats",
                             "--hide-inclusion-graph", "advisories", "sources"], work, env)
        if result.returncode not in (0, 1):
            require_success(result, "cargo-deny")
        events = [json.loads(line) for line in (result.stderr + "\n" + result.stdout).splitlines() if line.strip()]
        if not any(event["type"] == "summary" for event in events):
            raise ScanError(f"cargo-deny did not complete: {result.stderr.strip()}")
        for event in events:
            if event["type"] != "diagnostic":
                continue
            diag = event["fields"]
            code = diag.get("code")
            advisory = diag.get("advisory")
            if advisory or code == "yanked":
                # These diagnostics label the exact affected package in cargo-deny's synthetic lock.
                package = tuple(diag["labels"][0]["span"].split()[:2])
                location = locations[package]
            if code == "source-not-allowed":
                failures.append(("non-crates-io", f"Cargo.lock: {diag['message']}"))
            elif advisory and "malicious" in advisory["categories"]:
                failures.append(("rustsec-malicious", f"{location}: {advisory['package']} — {advisory['id']}: {advisory['title']}"))
            elif advisory and not with_index:
                flags["RustSec advisories"].append((location, f"{advisory['package']} — {advisory['id']}: {advisory['title']}"))
            elif code == "yanked":
                flags["Yanked dependencies"].append((location, f"{package[0]} {package[1]} is yanked."))
            elif code in ("index-failure", "index-cache-load-failure") or (not advisory and diag["severity"] in ("bug", "error")):
                raise ScanError(f"cargo-deny: {diag['message']}")
        if failures:
            return


def build_targets(files, manifests):
    scripts, macros = set(), set()
    for path, manifest in manifests.items():
        build = manifest.get("package", {}).get("build", True)
        if build is not False:
            script = path.parent / (build if isinstance(build, str) else "build.rs")
            if script in files or isinstance(build, str):
                scripts.add(script)
        lib = manifest.get("lib", {})
        if lib.get("proc-macro") or lib.get("proc_macro") or "proc-macro" in lib.get("crate-type", []):
            macros.add(path.parent)
    return scripts, macros


def release_diff(files, packages, locations, manifests, scripts, macros, prior, flags):
    if prior is None:
        for package in packages:
            if package.get("source"):
                name, version = package["name"], package["version"]
                flags["New or changed dependencies"].append((locations[name, version], f"{name} {version}"))
        return ["No previous approved release; every file, dependency, build script and proc-macro crate is new."]
    old = release_files(prior)
    old_manifests = {p: read_toml(f) for p, f in old.items() if p.name == "Cargo.toml"}
    old_scripts, old_macros = build_targets(old, old_manifests)
    added = files.keys() - old.keys()
    removed = old.keys() - files.keys()
    changed = {p for p in files.keys() & old.keys() if sha(files[p]) != sha(old[p])}
    lines = [f"- {verb} {safe(path)}" for verb, paths in (("Added", added), ("Removed", removed), ("Changed", changed))
             for path in sorted(paths)]
    new_lock, old_lock = (lock_versions(data) for data in (packages, lock_packages(old)))
    for name in sorted(new_lock.keys() | old_lock.keys()):
        incoming, outgoing = new_lock.get(name, set()), old_lock.get(name, set())
        if incoming != outgoing:
            verb = "Added" if not outgoing else "Removed" if not incoming else "Changed"
            before = ", ".join(sorted(outgoing - incoming)) or "none"
            after = ", ".join(sorted(incoming - outgoing)) or "none"
            lines.append(f"- {verb} dependency {safe(name)}: {safe(before)} → {safe(after)}")
            for entry in sorted(incoming - outgoing):
                version = entry.split()[0]
                flags["New or changed dependencies"].append((locations[name, version], f"{name}: {entry}"))
    modified = added | removed | changed
    crate_roots = {p.parent for p in manifests | old_manifests}
    modified_crates = {next((parent for parent in p.parents if parent in crate_roots), None) for p in modified}
    for label, paths in (("Build scripts", modified & (scripts | old_scripts)),
                         ("Proc-macro crates", modified_crates & (macros | old_macros))):
        lines.append(f"- {label} changed: " + (", ".join(safe(p) for p in sorted(paths)) if paths else "no"))
    return lines


def sha(path):
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def lock_versions(packages):
    result = defaultdict(set)
    for p in packages:
        if p.get("source"):
            result[p["name"]].add(f"{p['version']} ({p['source']}; checksum {p.get('checksum', 'none')})")
    return result


def scan(source, prior, work, failures, flags, note):
    files = release_files(source)
    manifests = {p: read_toml(f) for p, f in files.items() if p.name == "Cargo.toml"}
    packages = lock_packages(files)
    if (manifests or Path("Cargo.lock") in files) and not {Path("Cargo.toml"), Path("Cargo.lock")} <= files.keys():
        failures.append(("lockfile-out-of-date", "A Rust release must include a root Cargo.toml and Cargo.lock."))
    source_policy(source, manifests, packages, failures)
    semgrep(files, work, flags)
    scripts, macros = build_targets(files, manifests)
    for path in sorted(scripts):
        flags["Build scripts"].append((f"{path}:1", "Review the code Cargo runs before compiling the mod."))
    for path in sorted(macros):
        flags["Proc-macro crates"].append((f"{path / 'Cargo.toml'}:1", "Review the code that runs during compilation."))
    locations = lock_locations(files[Path("Cargo.lock")], packages) if packages else {}
    if manifests and not failures:
        dependency_scan(source, packages, locations, work, failures, flags, note)
    return release_diff(files, packages, locations, manifests, scripts, macros, prior, flags)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--previous", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output = args.output.resolve()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    failures, flags, note, diff = [], defaultdict(list), [], []
    status = 0
    try:
        with tempfile.TemporaryDirectory(dir=args.output.parent) as scratch:
            diff = scan(args.source.absolute(), args.previous, Path(scratch), failures, flags, note)
        status = int(bool(failures))
    except (ScanError, OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        failures.append(("scan-incomplete", str(error)))
        status = 2
    sections = ["## Release scan", "### Hard failures"]
    sections += [f"- **{kind}**: {safe(message)}" for kind, message in sorted(set(failures))] or ["None."]
    sections += ["", "### Flags"]
    if not flags:
        sections.append("None.")
    for subject, findings in sorted(flags.items()):
        sections += ["", f"#### {safe(subject)}"]
        sections += [f"- {safe(location)}: {safe(message)}" for location, message in sorted(set(findings))]
    sections += ["", "### Release diff", *(diff or ["No diff available."]), "", *note]
    args.output.write_text("\n".join(sections) + "\n")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
