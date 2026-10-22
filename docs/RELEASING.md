# Releasing semula

A release is a tag. Pushing `vX.Y.Z` to a commit that already passed CI on `main` creates a GitHub
Release from the CHANGELOG and publishes the container image to
`ghcr.io/leejianrong/semula`. Nothing is built by hand.

## Cut a release

1. **Release PR.** Off fresh `main`:
   - set `version` in `pyproject.toml`;
   - run `uv lock` (the lockfile records the version, and CI installs it frozen);
   - in `CHANGELOG.md`, rename `## [Unreleased]` to `## [X.Y.Z] - YYYY-MM-DD` and add a new empty
     `## [Unreleased]` above it;
   - `uv sync --extra dev && make check`, then open the PR and merge it when CI is green.
2. **Check it** without publishing: *Actions → Release → Run workflow* (a dry run) builds the
   image for both architectures, runs the smoke test and publishes nothing.
3. **Tag the merge commit** and push the tag:
   ```bash
   git checkout main && git pull
   git tag -a vX.Y.Z -m "semula X.Y.Z" && git push origin vX.Y.Z
   ```
4. **Watch the Release run.** It fails, and publishes nothing, unless:
   - the tag is `vMAJOR.MINOR.PATCH` and the commit is on `main`;
   - that commit has a green CI run;
   - the tag, `pyproject.toml`, `uv.lock` and the CHANGELOG all say the same version
     (`scripts/release.py check vX.Y.Z` runs the same test locally).
5. **After it:** pull the image on a clean machine and run it
   (`docker run --rm -p 127.0.0.1:8080:8080 ghcr.io/leejianrong/semula:X.Y.Z`).

## What the run publishes

- A GitHub Release named `semula X.Y.Z` with the CHANGELOG section as its notes. A `0.x` release is
  marked pre-release.
- The image `ghcr.io/leejianrong/semula` for `linux/amd64` and `linux/arm64`, tagged `X.Y.Z`,
  `X.Y` and `latest` (and `X` from 1.0), with build provenance. It is smoke-tested after the push:
  started, `/health` and `/ui` answered.

## If it goes wrong

- **A check failed before anything was published:** fix it in a PR, delete the tag
  (`git push origin :refs/tags/vX.Y.Z` and `git tag -d vX.Y.Z`), and tag the new merge commit.
- **The release was created but the image failed:** re-run the failed job; the image job is safe to
  repeat. Do not move a published tag.
- **A bad release is out:** publish a fixed patch version. Mark the bad GitHub Release as a draft
  or add a note; do not delete image tags people may have pulled.

## Visibility

The package is linked to this repository through the `org.opencontainers.image.source` label. If
the first image shows as private on the package page, set it to public once (package settings →
Danger zone → Change visibility); later pushes keep it.
