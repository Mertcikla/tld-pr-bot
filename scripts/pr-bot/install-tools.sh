#!/usr/bin/env bash
# Download the tld release binary and install the requested SCIP indexers into a
# cache directory. Shared by action.yml and the Docker end-to-end test so the
# install logic is exercised exactly as it runs in CI.
#
# Required env: TLD_BINARY_DIR, TLD_VERSION, INDEXERS.
# Optional env: RUNNER_OS, RUNNER_ARCH, GITHUB_PATH (appended when set),
#               PUB_CACHE, COMPOSER_HOME, GOWORK, CGO_ENABLED.
set -euo pipefail

: "${TLD_BINARY_DIR:?TLD_BINARY_DIR is required}"
: "${TLD_VERSION:?TLD_VERSION is required}"
: "${INDEXERS:?INDEXERS is required}"

RUNNER_OS="${RUNNER_OS:-$(uname -s)}"
case "$(uname -m)" in
  x86_64 | amd64) default_arch=X64 ;;
  arm64 | aarch64) default_arch=ARM64 ;;
  *) default_arch="$(uname -m)" ;;
esac
RUNNER_ARCH="${RUNNER_ARCH:-$default_arch}"

if [[ "$RUNNER_OS" != Linux ]]; then
  echo 'The tld PR action requires an Ubuntu runner.'
  exit 1
fi
# shellcheck disable=SC1091
source /etc/os-release
if [[ "$ID" != ubuntu ]]; then
  echo 'The tld PR action requires an Ubuntu runner.'
  exit 1
fi

mkdir -p "$TLD_BINARY_DIR"
if [[ ! -x "$TLD_BINARY_DIR/tld" ]]; then
  case "$RUNNER_OS-$RUNNER_ARCH" in
    Linux-X64) asset=tld_Linux_x86_64.tar.gz ;;
    Linux-ARM64) asset=tld_Linux_arm64.tar.gz ;;
    *) echo "Unsupported runner: $RUNNER_OS-$RUNNER_ARCH"; exit 1 ;;
  esac
  download_dir=$(mktemp -d)
  trap 'rm -rf "$download_dir"' EXIT
  curl --fail --silent --show-error --location --retry 3 \
    "https://github.com/Mertcikla/tld/releases/download/$TLD_VERSION/$asset" \
    --output "$download_dir/archive.tar.gz"
  tar -xzf "$download_dir/archive.tar.gz" -C "$download_dir" tld
  chmod +x "$download_dir/tld"
  mv "$download_dir/tld" "$TLD_BINARY_DIR/tld"
fi
"$TLD_BINARY_DIR/tld" --version

# Indexers install into the cached binary dir. A failed install is a warning,
# not an error: tld reports any tool a project needs but cannot find, and the
# diagram is omitted only for that project.
npm_prefix="$TLD_BINARY_DIR/scip-npm"
dotnet_dir="$TLD_BINARY_DIR/scip-dotnet"
coursier_dir="$TLD_BINARY_DIR/scip-coursier"
pub_bin="${PUB_CACHE:-$TLD_BINARY_DIR/scip-pub}/bin"
php_bin="${COMPOSER_HOME:-$TLD_BINARY_DIR/scip-php}/vendor/bin"
rust_bin="${HOME:-/root}/.cargo/bin"
export PATH="$TLD_BINARY_DIR:$npm_prefix/bin:$dotnet_dir:$coursier_dir:$pub_bin:$php_bin:$rust_bin:$PATH"

