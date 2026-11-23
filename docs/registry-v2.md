# Sunrise mod registry v2

**V2 is built and checked before it goes live. Publishing starts when the registry signing key is configured. The current registry and its published files are unchanged.**

V2 uses `registry/` for its source files. The launcher and site will read `v2/index.json` and
`v2/index.json.minisig` on the existing registry site, not the TOML files. The author tool reads
`watched.toml`, `categories.toml`, and `release-workflow.toml` from raw `main`.

## Repository layout

All TOML tables reject unknown keys. Text fields described as dates use quoted `YYYY-MM-DD` strings.
`project` and project `id` values are eight case-sensitive base62 characters (`0–9`, `A–Z`, `a–z`).
The registration service assigns each project ID once and never reuses it. `mod` is the in-game ID
from `sunrise-mod.toml`. It follows Sunrise's mod-ID rules, including the length and reserved
Windows device names.

Paths in the layout use forward slashes. They may not be absolute, contain backslashes or drive
prefixes, or contain empty, `.` or `..` segments. A package path may be exactly `.` for the root
of the author's repository. The checker does not follow symlinks in the registry tree.

### `registry/watched.toml`

The registration service records a `[[repository]]` row with the positive numeric GitHub `id`
and current `name` (`owner/repo`). The watcher refreshes `name` on a rename; `id` is the identity.
Each `[[repository.package]]` row gives the `path` of the folder holding `sunrise-mod.toml` and its
permanent `project` ID. Repository IDs, paths within a repository, and project IDs are unique.
Every package has exactly one `project.toml` and vice versa. An empty registry has `repository = []`.

### `registry/release-workflow.toml`

Maintainers set `uses` to `sunrise-org/sunrise-mods/.github/workflows/release.yml@release-v1`.
The file has only this key. The suffix is a `release-v<number>` tag, not a moving branch. The
author tool copies `uses` into the caller workflow. Change the pointer to a new tag when that
version of the shared workflow is available.

### `registry/categories.toml`

Maintainers own the list. Each `[[category]]` has a unique `id`, a Lucide `icon` name, and `kinds`,
the package kinds that may use it. The initial names come from Modrinth's mod categories. No
Modrinth artwork is used, and neither readers nor the checker need to query Modrinth.

### `registry/projects/<project>/project.toml`

The registration service writes this file when the project is registered.

| Field | Meaning |
| --- | --- |
| `id` | Permanent project ID. It matches the directory name. |
| `slug` | Unique site page address. It uses `a–z`, `0–9`, `-`, and `_` and may change in a reviewed PR. |
| `mod` | In-game ID. Only two subfolder packages in the same repository cannot share it, because both use it in release tags and caller workflow names. A root package has its own tag and caller names. |
| `kind` | `mod`, `native-mod`, `modpack`, `resourcepack`, `shader`, `shaderpack` or `datapack`. Pack-specific fields are not defined here. |
| `owners` | GitHub logins allowed to release. This is not the list of page credits. |
| `repository`, `path` | Numeric repository ID and package path, matching `watched.toml`. |
| `workflow` | Signer workflow path, equal to the part of `uses` before `@`. |
| `registered` | Registration date. |

### `registry/projects/<project>/releases/<version>.toml`

The watcher adds one file per approved release. A merged release file is not edited except by a
takedown. The checker checks a checkout, not the history of that file.

| Field | Meaning |
| --- | --- |
| `version` | Package semantic version. It matches the filename without `.toml`. |
| `tag` | Git tag. Use `v<version>` at the repository root and `<mod>/v<version>` in a subfolder. |
| `published` | Publication date. |
| `sunrise` | The stable tag (for example `26.3-0.1.0`) or exact Nightly/Dev build ID (`<channel>-<YYYY-MM-DD>-<commit>`) the package was made for. A rolling channel name is not a build ID. |
| `workflow` | Exact signer workflow ref recorded by the attestation: the project's workflow path followed by `@refs/tags/release-v<number>`. Existing releases keep their ref when `release-workflow.toml` selects a new tag. |
| `[file]` | The `.smod` asset. Native mods ship source and a lockfile, not a binary. |
| `[page]` | The separate page bundle. It is not part of the install or the published index. |
| `[[dependencies]]` | Optional list. Each entry names a `mod` slot and whether it is `required`. An optional `project` pins a specific registered provider of that in-game ID. |

