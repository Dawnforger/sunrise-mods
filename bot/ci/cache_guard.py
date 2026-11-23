"""Reject cache restores in release-check's author-facing jobs and their local callees."""
import sys
from pathlib import Path

import yaml

# These exact versions have no implicit cache restore. A new external action needs
# inspection: not every setup action needs an explicit cache option to restore one.
AUDITED_ACTIONS = {
    "actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683",
    "actions/checkout@11d5960a326750d5838078e36cf38b85af677262",
    "actions/download-artifact@9000827ccba6bdab643e8b6fd33ac0654aef8333",
    "actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9",
}


def guard(root):
    active = set()

    def load(path):
        if path in active:
            raise ValueError(f"recursive author job dependency: {path}")
        active.add(path)
        return yaml.safe_load((root / path).read_text(encoding="utf-8"))

    def steps(path, rows):
        for step in rows:
            uses = step.get("uses", "").lower()
            action = uses.partition("@")[0]
            if action in ("actions/cache", "actions/cache/restore") or any(
                "cache" in key.lower() for key in (step.get("with") or {})
            ):
                raise ValueError(f"{path}: author-facing job contains a cache step or option: {uses}")
            if uses.startswith("./"):
                directory = step["uses"]
                target = next((f"{directory}/action.{ext}" for ext in ("yml", "yaml")
                               if (root / f"{directory}/action.{ext}").is_file()), None)
                if target is None:
                    raise ValueError(f"{path}: cannot inspect local action {directory}")
                runs = load(target)["runs"]
                if runs["using"] != "composite":
                    raise ValueError(f"{target}: author-facing local action is not a composite")
                steps(target, runs["steps"])
                active.remove(target)
            elif uses and uses not in AUDITED_ACTIONS:
                raise ValueError(f"{path}: unaudited external action in author-facing job: {uses}")

    def workflow(path, job_names=None):
        data = load(path)
        jobs = data["jobs"]
        for name in job_names if job_names is not None else jobs:
            job = jobs[name]
            if "uses" in job:
                target = job["uses"]
                if not target.startswith("./.github/workflows/"):
                    raise ValueError(f"{path}: cannot inspect external reusable workflow {target}")
                workflow(target)
            else:
                if job.get("cache-mode", data.get("cache-mode")) != "none":
                    raise ValueError(f"{path}: author-facing job {name} must set cache-mode: none")
                steps(path, job["steps"])
        active.remove(path)

    workflow(".github/workflows/release-check.yml", ("build", "scanner"))


if __name__ == "__main__":
    try:
        guard(Path(__file__).resolve().parents[2])
    except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError) as error:
        sys.exit(str(error))
