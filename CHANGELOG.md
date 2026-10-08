# Changelog

## 0.1.0

- Offline branch/upstream/worktree inspection.
- Exact upstream mirroring without switching unchecked-out branches; non-fast-forward recovery refs.
- Explicit current-branch pull-with-rebase and sequential tracking-branch rebase, preserving pre-fetch fork points and conflict state.
- Fetching dry runs, branch selection, backup listing, and reversible restoration.
- Shared-worktree locking, unsafe fetch/symbolic-ref rejection, dirty and ignored-file preservation, and disabled submodule recursion.
- Preserve exact POSIX repository paths and non-UTF-8 filenames; prevent implicit network fetching during read-only inspection and report surviving backups on reset failure.
