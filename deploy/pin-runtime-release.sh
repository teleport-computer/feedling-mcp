#!/usr/bin/env bash
set -euo pipefail

# Pin the backend and Runtime V2 runner images as one release-source commit.
# The push is compare-and-swap: a newer branch head makes this run stop before
# any CVM mutation. Runner jobs consume the exact emitted commit and never read
# a moving branch head.

branch="${1:?branch is required}"
main_compose="${2:?main compose path is required}"
runner_compose="${3:?runner compose path is required}"
trigger_sha="${GITHUB_SHA:?GITHUB_SHA is required}"
owner="${GITHUB_REPOSITORY_OWNER:?GITHUB_REPOSITORY_OWNER is required}"

if [ "$(git rev-parse HEAD)" != "$trigger_sha" ]; then
  echo "::error::release checkout is not the triggering commit $trigger_sha"
  exit 1
fi
for path in "$main_compose" "$runner_compose"; do
  if [ ! -f "$path" ]; then
    echo "::error::$path is missing from triggering commit $trigger_sha"
    exit 1
  fi
done

git fetch origin "$branch"
remote_sha=$(git rev-parse "origin/$branch")
if [ "$remote_sha" != "$trigger_sha" ]; then
  # A retry of this same workflow may find the exact pin commit created by
  # its first attempt. Accept only that direct child, with the exact subject
  # and the two expected compose files as its complete diff. Any other branch
  # movement belongs to a newer release and must fail closed.
  expected_subject="deploy(${branch}): pin Runtime V2 release :${trigger_sha:0:7} [skip ci]"
  remote_parent=$(git rev-parse "${remote_sha}^")
  remote_subject=$(git log -1 --format=%s "$remote_sha")
  remote_author=$(git log -1 --format='%an <%ae>' "$remote_sha")
  remote_committer=$(git log -1 --format='%cn <%ce>' "$remote_sha")
  changed_paths=$(git diff --name-only "$trigger_sha" "$remote_sha")
  actual_paths=$(printf '%s\n' "$changed_paths" | sed '/^$/d' | LC_ALL=C sort)
  unexpected_paths=$(printf '%s\n' "$actual_paths" \
    | grep -Fvx -e "$main_compose" -e "$runner_compose" || true)
  if [ "$remote_parent" != "$trigger_sha" ] \
    || [ "$remote_subject" != "$expected_subject" ] \
    || [ "$remote_author" != 'feedling-ci <ci@feedling.app>' ] \
    || [ "$remote_committer" != 'feedling-ci <ci@feedling.app>' ] \
    || [ -z "$actual_paths" ] \
    || [ -n "$unexpected_paths" ]; then
    echo "::error::$branch advanced from trigger $trigger_sha to $remote_sha; the newer workflow owns deployment"
    exit 1
  fi

  short_sha="${trigger_sha:0:7}"
  image_owner=$(printf '%s' "$owner" | tr '[:upper:]' '[:lower:]')
  expected_main=$(git show "$trigger_sha:$main_compose" | sed -E \
    "s|ghcr\\.io/[^/]+/feedling:[a-f0-9]+|ghcr.io/${image_owner}/feedling:${short_sha}|g")
  expected_runner=$(git show "$trigger_sha:$runner_compose" | sed -E \
    "s|ghcr\\.io/[^/]+/feedling-agent-runner:[a-f0-9]+|ghcr.io/${image_owner}/feedling-agent-runner:${short_sha}|g")
  actual_main=$(git show "$remote_sha:$main_compose")
  actual_runner=$(git show "$remote_sha:$runner_compose")
  if [ "$actual_main" != "$expected_main" ] || [ "$actual_runner" != "$expected_runner" ]; then
    echo "::error::$remote_sha resembles this release pin but contains an unexpected compose change"
    exit 1
  fi
  git switch --detach "$remote_sha"
  echo "reusing release pin $remote_sha from an earlier attempt"
  echo "pinned_sha=$remote_sha"
  if [ -n "${GITHUB_OUTPUT:-}" ]; then
    echo "pinned_sha=$remote_sha" >> "$GITHUB_OUTPUT"
  fi
  exit 0
fi

short_sha="${trigger_sha:0:7}"
image_owner=$(printf '%s' "$owner" | tr '[:upper:]' '[:lower:]')
main_tmp=$(mktemp "${TMPDIR:-/tmp}/feedling-main-compose.XXXXXX")
runner_tmp=$(mktemp "${TMPDIR:-/tmp}/feedling-runner-compose.XXXXXX")
trap 'rm -f "$main_tmp" "$runner_tmp"' EXIT
sed -E \
  "s|ghcr\\.io/[^/]+/feedling:[a-f0-9]+|ghcr.io/${image_owner}/feedling:${short_sha}|g" \
  "$main_compose" > "$main_tmp"
sed -E \
  "s|ghcr\\.io/[^/]+/feedling-agent-runner:[a-f0-9]+|ghcr.io/${image_owner}/feedling-agent-runner:${short_sha}|g" \
  "$runner_compose" > "$runner_tmp"
# Copy over the existing files rather than moving the temp files so compose
# ownership and permissions remain unchanged on both GNU and BSD userlands.
cp "$main_tmp" "$main_compose"
cp "$runner_tmp" "$runner_compose"

if ! grep -Eq "ghcr\\.io/${image_owner}/feedling:${short_sha}([[:space:]]|$)" "$main_compose"; then
  echo "::error::failed to pin backend image in $main_compose"
  exit 1
fi
if ! grep -Eq "ghcr\\.io/${image_owner}/feedling-agent-runner:${short_sha}([[:space:]]|$)" "$runner_compose"; then
  echo "::error::failed to pin runner image in $runner_compose"
  exit 1
fi

if git diff --quiet -- "$main_compose" "$runner_compose"; then
  echo "release images already pinned to :$short_sha"
else
  git config user.name 'feedling-ci'
  git config user.email 'ci@feedling.app'
  git add "$main_compose" "$runner_compose"
  git commit -m "deploy(${branch}): pin Runtime V2 release :$short_sha [skip ci]"
  # Non-force push is the second half of the CAS. If the branch advanced after
  # the fetch/check above, this fails and no job deploys a mixed source tree.
  git push origin "HEAD:refs/heads/$branch"
fi

pinned_sha=$(git rev-parse HEAD)
echo "pinned_sha=$pinned_sha"
if [ -n "${GITHUB_OUTPUT:-}" ]; then
  echo "pinned_sha=$pinned_sha" >> "$GITHUB_OUTPUT"
fi
