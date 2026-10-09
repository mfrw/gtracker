from __future__ import annotations

import argparse
import shutil
import sys
from collections import Counter
from importlib.metadata import version

from gtracker.mirror import Backup, cleanup_backups, list_backups, restore, sync_mirror
from gtracker.rebase import RebaseConflict, pull_rebase, sync_rebase
from gtracker.repo import Branch, GitError, Repo, Result, UpdateError, UpdateInterrupted, locked


def _positive_backup_count(value: str) -> int:
    count = int(value)
    if count < 1:
        raise argparse.ArgumentTypeError("must be positive; use --all to delete every backup")
    return count


def _cleanup_options(command: argparse.ArgumentParser) -> None:
    policy = command.add_mutually_exclusive_group(required=True)
    policy.add_argument(
        "--keep", type=_positive_backup_count, metavar="N", help="keep newest N backups per branch"
    )
    policy.add_argument("--all", action="store_true", help="remove all selected backup refs")
    mode = command.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="preview without deleting backup refs")
    mode.add_argument("--yes", action="store_true", help="confirm deletion of selected backup refs")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="gtracker", description="Mirror tracking branches; explicitly rebase local work."
    )
    root.add_argument("--repo", default=".", help="repository path (default: current directory)")
    root.add_argument("--version", action="version", version=f"%(prog)s {version('gtracker')}")
    commands = root.add_subparsers(dest="command", required=True)
    status = commands.add_parser(
        "status", help="inspect last-fetched branch state without fetching"
    )
    sync = commands.add_parser("sync", help="fetch all remotes, then update tracking branches")
    pull = commands.add_parser("pull", help="fetch and rebase the current branch")
    backups = commands.add_parser("backups", help="list saved branch tips")
    recovery = commands.add_parser("restore", help="restore a branch from an exact backup ref")
    cleanup = commands.add_parser("cleanup", help="explicitly remove saved branch-tip backups")
    for command in (status, sync, pull, backups, recovery, cleanup):
        command.add_argument("--repo", default=argparse.SUPPRESS, help="repository path")
    for command in (status, sync, cleanup):
        command.add_argument(
            "--branch", action="append", default=[], help="branch name; repeat to select"
        )
    for command in (sync, pull, recovery):
        command.add_argument(
            "--dry-run", action="store_true", help="preview without local branch changes"
        )
    sync.add_argument("--strategy", choices=("mirror", "rebase"), default="mirror")
    pull.add_argument(
        "--rebase", action="store_true", required=True, help="explicitly allow rebasing"
    )
    pull.add_argument(
        "--upstream", help="remote/branch for this invocation; does not set tracking config"
    )
    recovery.add_argument(
        "backup", help="full refs/gtracker/backups/... ref from `gtracker backups`"
    )
    recovery.add_argument("--branch", help="restore into this existing local branch instead")
    _cleanup_options(cleanup)
    return root


def _status_state(branch: Branch) -> str:
    if branch.upstream_error:
        return "invalid-upstream"
    if not branch.upstream:
        return "local-only"
    if branch.upstream_oid is None:
        return "missing-upstream"
    if branch.ahead and branch.behind:
        return "diverged"
    if branch.ahead:
        return "ahead"
    if branch.behind:
        return "behind"
    return "matching"


def _status_cells(branch: Branch, state: str, marker: str) -> tuple[str, ...]:
    counts_known = branch.upstream_oid is not None and not branch.upstream_error
    return (
        f"{marker} {branch.name}",
        branch.upstream.removeprefix("refs/remotes/") or "-",
        str(branch.ahead) if counts_known else "-",
        str(branch.behind) if counts_known else "-",
        state,
    )


def _status_columns(cells: tuple[str, ...], widths: tuple[int, ...]) -> str:
    return (
        "  "
        + "  ".join(
            cell.rjust(width) if index in (2, 3) else cell.ljust(width)
            for index, (cell, width) in enumerate(zip(cells, widths, strict=True))
        ).rstrip()
    )


def show_status(repo: Repo, names: tuple[str, ...]) -> int:
    branches = repo.branches(names)
    current = repo.current_branch()
    trees = {tree.branch: tree for tree in repo.worktrees() if tree.branch}
    states = tuple(_status_state(branch) for branch in branches)
    rows = tuple(
        _status_cells(
            branch,
            state,
            "*" if branch.ref == current else "+" if branch.ref in trees else " ",
        )
        for branch, state in zip(branches, states, strict=True)
    )
    headers = ("  BRANCH", "UPSTREAM", "AHEAD", "BEHIND", "STATE")
    all_rows = (headers, *rows)
    widths = tuple(max(len(row[index]) for row in all_rows) for index in range(len(headers)))
    stacked = sum(widths) + 2 * len(headers) > shutil.get_terminal_size().columns
    print("Last-fetched state; no network access or branch updates.")
    print("* current branch; + checked out in another worktree")
    groups = (
        (
            "Attention needed",
            frozenset({"invalid-upstream", "missing-upstream", "diverged", "ahead", "behind"}),
        ),
        ("Matching", frozenset({"matching"})),
        ("Local-only", frozenset({"local-only"})),
    )
    for title, members in groups:
        selected = tuple(
            (branch, row)
            for branch, state, row in zip(branches, states, rows, strict=True)
            if state in members
        )
        if not selected:
            continue
        print(f"\n{title} ({len(selected)})")
        if not stacked:
            print(_status_columns(headers, widths))
        for branch, row in selected:
            if stacked:
                print(f"  {row[0]}\n    upstream: {row[1]}")
                print(f"    state: {row[4]}; ahead: {row[2]}; behind: {row[3]}")
            else:
                print(_status_columns(row, widths))
            if branch.upstream_error:
                for line in branch.upstream_error.splitlines():
                    print(f"    {line}")
            tree = trees.get(branch.ref)
            if tree:
                print(f"    worktree: {tree.path}" + (" (locked)" if tree.locked else ""))
            if stacked:
                print()
    if not branches:
        print("\nNo local branches.")
    sys.stdout.flush()
    return int("invalid-upstream" in states)


