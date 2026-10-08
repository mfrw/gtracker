# gtracker

Keep local Git tracking branches aligned with their upstreams, without checking out each branch. Explicitly rebase local work when requested.

**`sync` treats every local branch with a configured upstream as a disposable mirror.** Ahead or divergent tracking branches are replaced with the upstream's exact commit. Branches without an upstream are not enrolled in bulk synchronization. Non-fast-forward replacements preserve the previous tip under `refs/gtracker/backups/`.

Requires Python 3.11+, Git 2.45.0+, and Linux/macOS (POSIX file locking and process groups). Git 2.45 introduced the `GIT_NO_LAZY_FETCH` control used for offline inspection; older or unrecognized Git versions are rejected before repository operations. Prefer a current security-maintained Git release. No runtime Python dependencies. Git repository configuration, hooks, and remote transports are trusted; this is not a sandbox for untrusted repositories.

## Run

From this project:

```sh
uv sync
uv run gtracker status --repo ~/linux
uv run gtracker sync --repo ~/linux --dry-run
uv run gtracker sync --repo ~/linux
```

From any directory:

```sh
uv run --project ~/pyutils/gtracker gtracker status --repo ~/linux
```

Optional installation onto your user tool PATH:

```sh
uv tool install --editable ~/pyutils/gtracker
```

`--repo` defaults to the current directory and can appear before or after the subcommand. It accepts a worktree path or a directory inside one. Bare repositories are refused.

### Symlink into `~/bin`

Create the project's virtual environment and link its installed entrypoint:

```sh
uv sync --project "$HOME/pyutils/gtracker"
mkdir -p "$HOME/bin"
ln -s "$HOME/pyutils/gtracker/.venv/bin/gtracker" "$HOME/bin/gtracker"
```

`ln -s` intentionally does not overwrite an existing `~/bin/gtracker`. If that name is already in use, inspect it before choosing another name or replacing the existing link.

Make `~/bin` available in the current shell:

```sh
export PATH="$HOME/bin:$PATH"
command -v gtracker
```

Add the same `export PATH=...` line to your shell startup file if `~/bin` is not already on your PATH: `~/.bashrc` for Bash or `~/.zshrc` for Zsh. Open a new terminal after saving it. You do not need to activate the virtual environment or prefix commands with `uv run`: the linked entrypoint uses the project's Python interpreter directly.

Link `.venv/bin/gtracker`, not a Python file under `src/`. Keep the project and virtual environment at their current paths; the generated entrypoint has an absolute interpreter path. After moving the project, recreate its environment with `uv sync` and recreate the link. Editable installation means source changes in the existing project are picked up without relinking.

### Use the repository you are in

The symlink does not change your working directory or make the gtracker project the target repository:

```sh
cd ~/linux
gtracker status
gtracker sync --dry-run
gtracker sync
gtracker pull --rebase
```

These commands use `~/linux` because that is your current Git worktree. The same applies from a subdirectory such as `~/linux/drivers`: Git locates the containing repository automatically. `pull --rebase` acts on that worktree's current branch and requires a configured or explicitly supplied upstream.

Outside a Git worktree, or to target a different repository, supply its path:

```sh
gtracker status --repo ~/linux
```

Without `--repo`, running outside a Git worktree reports an error; it does not silently fall back to the kernel tree or to the tool's own project.


## Commands

### Inspect without fetching

```sh
gtracker status --repo ~/linux
gtracker status --repo ~/linux --branch master --branch mm-everything
```

Reports full local branch names, upstreams, ahead/behind counts, and registered checkout locations. Aligned columns separate **Attention needed**, **Matching**, and **Local-only** groups with counts and blank lines. `*` marks the current branch; `+` marks another worktree's branch. Paths and diagnostics appear on continuation lines. Narrow terminals use a stacked layout rather than truncating names.

Counts describe the last-fetched refs, not the current remote server. Repeated `--branch` selections are deduplicated and applied before upstream resolution or history walks, so unrelated branches are not inspected. Malformed remote/merge configuration is reported as `invalid-upstream`, not local-only, and makes `status` fail. `status` and `backups` disable Git's implicit lazy fetching in partial clones; missing local objects produce an error instead of network access. Hydrate them with an explicit fetch before inspecting again.

### Mirror upstreams

```sh
gtracker sync --repo ~/linux
gtracker sync --repo ~/linux --branch mm-everything --branch rcu-dev
```

Fetches eligible remotes sequentially with pruning, using Git's fetch configuration, then mirrors the selected branches (or all configured-upstream branches by default). Any fetch failure stops local branch updates, although successful fetches may already have changed remote-tracking refs. All selected upstream configuration is validated before resolving upstream objects or fetching, including implicit partial-clone downloads. Invalid values containing tabs or newlines produce diagnostics rather than tracebacks.

Only commits reported by that remote's successful fetch can supply an update. Branches whose remotes were skipped, or whose refs were excluded by fetch configuration, are reported without using stale cached refs. Both `remote.<name>.skipFetchAll` and its deprecated `skipDefaultUpdate` alias are respected; the last setting wins. Local upstreams configured with remote `.` use local refs instead of requiring a network fetch.

Per-remote progress and native Git transfer output appear live on stderr. Results and backup refs appear on stdout, followed by updated, unchanged, skipped, and failed totals. Dry runs also count previewed changes; failed fetches, partial failures, and interruptions are labelled separately from successful completion.

Unchecked-out branches use expected-old-OID ref transactions. A non-fast-forward branch replacement and its backup are committed together. Checked-out branches are updated in their existing worktrees with `git reset --keep`; no checkout loop is needed. A dirty or locked affected worktree is skipped. A missing upstream is reported without deleting its local branch.

