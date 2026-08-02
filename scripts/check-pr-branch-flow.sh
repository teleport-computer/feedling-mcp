#!/usr/bin/env bash
set -euo pipefail

base_branch="${1:-}"
head_branch="${2:-}"

if [[ -z "$base_branch" || -z "$head_branch" ]]; then
  echo "::error title=Invalid PR branch flow::base and head branch names are required" >&2
  exit 2
fi

if [[ "$base_branch" == "main" && "$head_branch" != "test" && "$head_branch" != "pre" ]]; then
  echo "::error title=Invalid PR branch flow::main only accepts pull requests from test or pre; got '$head_branch'" >&2
  exit 1
fi

echo "Branch flow allowed: $head_branch -> $base_branch"
