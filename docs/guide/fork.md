# How to fork semula and publish your own

Fork the repository and you get the same release machinery we use: a tagged push builds the image, the CLI binaries and the docs site. This page lists what to change so they publish under your name, and what to turn on in GitHub first.

## Rename it

The upstream names appear in the docs, the deploy files, the Helm chart, `pyproject.toml`, `zensical.toml` and one hint in the CLI. One script rewrites them:

```bash
python3 scripts/fork.py acme/semula         # shows the files it would change
python3 scripts/fork.py acme/semula --apply # rewrites them
git diff
```

It swaps the image registry (`ghcr.io/acme/semula`, always lowercase), the docs site (`acme.github.io/semula`), and the repository links. It leaves history alone (the changelog, the decision records, the lockfile) and every other project's name. Running it twice changes nothing more. It needs only Python 3.

It does not touch the author line in `pyproject.toml`; set that yourself.

## Turn things on in GitHub

1. **Actions.** Enable workflows on the fork (GitHub pauses them on a new fork).
2. **Packages.** The release workflow pushes with the built-in `GITHUB_TOKEN`, so it needs no secret. After the first release, open the package on your profile and make it public, or people cannot pull it without logging in.
3. **Pages.** Settings > Pages > Source: GitHub Actions. The docs workflow publishes the site.
4. **Deploying is optional.** The release workflow's last job deploys to your own server (`deploy/droplet`), and it runs only when the repository variable `DEPLOY_DOMAIN` is set. Leave that unset and a release publishes the image, the binaries and the notes, and stops. To deploy, set the variable and the `DEPLOY_HOST`, `DEPLOY_HOST_KEY` and `DEPLOY_SSH_KEY` secrets in a `production` environment with a required reviewer, as [`deploy/droplet/README.md`](https://github.com/leejianrong/semula/blob/main/deploy/droplet/README.md) describes.

## Cut a release

[`docs/RELEASING.md`](https://github.com/leejianrong/semula/blob/main/docs/RELEASING.md) is the full checklist. In short: set `version` in `pyproject.toml`, run `uv lock`, set `appVersion` in `deploy/helm/semula/Chart.yaml` to match, date the changelog section, merge, and push the tag. `make release-check TAG=vX.Y.Z` tells you whether the pieces agree before you push.

Each release publishes `ghcr.io/<you>/semula:<version>` and `<version>-hosted` for amd64 and arm64, and the CLI binaries with checksums. The image name comes from the repository, so nothing else needs editing.

## Run your build

Point the self-host files at your image:

```bash
# Docker Compose (deploy/selfhost/.env)
SEMULA_REPO=ghcr.io/acme/semula
SEMULA_VERSION=0.6.0
```

```bash
# Helm
helm install semula deploy/helm/semula -n semula --create-namespace \
  --set image.repository=ghcr.io/acme/semula --set image.tag=0.6.0
```

After `scripts/fork.py --apply` those are already the defaults. The [self-hosting guide](self-host.md) takes it from there.

## Stay in sync with upstream

```bash
git remote add upstream https://github.com/leejianrong/semula
git fetch upstream && git merge upstream/main
python3 scripts/fork.py acme/semula --apply   # new upstream text brings the upstream names back
```

Run the script again after each merge. It is safe to repeat, and it keeps the rename down to one commit you can look at.