**Dry runs fetch.** `sync --dry-run` updates remote-tracking refs and can prune deleted remote refs, but does not change local branch tips, working files, or backup refs. It reports the planned local updates and safety skips.

### Rebase the current branch

```sh
gtracker pull --rebase --repo ~/linux
```

Uses its configured upstream. For a local-only development branch, supply an upstream for this invocation:

```sh
gtracker pull --rebase --upstream stable/linux-6.6.y --repo ~/linux
```

The explicit upstream must resolve to an existing remote-tracking ref. It does **not** set branch tracking configuration, so the development branch remains outside bulk mirroring. Fetch that remote first if the tracking ref is not yet present.

This implements pull-with-rebase integration using one fetch and a pinned target commit, rather than issuing a second fetch during `git pull`. It captures the pre-fetch fork point so rebuilt upstream history is not replayed as local work. Genuine local commits are replayed when needed; already-ahead branches need no rewrite. The old tip is saved before an update. Git's automatic stashing and multi-branch `rebase.updateRefs` behavior are disabled.

Rebase selected tracking branches instead of mirroring them:

```sh
gtracker sync --strategy rebase --branch rcu-dev --branch tglx-master --repo ~/linux
```

Rebases run sequentially. Existing checked-out branches use their worktrees; other branches temporarily use the calling worktree. Successful batches restore the initial checkout, including an initially detached HEAD. Ordinary failures and Ctrl-C also attempt restoration when safe and report earlier completed updates and backups. A batch is not globally atomic: earlier successful branches remain updated. Once created, rebase recovery refs are retained even if the attempt fails before changing the branch tip; pre-detected unchanged branches create no backup.

`pull --rebase --dry-run` and `sync --strategy rebase --dry-run` fetch and preview without rewriting local refs or creating backups. They do not attempt a rebase and cannot predict all merge conflicts.

### Resolve a conflict

A conflicting rebase stops the batch immediately. Its worktree and conflict state remain available; the error includes its path, backup ref, and recovery commands:

```sh
git -C /path/to/affected/worktree status
# Resolve files, then git add the resolutions.
git -C /path/to/affected/worktree rebase --continue
# Or abandon the rebase:
git -C /path/to/affected/worktree rebase --abort
```

A stopped batch never switches away from the conflicting checkout. If the conflict is in a different linked worktree, the unaffected calling checkout is restored when safe. After manual resolution or abort in the affected worktree, switch back to your desired branch yourself. No automatic conflict resolution or pushing occurs.

Ctrl-C exits with status `130`. Git and its hook process group are stopped before automatic checkout recovery. A clean, unaffected caller can be restored; dirty or active caller state is left in place with recovery instructions. An active linked rebase is preserved while the caller is restored when safe. If process-group termination cannot be confirmed, automatic restoration is disabled and the diagnostic tells you to inspect the remaining processes. Ordinary terminal authentication prompts remain available during fetching.

### List and restore saved tips

```sh
gtracker backups --repo ~/linux
gtracker restore refs/gtracker/backups/<id>/<branch> --repo ~/linux --dry-run
gtracker restore refs/gtracker/backups/<id>/<branch> --repo ~/linux
```

Copy the full ref from `backups`; arbitrary Git revisions are not accepted. Restore defaults to the original branch, which must still exist. `--branch existing-local-branch` chooses another existing branch without changing its upstream configuration. Restoration saves the replaced tip too, and refuses dirty/locked worktrees or active Git operations. Backups are direct commit refs outside `refs/heads/`, keep their objects reachable, and are never automatically expired.

## Safety boundaries

- No automatic stashing, hard reset, branch deletion, merge commits, conflict resolution, or push.
- Active merge/rebase/cherry-pick/revert/bisect operations or unavailable registered worktrees block mutations.
- Registered worktree paths must still refer to their original worktrees. Do not replace a registered path with another clone; repair moved worktrees with `git worktree repair` before synchronization. Repository identity at stale registered paths is not currently validated.
- Ignored-file collisions, including rename destinations, root-commit additions, and intermediate rebase additions, are refused. Unrelated ignored kernel build artifacts remain in place.
- Checked-out worktrees using assume-unchanged/skip-worktree index flags are refused conservatively; sparse checkouts are not automatically updated.
- Submodule recursion is disabled even if Git configuration enables it. Submodule working trees are not automatically advanced; dirty submodules still make the containing worktree unsafe.
- Unsafe fetch mappings or destination symrefs that can overwrite local branches/backup refs are refused before fetching. Symbolic local branches and symbolic mutation pseudorefs are refused.
- A lock in the shared Git directory prevents overlapping **gtracker** mutations across linked worktrees. Do not run other Git mutations concurrently: the lock cannot exclude ordinary Git commands. Expected-old OIDs detect ref races but do not provide a repository-wide Git lock.

Exit status is `0` for successful completion (local-only skips are normal), `1` for errors, invalid configuration, or unsafe/missing/unfetched-upstream skips, `2` for invalid CLI usage, and `130` for interruption. Some branches can be updated before a later failure; read the reported results and inspect Git state before retrying.

## Development

```sh
uv sync --group dev
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy
uv run pytest --cov=gtracker --cov-report=term-missing
uv build
```

Tests use isolated disposable local Git repositories and temporary HOME/XDG configuration, not your kernel tree. Coverage includes the subprocess CLI and has an 80% minimum. GitHub Actions checks formatting, lint, strict types, coverage, and distribution builds on Python 3.11 and 3.14 across Linux and macOS. The Linux/Python 3.11 job builds and exercises the minimum Git 2.45.0 release from a checksum-verified source archive.
