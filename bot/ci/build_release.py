"""Build the verified source on Linux; compile-check it on the other SDK hosts."""
import base64
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

PLATFORMS = {
    ("linux", "x86_64"): "x86_64-unknown-linux-gnu",
    ("darwin", "arm64"): "aarch64-apple-darwin",
    ("win32", "amd64"): "x86_64-pc-windows-gnu",
}
# Public Ed25519 release key, as pinned by Sunrise's mod-tool install guide and release workflow.
KEY = "r53xb3cuafGxZSicLz06KC+xXp9NJdPH8snNmXo5pAc="


def install_tool(work):
    host = (sys.platform, platform.machine().lower())
    if host not in PLATFORMS:
        raise ValueError("runner is not a Sunrise SDK host")
    platform_asset = f"tool-{PLATFORMS[host]}.tar.xz"
    sha_file, sig_file, info = "SHA256SUMS", "SHA256SUMS.sig", "tool.json"
    for name in (platform_asset, sha_file, sig_file, info):
        subprocess.run(["curl", "--disable", "--proto", "=https", "--proto-redir", "=https", "-fsSL", "-o", work / name,
                        f"https://github.com/sunrise-org/sunrise/releases/download/mod-tool/{name}"], check=True)
    if sys.platform == "win32":
        openssl = Path(os.environ["ProgramFiles"]) / "Git/usr/bin/openssl.exe"
    elif sys.platform == "darwin":
        prefix = subprocess.check_output(["brew", "--prefix", "openssl@3"], text=True).strip()
        openssl = Path(prefix) / "bin/openssl"
    else:
        openssl = "openssl"
    sums = (work / sha_file).read_bytes()
    message, key, signature = (work / name for name in ("message", "key.der", "signature"))
    message.write_bytes(b"horizon release mod-tool\n" + sums)
    key.write_bytes(bytes.fromhex("302a300506032b6570032100") + base64.b64decode(KEY))
    for row in (work / sig_file).read_bytes().splitlines():
        if not row:
            continue
        signature.write_bytes(base64.b64decode(row, validate=True))
        verified = subprocess.run([openssl, "pkeyutl", "-verify", "-rawin", "-pubin", "-keyform", "DER", "-inkey", key,
                                   "-in", message, "-sigfile", signature], capture_output=True)
        if verified.returncode == 0:
            break
    else:
        raise ValueError("mod-tool checksums are not signed by Sunrise")
    for name in (platform_asset, info):
        matches = re.findall(rf"^([0-9a-f]{{64}}) [ *]{re.escape(name)}$", sums.decode(), re.M)
        with (work / name).open("rb") as stream:
            if matches != [hashlib.file_digest(stream, "sha256").hexdigest()]:
                raise ValueError(f"{name} differs from the signed checksums")
    meta = json.loads((work / info).read_bytes())
    if meta["schema"] != 1 or meta["tag"] != "mod-tool" or not re.fullmatch(r"[0-9a-f]{40}", meta["commit"]):
        raise ValueError("invalid signed mod tool descriptor")
    executable = "cargo-sunrise-mod.exe" if sys.platform == "win32" else "cargo-sunrise-mod"
    binary = work / executable
    with tarfile.open(work / platform_asset, "r:xz") as archive:
        members = [entry for entry in archive if entry.name.removeprefix("./") == executable]
        if len(members) != 1 or not members[0].isfile():
            raise ValueError(f"expected one regular {executable} in the signed tool archive")
        with archive.extractfile(members[0]) as source, binary.open("xb") as target:
            shutil.copyfileobj(source, target)
    binary.chmod(0o755)
    version = subprocess.check_output([binary, "--version"], cwd=work, text=True).strip()
    if version != f'cargo-sunrise-mod commit {meta["commit"]} native-contract {meta["native_contract"]}':
        raise ValueError("tool does not match the signed descriptor")
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as summary:
        print(f'Signed Sunrise mod tool: `{meta["commit"]}`.', file=summary)
    return binary


def run(source, operation):
    # Compilation may execute build scripts or proc macros. This is the final step of a
    # disposable hosted runner with only contents:read, no persisted credentials or secrets.
    # The child gets no workflow file-command paths or tokens, nor a repository checkout as cwd.
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("GITHUB_", "ACTIONS_", "RUNNER_", "GH_"))}
    with tempfile.TemporaryDirectory() as folder:
        tool = install_tool(Path(folder))
        with subprocess.Popen([tool, "sunrise-mod", operation], cwd=source, env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as process:
            for line in process.stdout:
                line = line.rstrip(b"\r\n").decode("utf-8", errors="backslashreplace")
                line = "".join(c if c.isprintable() else f"\\u{ord(c):04x}" for c in line)
                print("mod build | " + line.replace("::", "%3A%3A").replace("##[", "%23%23%5B"), flush=True)
            if process.wait():
                raise ValueError(f"cargo sunrise-mod {operation} failed")


if __name__ == "__main__":
    try:
        source, operation = sys.argv[1:]
        if operation not in ("build", "check"):
            raise ValueError("expected build or check")
        run(Path(source), operation)
    except (ValueError, OSError, KeyError, subprocess.CalledProcessError) as error:
        sys.exit("release build: " + str(error).encode("unicode_escape").decode("ascii").replace("::", "%3A%3A").replace("##[", "%23%23%5B"))
