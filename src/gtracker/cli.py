from __future__ import annotations

import argparse
import sys
from importlib.metadata import version

from gtracker.mirror import list_backups, restore, sync_mirror
from gtracker.rebase import RebaseConflict, pull_rebase, sync_rebase
from gtracker.repo import GitError, Repo, Result, UpdateError, locked


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
    for command in (status, sync, pull, backups, recovery):
        command.add_argument("--repo", default=argparse.SUPPRESS, help="repository path")
    for command in (status, sync):
        command.add_argument(
            "--branch", action="append", default=[], help="local branch; repeat to select"
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
    return root


def show_status(repo: Repo, names: tuple[str, ...]) -> None:
    branches = repo.branches()
    unknown = frozenset(names) - frozenset(branch.name for branch in branches)
    if unknown:
        raise GitError(f"Unknown local branches: {', '.join(sorted(unknown))}")
    current = repo.current_branch()
    trees = {tree.branch: tree for tree in repo.worktrees() if tree.branch}
    print("Last-fetched state; no network access or branch updates.")
    print("BRANCH\tUPSTREAM\tAHEAD\tBEHIND\tSTATE")
    for branch in branches:
        if names and branch.name not in names:
            continue
        if not branch.upstream:
            state = "local-only"
        elif branch.upstream_oid is None:
            state = "missing-upstream"
        elif branch.ahead and branch.behind:
            state = "diverged"
        elif branch.ahead:
            state = "ahead"
        elif branch.behind:
            state = "behind"
        else:
            state = "matching"
        tree = trees.get(branch.ref)
        if tree:
            state += f"; worktree={tree.path}"
            if tree.locked:
                state += "; locked"
        prefix = "*" if branch.ref == current else " "
        upstream = branch.upstream.removeprefix("refs/remotes/") or "-"
        print(f"{prefix}{branch.name}\t{upstream}\t{branch.ahead}\t{branch.behind}\t{state}")


def show_results(results: tuple[Result, ...]) -> int:
    for result in results:
        print(
            f"{result.branch}: {result.action}" + (f" — {result.detail}" if result.detail else "")
        )
        if result.backup:
            print(f"  backup: {result.backup}")
    return int(
        any(
            result.action in {"skipped", "skipped-missing", "skipped-unfetched", "failed"}
            for result in results
        )
    )


def execute(args: argparse.Namespace, repo: Repo) -> int:
    if args.command == "status":
        show_status(repo, tuple(args.branch))
        return 0
    if args.command == "backups":
        saved = list_backups(repo)
        if not saved:
            print("No gtracker backups.")
        for backup in saved:
            print(f"{backup.branch}\t{backup.oid[:12]}\t{backup.ref}")
        return 0
    with locked(repo):
        if args.command == "restore":
            return show_results((restore(repo, args.backup, args.branch, args.dry_run),))
        if args.dry_run:
            print(
                "Dry run: fetching updates remote-tracking refs; local branches remain unchanged."
            )
        else:
            print("Fetching upstreams...", flush=True)
        if args.command == "pull":
            return show_results((pull_rebase(repo, args.upstream, args.dry_run),))
        sync = sync_mirror if args.strategy == "mirror" else sync_rebase
        return show_results(sync(repo, tuple(args.branch), args.dry_run))


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        repo = Repo.open(args.repo, offline=args.command in {"status", "backups"})
        return execute(args, repo)
    except (GitError, OSError) as exc:
        if isinstance(exc, (UpdateError, RebaseConflict)):
            show_results(exc.results)
        print(f"gtracker: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("gtracker: interrupted; inspect Git state before retrying", file=sys.stderr)
        return 130
