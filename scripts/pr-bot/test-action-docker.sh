#!/usr/bin/env bash
# Build and run the Docker end-to-end test for the tld PR bot.
#
# Usage:
#   scripts/pr-bot/test-action-docker.sh
#   INDEXERS="go typescript" TLD_VERSION=v2.4.2 scripts/pr-bot/test-action-docker.sh
#
# Requires a running Docker daemon. The first run downloads the toolchains into
# the image; later runs reuse the layer cache. The image targets linux/amd64
# (matching GitHub's runners and the published scip-clang/scip-ruby assets);
# override PLATFORM to change that.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/../.." && pwd)"
image="${IMAGE:-tld-pr-bot-e2e}"
platform="${PLATFORM:-linux/amd64}"
indexers="${INDEXERS:-go typescript python dotnet clang java dart php ruby rust}"

docker build --platform "$platform" -f "$here/docker/Dockerfile" -t "$image" "$root"

run_args=(--rm --platform "$platform" -e "INDEXERS=$indexers")
if [[ -n "${TLD_VERSION:-}" ]]; then
  run_args+=(-e "TLD_VERSION=$TLD_VERSION")
fi
if [[ -n "${GITHUB_TOKEN:-}" ]]; then
  run_args+=(-e "GITHUB_TOKEN=$GITHUB_TOKEN")
fi
docker run "${run_args[@]}" "$image"
