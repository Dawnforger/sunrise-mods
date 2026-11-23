"""Resolve workflow inputs and publish the scanner's Markdown, including on failure."""

import html
import os
from pathlib import Path
import re
import subprocess
import sys


def input_dir(path, artifact):
    if not path:
        return artifact
    root = Path.cwd()
    relative = Path(path)
    candidate = root / relative
    if relative.is_absolute() or relative == Path(".") or ".." in relative.parts or candidate != candidate.resolve():
        raise SystemExit("A path input must name a directory inside the trusted checkout, without symlinks.")
    return candidate


def check_fixture(case, code, report):
    if case == "flags":
        # Rule test positives and the live report must agree on exactly the ten rule subjects.
        rust = Path("scanner/tests/flags/src/lib.rs").read_text()
        for rule in re.findall(r"// ruleid: ([\w-]+)", rust):
            assert f"#### <code>{rule}</code>" in report, rule
        literal = html.unescape(report)
        for text in ("Build scripts changed: <code>build.input</code>", "Proc-macro crates changed: <code>macros</code>",
                     "<code>build.input:2</code>", "<code>special.entry:2</code>",
                     "<code>.internal/[link](www.example.invalid).rs:2</code>",
                     "Removed <code>.internal/retired.rs</code>", "Removed <code>removed.txt</code>",
                     "Changed <code>src/lib.rs</code>", "Changed dependency <code>crossbeam-channel</code>",
                     "RUSTSEC-2025-0024", "crossbeam-channel 0.5.14 is yanked."):
            assert text in literal, text
        # Encode the Markdown link delimiters and the bare www. autolink trigger.
        assert "&#91;link&#93;&#40;www&#46;example&#46;invalid&#41;" in report
        assert "[link](www.example.invalid)" not in report
    elif case == "clean":
        assert "### Flags\nNone." in report
    else:
        expected = {"git-dependency": "non-crates-io", "malicious-dependency": "rustsec-malicious",
                    "stale-lock": "lockfile-out-of-date"}[case]
        assert code == 1 and f"**{expected}**" in report, f"Expected {expected} (got exit {code})"
        if case == "malicious-dependency":
            assert "RUSTSEC-2025-0158" in report
        return
    assert code == 0, f"Expected a successful scan (got exit {code})"


def main():
    artifacts = Path(os.environ["RUNNER_TEMP"]) / "scan-input"
    output = Path("scan-output/summary.md").absolute()
    source = input_dir(os.environ["SOURCE_PATH"], artifacts / "source")
    previous = os.environ["PREVIOUS_PATH"]
    args = [sys.executable, "-I", "scanner/scan.py", str(source), "--output", str(output)]
    if previous or os.environ["HAS_PREVIOUS_ARTIFACT"] == "true":
        args += ["--previous", str(input_dir(previous, artifacts / "previous"))]
    code = subprocess.run(args).returncode
    report = output.read_text()
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as step_summary:
        step_summary.write(report)
    fixture = os.getenv("FIXTURE")
    if fixture:
        check_fixture(fixture, code, report)
        return 0
    return code


if __name__ == "__main__":
    raise SystemExit(main())
