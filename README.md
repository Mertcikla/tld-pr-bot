# tld PR diagram bot

A reusable GitHub composite action that generates and caches PR impact diagrams in mermaid format
with the [tld](https://github.com/Mertcikla/tld) CLI, then publishes them from a
trusted workflow. It has two modes:

| Mode | Purpose |
| --- | --- |
| `generate` | Compare a PR's merge base to HEAD, restore/save snapshots, upload a result artifact |
| `publish` | Verify the originating run and update one bot-owned PR comment |

All bot failures are advisory. The publisher only comments when a diagram is
ready. A successful comment displays only the Mermaid diagram. Empty comparisons,
oversized output, and setup or indexing errors create no new comment. If a diagram
was already published, an unsuccessful update preserves it and adds a
“Diagram is stale” notice. The next successful update replaces the diagram and
removes the notice. Details are written to the workflow console. Existing test
and lint checks are independent of this bot.

The action downloads the tld release named by the `tld-version` input, which
defaults to `latest`. The bot therefore needs no version bump when tld ships; a
deployment can pin an exact tag by setting `tld-version: v2.4.2`.

## Installing in another repository

Use an Ubuntu runner (x86_64 or ARM64). Replace `PINNED_REF` in both examples
with a release tag or commit of this repository that contains the action.
The action sets up Go 1.26.2, Node 24, and (when requested) Dart, then installs
the selected SCIP indexers using the toolchains already on the Ubuntu runner.
The default `indexers: auto` installs every supported indexer: `scip-go`,
`scip-typescript`, `scip-python`, `scip-dotnet`, `scip-clang`, `scip-java`,
`scip-dart`, `scip-php`, `scip-ruby`, and `rust-analyzer`. Give `indexers` a
subset to install only those. A failed indexer install is a warning, not a
failure; tld omits the diagram only for projects that need the missing tool. The
bot itself runs on the `python3` provided by the Ubuntu runner.

The producer and publisher must have different permissions. The publisher
workflow must exist on the default branch before it can receive `workflow_run`
events. Do not execute PR code in the publisher.

Create `.github/workflows/pr-diagram.yml`:

```yaml
name: PR diagram
on:
  pull_request:
    types: [opened, synchronize, reopened, edited]
permissions:
  contents: read
concurrency:
  group: pr-diagram-${{ github.event.pull_request.number }}
  cancel-in-progress: true
jobs:
  diagram:
    runs-on: ubuntu-latest
    timeout-minutes: 45
    continue-on-error: true
    steps:
      - uses: actions/checkout@v6
        with:
          ref: ${{ github.event.pull_request.head.sha }}
          fetch-depth: 0
          persist-credentials: false
      - uses: Mertcikla/pr-bot@PINNED_REF
        env:
          TLD_PR_MAX_ELEMENTS: '40'
          TLD_PR_MAX_CONNECTORS: '80'
        with:
          mode: generate
          tld-version: latest
          indexers: auto  # or a subset, e.g. go typescript python
          # Customize for your repo; this runs separately inside each revision.
          prepare-command: npm --prefix frontend ci --ignore-scripts
```

Create `.github/workflows/pr-diagram-publish.yml`:

```yaml
name: Publish PR diagram
on:
  workflow_run:
    workflows: [PR diagram]
    types: [completed]
permissions:
  contents: read
  actions: read
  pull-requests: write
concurrency:
  group: pr-diagram-publish-${{ github.event.workflow_run.head_repository.id }}-${{ github.event.workflow_run.head_branch }}
  cancel-in-progress: false
jobs:
  comment:
    if: github.event.workflow_run.event == 'pull_request'
    runs-on: ubuntu-latest
    timeout-minutes: 5
    continue-on-error: true
    steps:
      - uses: Mertcikla/pr-bot@PINNED_REF
        with:
          mode: publish
          source-workflow: pr-diagram.yml
```

An empty preparation command is appropriate for projects that need no setup.
Because the action is fetched from this repository at `PINNED_REF`, PR source
changes cannot change the bot code or cache namespace on a synchronize event.

## Configuration

| Setting | Default | Meaning |
| --- | --- | --- |
| `tld-version` | `latest` | tld release tag to download, or `latest` for the newest stable release |
| `TLD_PR_MAX_ELEMENTS` | `40` | Maximum diagram nodes |
| `TLD_PR_MAX_CONNECTORS` | `80` | Maximum diagram dependency edges |
| `repository-path` | current workspace | Checkout to index |
| `prepare-command` | empty | Bash setup inside each uncached commit worktree |
| `indexers` | `auto` | SCIP indexers to install; `auto` installs all (go, typescript, python, dotnet, clang, java, dart, php, ruby, rust) |
| `artifact-name` | `tld-pr-diagram` | Must match between producer and publisher |
| `run-id` | triggering run | Must match the `workflow_run` event |
| `source-workflow` | `pr-diagram.yml` | Expected generator workflow filename |
| `github-token` | workflow token | Resolves the latest tld release and authorizes publication |

Both limits must be positive integers. Zero, negatives, fractions, and malformed
values produce an advisory configuration error. Limits are checked before
Mermaid rendering and automatic budget shrinking. They count the requested PR
diagram, rather than every symbol or relation in the full repository indexes.
Equality is permitted; exceeding either limit skips the whole diagram.

Consumers typically read the limits from GitHub repository variables with the
same names, falling back to 40 and 80, or set them in the action step's `env`
block.

Artifact size is capped at 256 KiB and comment size at 60 KiB. Oversized output
becomes a short skip status. Indexer warnings omit the diagram rather than
presenting incomplete dependency results. Failures and reasons are logged in
the job summary; detailed preparation/indexer output stays in the run logs.

## Cache behavior and fork PRs

The action caches the release binary and indexers separately from the isolated
tld database. Snapshot keys include repository identity, action source digest,
the tld release, runtime and indexer versions, enabled indexers, and preparation configuration.
Binary cache keys include the tld release, runtime/indexer versions, and runner
OS/architecture; action source changes do not redownload the same release.
Restore reuses the PR's previous database, preferring the same BASE revision.
The database is saved after the compare process closes, including SQLite
sidecars when present. Snapshots are retained even when a diagram is skipped.
Temporary worktrees, config files, checkouts, and credentials are excluded.

The first run indexes BASE and HEAD. Subsequent runs reuse compatible snapshots
from that PR's cache. An evicted entry, a different action version, setup changes,
or indexer changes may require indexing BASE again. First-time fork workflows may require the
repository's normal GitHub Actions approval before generation can run.

Publication verifies the run's repository, event, workflow path, and PR
association through GitHub APIs. Missing fork associations fall back to verified
commit/PR associations. It rejects mismatched artifact identities, checks the
current HEAD and target BASE, and ignores closed/stale PRs. Artifacts are data
only. A stable marker and `github-actions[bot]` ownership identify the comment;
run metadata prevents older publications from replacing a newer one.

After merging, verify one same-repository PR and one fork PR.
Confirm a synchronize reuses compatible snapshots and updates the existing comment, and
temporarily lower a limit to check that a stale notice appears below the preserved diagram
while CI remains green.

## Local validation

```bash
python3 -m unittest discover -s scripts/pr-bot -p 'test_*.py' -v
actionlint .github/workflows/*.yml
scripts/pr-bot/test-action-docker.sh          # end-to-end, requires Docker
```

The Docker test builds an Ubuntu image with the toolchains the action relies on
(Go, Node, PHP/Composer, .NET, Java, Dart, Ruby, Rust) and, using
`scripts/pr-bot/install-tools.sh` (the same script the action runs), installs the
tld release plus every SCIP indexer. It probes each binary, confirms `tld doctor`
finds them all, then drives `main.py init` and `main.py generate` against a small
Go + TypeScript fixture repository and asserts a ready Mermaid diagram. Override
`INDEXERS` to test a subset, pin `TLD_VERSION`, or set `PLATFORM` (defaults to
`linux/amd64`, since the `scip-clang` and `scip-ruby` releases only publish
x86_64 Linux artifacts).
