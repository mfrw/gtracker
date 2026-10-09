# Changelog

## Unreleased

- Add explicit offline backup cleanup with per-branch `--keep N` retention or `--all`, exact branch filters, mandatory preview/confirmation, transactional expected-OID deletion, and worktree/symbolic-ref safeguards.
- Synchronize only upstream commits actually reported by successful fetches; preserve branches excluded by remote skip settings or negative refspecs, including overlapping fetch destinations.
- Preserve completed results and backup refs on Ctrl-C, stop Git and hook process groups before safe checkout restoration, and retain active or dirty recovery state without discarding work.
- Preserve terminal authentication and handle terminal Ctrl-C, including hooks that ignore SIGINT or retain captured output pipes.
- Diagnose malformed upstream configuration, including tab/newline values, instead of treating it as local-only; reject invalid selected metadata before implicit object fetching.
- Apply exact, deduplicated branch selection before resolving upstreams or walking history.
- Show live per-remote/native fetch progress and final updated, unchanged, skipped, failed, and previewed totals, including failed-fetch summaries.
- Enforce Git 2.45.0+ and add Linux/macOS CI for Python 3.11/3.14, including minimum-Git coverage and isolated test configuration.
- Group status into spaced, aligned sections with worktree markers and a lossless narrow-terminal layout.

## 0.1.0

- Offline branch/upstream/worktree inspection.
- Exact upstream mirroring without switching unchecked-out branches; non-fast-forward recovery refs.
- Explicit current-branch pull-with-rebase and sequential tracking-branch rebase, preserving pre-fetch fork points and conflict state.
- Fetching dry runs, branch selection, backup listing, and reversible restoration.
- Shared-worktree locking, unsafe fetch/symbolic-ref rejection, dirty and ignored-file preservation, and disabled submodule recursion.
- Preserve exact POSIX repository paths and non-UTF-8 filenames; prevent implicit network fetching during read-only inspection and report surviving backups on reset failure.
