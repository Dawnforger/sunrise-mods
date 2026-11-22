"""Native mods in `mods/` are for stable Sunrise releases only (README.md, "Listing your mod").

    python3 -I scripts/check_native_rows.py <base> <head>

Refuses a pull request that adds or changes a `native-mod` version row with no `sunrise`: a native mod for
Nightly and Dev is listed by its source in `sources/<id>.toml`. Rows already on main stay as they are. An
entry that doesn't parse is left to the registry tool, which reports it.
"""
import sys
import tomllib
from pathlib import Path


def entry(path: Path) -> dict:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return {}


def rows(data: dict) -> list:
    versions = data.get("versions")
    return versions if isinstance(versions, list) else []


def check(base: Path, head: Path) -> list[str]:
    errors = []
    for path in sorted((head / "mods").glob("*.toml")):
        new = entry(path)
        if new.get("kind") != "native-mod":
            continue
        published = list(rows(entry(base / "mods" / path.name)))
        for row in rows(new):
            if isinstance(row, dict) and "sunrise" in row:
                continue
            if row in published:
                published.remove(row)
                continue
            version = row.get("version") if isinstance(row, dict) else None
            errors.append(
                f"mods/{path.name} {version}: a native mod's version is listed in mods/ only for a stable Sunrise "
                f'release (sunrise = "<release tag>", signed). For Nightly and Dev, list its source in '
                f'sources/{path.stem}.toml instead (README, "Source mods")'
            )
    return errors


def main(args: list[str]) -> int:
    if len(args) != 2:
        sys.exit(__doc__)
    errors = check(Path(args[0]), Path(args[1]))
    for error in errors:
        print(f"error: {error}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