Both asset tables give the HTTPS `url`, `size` in bytes, `sha256` (64 lowercase hexadecimal
characters), and HTTPS `attestation` URL. Both URLs use
`https://github.com/<owner>/<repo>/releases/download/<tag>/<asset>` with the release row's tag.
The named assets are `<mod>-<version>.smod` and `<mod>-<version>.page.tar`; each bundle adds
`.sigstore.json` to its asset's name. The owner/repo in a URL may reflect a repository's earlier
name. The layout checker does not download the assets, compare hashes, or verify that each
asset's GitHub artifact attestation (Sigstore) matches the registered repository and release ref.

### `registry/projects/<project>/page/`

The watcher copies the newest release's page assets here. It writes a resolved `page.toml` after
applying defaults from the package's `sunrise-mod.toml`. A project does not need a page before its
first release. After that, it needs all of the following:

- `name`, `summary`, `description`, `authors` (list), and `license` from the resolved page.
  The summary needs at least 25 Unicode scalar values and the description at least 125, after
  trimming surrounding whitespace. Markdown is not parsed or normalized for this count.
- `icon`, pointing to the mod's own icon copied from the package.
- At least one `[[screenshots]]` row with `path` and `caption`. The watcher writes an empty string
  if the author omitted a caption.

The page can also give `homepage`, `issues`, and `source` as HTTP(S) URLs. `categories` is an
optional list of 0–8 distinct category IDs that apply to the project's `kind`. There are no
free-form tags.

All image paths are relative to the resolved `page.toml`: use `gallery/recipe.png`, not
`page/gallery/recipe.png`. Each image must exist inside this folder. Screenshots are checked for
a PNG or JPEG signature, a matching `.png`, `.jpg` or `.jpeg` extension, and the 5 MiB gallery
limit. The icon follows the mod package's own PNG icon rules and must have a `.png` extension.
Apart from `page.toml`, files not named in the page are rejected. The publisher serves only the
validated images.

### `registry/fit/<project>.toml`

CI generates `registry/fit/` at publish time; it is never committed. The checkout checker rejects
it outside the test fixtures. The directory contains `builds.json` and one TOML file per project
with releases. Each `[[release]]` row has the existing release `version`, `fits` (exact build IDs
known to fit), and optionally `does_not_fit` (exact build IDs known not to fit). A missing verdict
means the comparison could not be made and will not be reused on the next publish.

Offered builds are every published `26.3-<semver>` stable release and up to the ten newest builds
with a published SDK surface from each of Nightly and Dev. Releases identify Nightlies by their
exact build IDs rather than by the rolling GitHub tag. The fit process verifies the package's size
and SHA-256 and the SDK's signed metadata and surface. It uses Sunrise's shared surface library
for native API comparisons and does not build or run packages. A deferred comparison, an unavailable
comparison, or a cross-build comparison requiring game-code evidence is left unknown. Definitive
verdicts are reused by package SHA-256 and exact build ID and commit, only from the previously
published signed index.

### `registry/revoked.toml`

Maintainers record `[[revoked]]` rows with `project`, `reason`, `date`, and optional `sha256`. Without
`sha256`, the entire project is taken down; otherwise only the file with that digest is taken down.
A given project/hash pair may appear only once. A row may outlive a listing or a release. The empty
list is `revoked = []`.

### Sources for the registry limits

In Modrinth `modrinth/code` at `7f6963c`, `apps/labrinth/fixtures/labrinth-seed-data-202508052143.sql:3–80`
contains the category names for project type `1` (`mod`, from
`apps/labrinth/migrations/20210509010206_project_types.sql:3–9,20–22`). The project ID length and base62
alphabet are from `apps/labrinth/src/database/models/ids.rs:29–30,236` and
`apps/labrinth/migrations/20240115052708_base62-helper-functions.sql:5`. In `apps/labrinth/src/validate/project/`,
`summary.rs:13`, `description.rs:17`, and `tags.rs:10` set the minimums and category cap. The screenshot
limit is `max_gallery_image_size` in `apps/labrinth/src/routes/internal/globals.rs:57`, shared by the
Sunrise page reader. The icon rules come from `sunrise_mod_package::icon::Icon`.

## Published index

The top-level object contains:

| Field | Meaning |
| --- | --- |
| `schema` | Integer `2`. |
| `serial` | Opaque unsigned 64-bit sequence number. A new index must exceed the live serial. Carrying the live index over keeps its exact bytes and serial. |
| `generated` | UTC RFC 3339 timestamp. Informational only; it does not expire the index. |
| `builds` | Array of offered `{ id, commit }` pairs. `commit` is the full SDK commit SHA. |
| `categories` | Array of `{ id, icon, kinds }` rows, as above. |
| `projects` | Projects with at least one release. |
| `revoked` | Takedown rows as above. A `null` `sha256` takes down the whole project. |

