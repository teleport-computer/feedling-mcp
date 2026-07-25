# Feedling — Reproducible Build Recipe

This document lets any third party recompute the exact container image
digest that a given git commit produces. It backs the "Reproducible build
recipe published" row in the iOS audit card and the `build_recipe_url`
field in the enclave attestation response.

Every git tag / release commit in the main branch comes with a
corresponding signed build attestation in GitHub Releases containing:

- The base image digest in use (from `deploy/Dockerfile`, `ARG PYTHON_IMAGE=python@sha256:…`)
- The expected output image digest (`sha256:…`) that this recipe should produce
- The production compose input for that commit,
  `deploy/docker-compose.phala.yaml`. In live-CVM mode the final
  `compose_hash` comes from dstack after deploy because dstack injects
  launch metadata; `deploy/publish-compose-hash.sh` reads that live
  value before publishing to AppAuth.

## Prerequisites

- `docker` ≥ 24
- `uv` ≥ 0.5 (https://docs.astral.sh/uv/)
- a POSIX shell

## Rebuild

```bash
# Clone the exact commit referenced in the on-chain AppAuth event
git clone https://github.com/teleport-computer/feedling-mcp.git
cd feedling-mcp
git checkout <git_commit_from_appauth>

# Confirm the pinned base image matches the one in Dockerfile
grep '^ARG PYTHON_IMAGE' deploy/Dockerfile

# Build the image — no cache, so transient layers don't hide drift
docker build --no-cache -f deploy/Dockerfile -t feedling-repro:local .

# Record the digest of the image you just built
docker image inspect feedling-repro:local --format '{{index .RepoDigests 0}}' \
  || docker image inspect feedling-repro:local --format '{{.Id}}'
```

Compare the printed digest against the expected digest published in the
GitHub Release for that commit. If they match, the image you hold is
byte-identical to the one referenced by the production compose that
produced the on-chain `compose_hash`.

## Refreshing pins (maintainers only)

### Base image digest

```bash
# Pull the latest python:3.12-slim
docker pull python:3.12-slim
docker inspect python:3.12-slim --format '{{index .RepoDigests 0}}'
# Copy the sha256:… portion into deploy/Dockerfile ARG PYTHON_IMAGE.
```

### Python dependency lockfile

```bash
# From repo root, regenerate requirements.lock with hashes
uv pip compile backend/requirements.txt \
    --generate-hashes \
    --python-version 3.12 \
    -o backend/requirements.lock

# The image installs a second lockfile too (deploy/Dockerfile) — regenerate
# it when backend/requirements-v2-worker.txt changes (command from the
# lockfile's own header; constrained by requirements.lock so shared deps
# stay on identical versions):
uv pip compile backend/requirements-v2-worker.txt \
    --generate-hashes \
    --python-version 3.12 \
    --python-platform linux \
    --constraint backend/requirements.lock \
    -o backend/requirements-v2-worker.lock

# Commit both requirements*.txt (source of truth for what we want) and
# requirements*.lock (exact versions + content hashes we ship with).
```

Any change to either pin invalidates the build digest, which invalidates
the compose_hash, which requires a new on-chain `addComposeHash()` +
user-visible audit-card prompt before iOS will talk to the new deployment.

## Known non-determinism

The remaining source of build non-determinism is the apt package set
installed in the base Dockerfile layer (`build-essential`, `libssl-dev`,
`libffi-dev`, `curl`). These are locked to the version available in the
base image's apt sources at build time. Because we pin the base image by
digest and set `--no-install-recommends`, this is deterministic *for that
base image* — but regenerating the base image (Debian sources shift daily)
would produce different apt versions.

This is an open gap, not a scheduled item. Future tightening options:

- Pin apt packages by exact version (`package=1.2.3-1`)
- Switch to a fully-static Python distribution (e.g. distroless Python)
- Use Nix / Bazel for a fully deterministic graph

## Reproducibility verification scripts

Two companion scripts live next to this file and mirror the pattern from
`dstack-tutorial/02-bitrot-and-reproducibility`:

### `deploy/build-reproducible.sh`

Runs two back-to-back `docker buildx build` passes with
`SOURCE_DATE_EPOCH=0` and `rewrite-timestamp=true`, outputs an OCI
tarball, and fails if the two tarball sha256s don't match. On success
writes `deploy/build-manifest.json`:

```json
{
  "image_hash":   "<sha256 of the OCI tarball>",
  "image_digest": "<sha256 of the OCI manifest inside the tarball>",
  "build_date":   "<UTC ISO-8601>",
  "source_date_epoch": 0
}
```

Commit `build-manifest.json` alongside a deploy so auditors can see what
you expected the build to produce.

### `deploy/verify-remote.sh`

```bash
./deploy/verify-remote.sh user@auditor-machine
```

Tarballs `deploy/Dockerfile` + `backend/`, ships them to a remote host,
rebuilds there with the same flags, and compares the tarball sha256
against `deploy/build-manifest.json`. A match proves the build is
deterministic across machines. Any mismatch points at Docker Buildx
version drift or apt package drift on the base image — see "Known
non-determinism" above.

Third-party auditors run `verify-remote.sh` against their own host
without needing write access to anything here — the only authority they
grant is a signed-in build environment of their choosing.
