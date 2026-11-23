"""Resolve workflow inputs and publish the scanner's Markdown, including on failure."""

import html
import os
from pathlib import Path
import re
import subprocess
import sys

# GitHub drops summaries larger than 1 MiB without failing the step:
# https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-commands#step-isolation-and-limits
STEP_SUMMARY_LIMIT = 1 << 20
TRUNCATION_NOTICE = b"\nThis summary was truncated. See the full `scan-summary` artifact (`summary.md`).\n"


def step_summary_preview(report: bytes) -> bytes:
    if len(report) < STEP_SUMMARY_LIMIT:
        return report
    # The report puts hard failures and flags before the diff. Keep whole Markdown lines.
    newline = report.rfind(b"\n", 0, STEP_SUMMARY_LIMIT - len(TRUNCATION_NOTICE) - 1)
    return report[:newline + 1] + TRUNCATION_NOTICE


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
        # Rule test positives and the live report must agree on the rule subjects.
        rust = Path("scanner/tests/flags/src/lib.rs").read_text()
        for rule in re.findall(r"// ruleid: ([\w-]+)", rust):
            assert f"#### <code>{rule}</code>" in report, rule
        literal = html.unescape(report)
        for text in ("Build scripts changed: <code>build.input</code>, <code>retired.input</code>",
                     "Proc-macro crates changed: <code>macros</code>",
                     "<code>build.input:2</code>", "<code>special.entry:2</code>",
                     "<code>.internal/[link](www.example.invalid).rs:2</code>",
                     "Removed <code>.internal/retired.rs</code>", "Removed <code>removed.txt</code>",
                     "Added <code>src/source.input</code>", "Changed <code>src/lib.rs</code>",
                     "Changed dependency <code>crossbeam-channel</code>",
                     "RUSTSEC-2025-0024", "crossbeam-channel 0.5.14 is yanked."):
            assert text in literal, text
        assert "<code>src/source.input:2</code>" not in literal
        for script in ("build.input", "retired.input"):
            for verb in ("Added", "Removed", "Changed"):
                assert f"- {verb} <code>{script}</code>" not in literal
        # Encode the Markdown link delimiters and the bare www. autolink trigger.
        assert "&#91;link&#93;&#40;www&#46;example&#46;invalid&#41;" in report
        assert "[link](www.example.invalid)" not in report
    elif case == "clean":
        assert "### Flags\nNone." in report
    elif case == "native-template":
        assert "### Hard failures\nNone." in report
        assert "### Scan incomplete" not in report
    elif case == "stale-lock":
        assert code == 2, f"Expected an incomplete scan (got exit {code})"
        assert "### Hard failures\nNone.\n\n### Scan incomplete\n- **scan-incomplete**:" in report
        assert "Cargo&#46;lock does not match the manifests" in report
        return
    elif case == "missing-target":
        assert code == 2, f"Expected an incomplete scan (got exit {code})"
        assert "### Scan incomplete\n- **scan-incomplete**:" in report
        literal = html.unescape(report)
        for target in ("src/absent.rs", "outside.rs"):
            assert target in literal, target
        return
    else:
        expected = {"git-dependency": "non-crates-io", "malicious-dependency": "rustsec-malicious"}[case]
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
    report = output.read_bytes()
    Path(os.environ["GITHUB_STEP_SUMMARY"]).write_bytes(step_summary_preview(report))
    fixture = os.getenv("FIXTURE")
    if fixture:
        check_fixture(fixture, code, report.decode("utf-8"))
        return 0
    return code


if __name__ == "__main__":
    raise SystemExit(main())
