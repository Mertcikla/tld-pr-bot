#!/usr/bin/env bash
# End-to-end validation of the tld PR bot inside a clean Ubuntu container.
#
# It installs the tld release binary and every SCIP indexer with the same script
# the composite action runs, probes each binary, checks `tld doctor`, then builds
# a small Go + TypeScript fixture repository with a base and head commit, drives
# the bot's init and generate modes exactly as the action does, and asserts a
# ready diagram.
set -euo pipefail

ACT=/action
PRBOT="$ACT/scripts/pr-bot"
export RUNNER_TEMP="${RUNNER_TEMP:-/tmp}"
export PYTHONPATH="$PRBOT"
state="$RUNNER_TEMP/tld-pr-bot"
repo="$RUNNER_TEMP/fixture"
bin="$state/bin"
indexers="${INDEXERS:-go typescript python dotnet clang java dart php ruby rust}"

export TLD_BINARY_DIR="$bin"
export INDEXERS="$indexers"
export GOWORK=off
export CGO_ENABLED=0
export PUB_CACHE="$bin/scip-pub"
export COMPOSER_HOME="$bin/scip-php"
export HOME="${HOME:-/root}"
export RUNNER_OS=Linux
case "$(uname -m)" in
  x86_64 | amd64) export RUNNER_ARCH=X64 ;;
  arm64 | aarch64) export RUNNER_ARCH=ARM64 ;;
esac
unset GITHUB_PATH || true

# Resolve the release once so the install and the bot agree on a version. Callers
# can pin TLD_VERSION to avoid the GitHub API lookup.
if [[ -z "${TLD_VERSION:-}" ]]; then
  TLD_VERSION="$(python3 -c 'import os; from lib import resolve_tld_version; print(resolve_tld_version("latest", token=os.environ.get("GITHUB_TOKEN")))')"
fi
export TLD_VERSION

echo "== installing tld $TLD_VERSION and indexers: $indexers =="
bash "$PRBOT/install-tools.sh"

# Resolve an indexer to the path the action's install places it at. The pub
# tool installs scip_dart and tld resolves the hyphen/underscore alias, so try
# both spellings.
indexer_bin() {
  case "$1" in
    go) echo "$bin/scip-go" ;;
    typescript) echo "$bin/scip-npm/bin/scip-typescript" ;;
    python) echo "$bin/scip-npm/bin/scip-python" ;;
    dotnet) echo "$bin/scip-dotnet/scip-dotnet" ;;
    clang) echo "$bin/scip-clang" ;;
    java) echo "$bin/scip-coursier/scip-java" ;;
    dart)
      if [[ -x "$PUB_CACHE/bin/scip_dart" ]]; then echo "$PUB_CACHE/bin/scip_dart"; else echo "$PUB_CACHE/bin/scip-dart"; fi
      ;;
    php) echo "$bin/scip-php/vendor/bin/scip-php" ;;
    ruby) echo "$bin/scip-ruby" ;;
    rust) echo "$HOME/.cargo/bin/rust-analyzer" ;;
    *) echo "" ;;
  esac
}
indexer_args() {
  case "$1" in
    php) echo "--help" ;;
    *) echo "--version" ;;
  esac
}

export PATH="$bin:$bin/scip-npm/bin:$bin/scip-dotnet:$bin/scip-coursier:$PUB_CACHE/bin:$bin/scip-php/vendor/bin:$HOME/.cargo/bin:$PATH"

echo "== probing SCIP binaries =="
for indexer in $indexers; do
  tool="$(indexer_bin "$indexer")"
  if [[ -z "$tool" ]]; then
    echo "no path mapping for indexer: $indexer" >&2
    exit 1
  fi
  if [[ ! -x "$tool" ]]; then
    echo "expected indexer not installed: $indexer ($tool)" >&2
    exit 1
  fi
  if ! "$tool" "$(indexer_args "$indexer")" >/dev/null 2>&1; then
    echo "indexer failed to run: $indexer ($tool)" >&2
    exit 1
  fi
  echo "  $indexer -> $tool"
done

echo "== checking tld doctor =="
python3 - "$bin/tld" "$indexers" <<'PY'
import json
import subprocess
import sys

tld, indexers = sys.argv[1], sys.argv[2].split()
report = json.loads(subprocess.run([tld, "doctor", "--json"], capture_output=True, text=True, check=True).stdout)
statuses = {tool["Name"]: tool for tool in report["tools"]}
wanted = {
    "go": "scip-go",
    "typescript": "scip-typescript",
    "python": "scip-python",
    "dotnet": "scip-dotnet",
    "clang": "scip-clang",
    "java": "scip-java",
    "dart": "scip-dart",
    "php": "scip-php",
    "ruby": "scip-ruby",
    "rust": "rust-analyzer",
}
for indexer in indexers:
    status = statuses.get(wanted[indexer], {})
    if not status.get("Found"):
        raise SystemExit(f"tld doctor did not find {wanted[indexer]}: {status}")