def _result_category(result: Result) -> str:
    if result.action in {"mirrored", "rebased", "restored"}:
        return "updated"
    if result.action == "unchanged":
        return "unchanged"
    if result.action.startswith("skipped"):
        return "skipped"
    if result.action.startswith("would-"):
        return "previewed"
    return "failed"


def show_results(
    results: tuple[Result, ...], *, outcome: str = "completed", dry_run: bool = False
) -> int:
    for result in results:
        print(
            f"{result.branch}: {result.action}" + (f" — {result.detail}" if result.detail else "")
        )
        if result.backup:
            print(f"  backup: {result.backup}")
    categories = tuple(_result_category(result) for result in results)
    stopped = outcome in {"failed", "interrupted"}
    terminal_failure = ("failed",) if stopped and "failed" not in categories else ()
    counts = Counter((*categories, *terminal_failure))
    warning = bool(counts["failed"]) or any(
        category == "skipped" and result.action != "skipped-local"
        for result, category in zip(results, categories, strict=True)
    )
    preview = dry_run or bool(counts["previewed"])
    label = outcome if stopped else "preview" if preview else "completed"
    if warning and not stopped:
        label += " with warnings"
    labels = ("updated", "unchanged", "skipped", "failed") + (("previewed",) if preview else ())
    totals = ", ".join(f"{counts[category]} {category}" for category in labels)
    print(f"\nSummary ({label}): {totals}", flush=True)
    return int(warning)


def show_cleanup(backups: tuple[Backup, ...], *, dry_run: bool) -> int:
    action = "would-delete" if dry_run else "deleted"
    for backup in backups:
        print(f"{backup.branch}: {action} — {backup.ref} ({backup.oid[:12]})")
    label = "preview" if dry_run else "completed"
    verb = "would be deleted" if dry_run else "deleted"
    print(f"\nSummary ({label}): {len(backups)} backup refs {verb}.", flush=True)
    return 0


def execute(args: argparse.Namespace, repo: Repo) -> int:
    if args.command == "status":
        return show_status(repo, tuple(args.branch))
    if args.command == "backups":
        saved = list_backups(repo)
        if not saved:
            print("No gtracker backups.")
        for backup in saved:
            print(f"{backup.branch}\t{backup.oid[:12]}\t{backup.ref}")
        return 0
    with locked(repo):
        if args.command == "cleanup":
            deleted = cleanup_backups(
                repo, tuple(args.branch), keep=args.keep, dry_run=args.dry_run
            )
            return show_cleanup(deleted, dry_run=args.dry_run)
        if args.command == "restore":
            return show_results(
                (restore(repo, args.backup, args.branch, args.dry_run),), dry_run=args.dry_run
            )
        if args.dry_run:
            print(
                "Dry run: fetching updates remote-tracking refs; local branches remain unchanged.",
                flush=True,
            )
        if args.command == "pull":
            return show_results(
                (pull_rebase(repo, args.upstream, args.dry_run),), dry_run=args.dry_run
            )
        sync = sync_mirror if args.strategy == "mirror" else sync_rebase
        return show_results(sync(repo, tuple(args.branch), args.dry_run), dry_run=args.dry_run)


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        repo = Repo.open(args.repo, offline=args.command in {"status", "backups", "cleanup"})
        return execute(args, repo)
    except UpdateInterrupted as exc:
        show_results(exc.results, outcome="interrupted", dry_run=getattr(args, "dry_run", False))
        print(f"gtracker: {exc}", file=sys.stderr, flush=True)
        return 130
    except (GitError, OSError) as exc:
        if isinstance(exc, (UpdateError, RebaseConflict)):
            results = exc.results
            if isinstance(exc, RebaseConflict) and not any(
                result.branch == exc.branch and _result_category(result) == "failed"
                for result in results
            ):
                results = (*results, Result(exc.branch, "failed", exc.detail, exc.backup))
            show_results(results, outcome="failed", dry_run=getattr(args, "dry_run", False))
        elif isinstance(exc, GitError) and args.command in {"sync", "pull", "restore"}:
            show_results((), outcome="failed", dry_run=getattr(args, "dry_run", False))
        print(f"gtracker: {exc}", file=sys.stderr, flush=True)
        return 1
    except KeyboardInterrupt:
        print(
            "gtracker: interrupted; inspect Git state before retrying", file=sys.stderr, flush=True
        )
        return 130