Each project carries `id`, `slug`, `mod`, `kind`, `owners`, and `workflow` from the project record.
Its `repository` object has `id`, `name`, and `path`. `name`, `summary`, `description`, `authors`,
`license`, `homepage`, `issues`, `source`, `categories`, `icon`, and `screenshots` come from the
resolved page. Missing links are `null`; absent categories are `[]`. Each screenshot is
`{ path, caption }`. Resolve image paths relative to the directory of `index.json`. They have the
form `pages/<project>/<sha256>.png` or `pages/<project>/<sha256>.jpg`: the digest is the SHA-256 of
the exact served bytes, in lowercase hexadecimal, and the extension is from the image format.
The author's original filename is not published.

Each project's `releases` array contains rows with `version`, `published`, `sunrise`, `workflow`,
`fits`, `file`, and `dependencies`. The release's `workflow` is an exact signer ref, not the project's
bare workflow path. `fits` is an array of exact build IDs; missing IDs do not fit for discovery.
The optional `does_not_fit` array lets the publisher distinguish a known negative verdict from an
unavailable comparison when reusing the index as its cache. Readers filter using `fits` only.
`file` contains
`url`, `size`, `sha256`, and `attestation`. Each dependency carries `mod`, `project` and `required`;
a `null` `project` means the dependency does not pin a provider. The index omits the Git tag and
page bundle. The launcher does not fetch the bundle to show the page or install the mod.

### Reader rules

1. Verify `index.json.minisig` with the registry public key over the **exact bytes** of `index.json`.
   Do not parse and reserialize those bytes for verification.
2. Accept only schema 2 at this address. Ignore unknown fields under schema 2. A breaking change
   raises `schema` and uses a new address.
3. Refuse a serial lower than the last accepted serial. Do not use `generated` for rollback checks.
4. Apply project and digest takedowns even if the listing or release is still present.
5. Use the release's exact fit list for the selected build. Do not infer compatibility from a version
   number or from the `sunrise` field alone.
6. Treat `id` as the project identity, `slug` as the page address, and `mod` as a slot. No array order
   chooses a provider. Enable at most one provider per slot in a profile and never switch it silently.
7. Check each file's hash and verify its attestation against the registered repository ID and
   that release's exact `workflow` ref. On install the launcher does this for the `.smod`. Remember
   the registered repository and workflow path per project; use the approved ref for each release.
   Verifying the catalogue is not a substitute for verifying the package.
8. Verify each icon and screenshot against the SHA-256 in its filename before displaying it.

## Local checks and example

- `registry-bot check <checkout>` validates a local tree, reports file and reason on each diagnostic
  line, and exits nonzero for an invalid tree.
- `registry-bot builds --out <file>` lists the offered game builds from GitHub. For authenticated
  API access, pass the usual GitHub CLI token in its environment.
- `registry-bot fit <checkout> --builds <file> [--cache <previous index.json>] --out <dir>` writes
  the generated fit directory. The optional cache must have its `.minisig` beside it, valid for
  `registry/index-key.pub` in the checkout.
- `registry-bot index <checkout> --serial <n> --out <dir>` validates the same tree and stages
  `index.json` and the exact referenced images under `pages/`. Use `--generated-fit` to consume CI
  fit output from `registry/fit/`; add `--after <previous index.json>` to verify the previous signed
  index and require a greater serial. The command uses the current UTC time for `generated` and
  does not sign or deploy anything.
- `registry-bot verify-index <index> <sig> --key <pub>` checks the exact bytes and the index schema.

The publish workflow uses the current UTC epoch second as the next serial. Signing and deployment
both require the current tip of `main`. Immediately before publishing, deployment compares the
candidate's signed serial with the verified live index and the snapshot used by the last successful
Pages deployment. A valid live response can lag that deployment, so it cannot replace a missing
or unverifiable snapshot from a keyed deployment. Assembly verifies the index and images and
checks that the Pages archive will include them. If the new catalogue fails, the workflow uses the
last complete signed snapshot, from Pages first or from the last successful deployment's retained
artifact. It deploys the legacy update and reports the v2 failure. If neither source can be
recovered, or the deployed serial cannot be authenticated, it reports `NO_AUTHENTICATED_V2_SNAPSHOT`
and leaves the entire site unchanged. Without a committed public key, the publish does not contact
the v2 services and deploys only the legacy site. A separate
scheduled workflow detects new offered builds and requests a publish on `main` without
committing generated files.