install_indexer() {
  case "$1" in
    go)
      if [[ ! -x "$TLD_BINARY_DIR/scip-go" ]]; then
        GOBIN="$TLD_BINARY_DIR" go install github.com/scip-code/scip-go/cmd/scip-go@v0.2.7
      fi
      ;;
    typescript)
      if [[ ! -x "$npm_prefix/bin/scip-typescript" ]]; then
        npm install --global --prefix "$npm_prefix" @sourcegraph/scip-typescript@0.4.0
      fi
      ;;
    python)
      if [[ ! -x "$npm_prefix/bin/scip-python" ]]; then
        npm install --global --prefix "$npm_prefix" @sourcegraph/scip-python
      fi
      ;;
    dotnet)
      if [[ ! -x "$dotnet_dir/scip-dotnet" ]]; then
        dotnet tool install --tool-path "$dotnet_dir" scip-dotnet
      fi
      ;;
    clang)
      if [[ ! -x "$TLD_BINARY_DIR/scip-clang" ]]; then
        case "$RUNNER_ARCH" in
          X64) asset=scip-clang-x86_64-linux ;;
          ARM64) asset=scip-clang-arm64-linux ;;
          *) echo "Unsupported arch for scip-clang: $RUNNER_ARCH"; return 1 ;;
        esac
        curl --fail --silent --show-error --location --retry 3 \
          "https://github.com/sourcegraph/scip-clang/releases/download/v0.4.0/$asset" \
          --output "$TLD_BINARY_DIR/scip-clang"
        chmod +x "$TLD_BINARY_DIR/scip-clang"
      fi
      ;;
    java)
      if [[ ! -x "$coursier_dir/scip-java" ]]; then
        mkdir -p "$coursier_dir"
        if [[ ! -x "$TLD_BINARY_DIR/cs" ]]; then
          curl --fail --silent --show-error --location --retry 3 \
            https://github.com/coursier/launchers/raw/master/coursier \
            --output "$TLD_BINARY_DIR/cs"
          chmod +x "$TLD_BINARY_DIR/cs"
        fi
        COURSIER_BIN_DIR="$coursier_dir" "$TLD_BINARY_DIR/cs" install scip-java \
          || COURSIER_BIN_DIR="$coursier_dir" "$TLD_BINARY_DIR/cs" install --contrib scip-java
      fi
      ;;
    dart)
      if [[ ! -x "$pub_bin/scip-dart" && ! -x "$pub_bin/scip_dart" ]]; then
        dart pub global activate scip_dart
      fi
      ;;
    php)
      if [[ ! -x "$php_bin/scip-php" ]]; then
        # scip-php's releases pin google/protobuf ^3.22, whose every version is
        # covered by advisory PKSA-tcfz-w4fm-hhk9 (patched only in 4.33.6).
        # scip-php serializes its own output and never parses untrusted
        # protobuf, so the DoS is not reachable here.
        composer config --global policy.advisories.ignore-id PKSA-tcfz-w4fm-hhk9
        composer global require --no-interaction davidrjenni/scip-php
      fi
      ;;
    ruby)
      if [[ ! -x "$TLD_BINARY_DIR/scip-ruby" ]]; then
        if [[ "$RUNNER_ARCH" == X64 ]]; then
          curl --fail --silent --show-error --location --retry 3 \
            "https://github.com/sourcegraph/scip-ruby/releases/latest/download/scip-ruby-x86_64-linux" \
            --output "$TLD_BINARY_DIR/scip-ruby"
          chmod +x "$TLD_BINARY_DIR/scip-ruby"
        else
          gem install scip-ruby
        fi
      fi
      ;;
    rust)
      # rustup installs a rust-analyzer proxy even when the component is absent,
      # so test that it actually runs instead of checking for the shim.
      if ! rust-analyzer --version >/dev/null 2>&1; then
        rustup component add rust-analyzer
      fi
      ;;
    *)
      echo "Unknown indexer: $1"
      return 1
      ;;
  esac
}

for indexer in $INDEXERS; do
  if install_indexer "$indexer"; then
    echo "Installed SCIP indexer: $indexer"
  else
    echo "::warning::Failed to install SCIP indexer: $indexer"
  fi
done

if [[ -n "${GITHUB_PATH:-}" ]]; then
  for dir in "$TLD_BINARY_DIR" "$npm_prefix/bin" "$dotnet_dir" "$coursier_dir" "$pub_bin" "$php_bin" "$rust_bin"; do
    echo "$dir" >> "$GITHUB_PATH"
  done
fi