print("tld doctor found every requested indexer")
PY


echo "== building fixture repository =="
rm -rf "$repo"
mkdir -p "$repo/internal/greet" "$repo/cmd/app" "$repo/web/src"
git -C "$repo" init -q
git -C "$repo" config user.email test@example.com
git -C "$repo" config user.name test

cat >"$repo/go.mod" <<'EOF'
module example.com/fixture

go 1.24
EOF

cat >"$repo/internal/greet/greet.go" <<'EOF'
package greet

// Hello returns a greeting.
func Hello() string {
	return "hello"
}
EOF

cat >"$repo/cmd/app/main.go" <<'EOF'
package main

import (
	"fmt"

	"example.com/fixture/internal/greet"
)

func main() {
	fmt.Println(greet.Hello())
}
EOF

cat >"$repo/web/package.json" <<'EOF'
{
  "name": "fixture-web",
  "private": true,
  "version": "0.0.0"
}
EOF

cat >"$repo/web/tsconfig.json" <<'EOF'
{
  "compilerOptions": {
    "target": "es2020",
    "module": "commonjs",
    "moduleResolution": "node",
    "strict": false,
    "skipLibCheck": true,
    "noEmit": true
  },
  "include": ["src"]
}
EOF

cat >"$repo/web/src/greet.ts" <<'EOF'
export function hello(): string {
  return "hello";
}
EOF

cat >"$repo/web/src/app.ts" <<'EOF'
import { hello } from "./greet";

export function run(): string {
  return hello();
}
EOF

git -C "$repo" add -A
git -C "$repo" commit -qm base
base_sha="$(git -C "$repo" rev-parse HEAD)"

cat >"$repo/internal/greet/greet.go" <<'EOF'
package greet

// Hello returns a greeting.
func Hello() string {
	return "hello"
}

// Exclaim returns a louder greeting.
func Exclaim() string {
	return "HELLO!"
}
EOF

cat >"$repo/cmd/app/main.go" <<'EOF'
package main

import (
	"fmt"

	"example.com/fixture/internal/greet"
)

func main() {
	fmt.Println(greet.Hello())
	fmt.Println(greet.Exclaim())
}
EOF

cat >"$repo/web/src/greet.ts" <<'EOF'
export function hello(): string {
  return "hello";
}

export function exclaim(): string {
  return "HELLO!";
}
EOF

cat >"$repo/web/src/app.ts" <<'EOF'
import { exclaim, hello } from "./greet";

export function run(): string {
  return hello() + exclaim();
}
EOF

git -C "$repo" add -A
git -C "$repo" commit -qm head
head_sha="$(git -C "$repo" rev-parse HEAD)"

echo "== running bot (init + generate) =="
export GITHUB_REPOSITORY=Mertcikla/tld
export GITHUB_RUN_ID=1
export GITHUB_RUN_ATTEMPT=1
export INPUT_MODE=generate
export INPUT_REPOSITORY_PATH="$repo"
export INPUT_INDEXERS="$indexers"
export INPUT_TLD_VERSION="$TLD_VERSION"
export INPUT_PREPARE_COMMAND=
export INPUT_TOOLS_READY=true

python3 - "$state/event.json" "$base_sha" "$head_sha" <<'PY'
import json
import sys

path, base, head = sys.argv[1:4]
with open(path, "w") as handle:
    json.dump({
        "repository": {"full_name": "Mertcikla/tld"},
        "pull_request": {"number": 1, "head": {"sha": head}, "base": {"sha": base}},
    }, handle)
PY
export GITHUB_EVENT_PATH="$state/event.json"

python3 "$PRBOT/main.py" init
python3 "$PRBOT/main.py" generate

echo "== asserting result artifact =="
python3 - "$state/result/result.json" <<'PY'
import json
import sys

with open(sys.argv[1]) as handle:
    result = json.load(handle)
if result.get("status") != "ready":
    raise SystemExit(f"expected status ready, got: {result}")
markdown = result.get("markdown", "")
if not markdown.startswith("```mermaid\nflowchart LR"):
    raise SystemExit(f"expected a Mermaid flowchart, got: {markdown[:200]}")
print("tld PR bot end-to-end test passed")
PY