The [two-provider fixture](../bot/tests/fixtures/two-ports/registry/) and
[its golden index](../bot/tests/fixtures/two-ports/index.json) show two projects for a single in-game ID.

## The watcher and release PR checks

`watch.yml` polls every 30 minutes and can also be started manually on `main`. It looks up each
repository by its numeric GitHub ID. A rename opens a separate maintenance PR for `watched.toml`;
it does not change project IDs or rewrite old release URLs. At registration the watcher considers
only the newest publication, by GitHub's timestamp. If its signature cannot be determined, the
watcher reports that and does not fall back to an older release. After an approval, only releases
published later than the latest approved release are eligible. The row still records just the date.
The same publication order selects the previous approved package for the scanner. A project has
at most one open release PR, and an existing bot PR (including a closed, unmerged PR) makes that
version handled. The watcher does not recreate it. Drafts never count; signed prereleases can count.
Observation and publication failures appear in the watch summary.

The read-only job downloads the `.smod`, page tar and both bundles. It checks GitHub's sizes and
available hashes, then uses `gh attestation verify` to check each artifact against the release
workflow and the same tagged source commit, resolved once. It checks the **verified certificate**
for the registered numeric repository ID and exact `release-v<number>` workflow ref, and requires
both bundles to agree. Native packages must pin a commit that a signed Sunrise build names. When a
content-only package has no SDK pin, the watcher records the current signed Nightly's exact build,
not the moving alias. The checker resolves the same exact build when it checks the PR. The existing
registry package download cap is 64 MiB (`sunrise_mod_package::index::MAX_FILE`); page entries use
Sunrise's manifest and screenshot limits. The write job gets only generated registry files. It
opens `release/<project>/<version>` and dispatches `release-check.yml` on `main` with the PR number
and exact head SHA. The registry's own GitHub Actions token does this; authors do not need one.

The release check reads the PR's Git tree as data, without checking it out. Only a new release row
and that project's page are allowed to change. It takes all four download URLs from the live release
of the registered repository, verifies the downloaded hashes and attestations, and requires the
row and page to match what it regenerated. GitHub API digests may be absent; the local SHA-256
checks still apply. An old URL for a previously approved source is accepted after a rename only if
it still reaches the same asset; the stored SHA-256 must still match.

The scanner receives the unpacked source and the last approved package when there is one. For the
native template's API path dependency, the scanner fetches the signed SDK pinned by the mod into a
scratch copy before passing the lockfile to Cargo; it never builds the mod. Separate hosted jobs
build on Linux and compile-check on Apple-chip macOS and Windows x64. These jobs use the signed
`cargo-sunrise-mod` tool, and its signed `tool.json` must declare `release_contract >= 1`. Publish
such a tool before enabling the watcher. Jobs that handle the package have no write permissions,
secrets, persisted checkout token or Actions cache access.

The Linux job does not start the game. The published native `cargo sunrise-mod run` needs prepared
resources. `sunrise-prepare --server` can prepare the public dedicated-server resources, but a
dedicated server cannot load client mods. Sunrise's client smoke needs private prepared Minecraft
client resources, which are not available to these hosted jobs. There is no cackle report: the
mod tool generates its own Cargo work tree and installs its own rustc wrapper for the SDK-native
build; it does not expose a cackle entry point into that build.

The check sets `registry-v2/release-check` to pending on the checked **PR head**, not on the
workflow's `main` commit. A final job in the same run posts the current scanner summary and job
outcomes in one PR comment and updates that status. Checks for one PR are serialized, and only the
newest dispatch and attempt for the head may post. It passes only when every required job succeeds
and the trusted scanner's report verifies for that attempt. A skipped, cancelled, timed-out or
incomplete check is not green; if cancellation prevents the final job from starting, the status
stays pending. Use **Rerun all jobs**; do not rerun only failed jobs. Ordinary scanner flags guide
review without failing the release.

Enable **Allow GitHub Actions to create and approve pull requests** before turning on the watcher.
Require `registry-v2/release-check` on `main` for every PR. A no-checkout workflow reports success
with `not a release` for PRs that change no files under `registry/projects/*/releases/` or
`registry/projects/*/page/`. Only the dispatched check can pass a PR that changes those paths.
A maintainer must approve the ordinary `check` and `dco` workflow runs for PRs the bot opens with
`GITHUB_TOKEN`.
