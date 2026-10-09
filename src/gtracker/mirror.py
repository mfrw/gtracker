from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import groupby
from uuid import uuid4

from gtracker.repo import (
    Branch,
    FetchReport,
    GitError,
    Repo,
    Result,
    UpdateError,
    UpdateInterrupted,
    Worktree,
)

BACKUP_PREFIX = "refs/gtracker/backups/"


@dataclass(frozen=True)
class Backup:
    ref: str
    branch: str
    oid: str


def _backup_ref(branch: Branch) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    return f"{BACKUP_PREFIX}{stamp}-{uuid4().hex}/{branch.name}"


def _require_direct(repo: Repo, branch: Branch) -> None:
    if repo.git("symbolic-ref", "-q", branch.ref, check=False).returncode == 0:
        raise GitError(f"Refusing symbolic local branch: {branch.name}")
    if repo.oid(branch.ref) != branch.oid:
        raise GitError(f"Branch changed concurrently: {branch.name}; inspect and retry")


def _interrupted(
    repo: Repo, branch: Branch, backup: str | None, exc: KeyboardInterrupt | None = None
) -> UpdateInterrupted:
    saved = backup if backup and repo.oid(backup) == branch.oid else None
    detail = f"inspect Git state at {repo.path}; original tip: {branch.oid}"
    return UpdateInterrupted(
        f"{exc}\nInterrupted updating {branch.name}: {detail}"
        if exc
        else f"Interrupted updating {branch.name}: {detail}",
        (Result(branch.name, "interrupted", detail, saved),),
        recovery_safe=exc.recovery_safe if isinstance(exc, UpdateInterrupted) else True,
    )


def create_backup(repo: Repo, branch: Branch, reason: str = "update") -> str:
    _require_direct(repo, branch)
    ref = _backup_ref(branch)
    try:
        repo.git(
            "update-ref",
            "-m",
            f"gtracker backup before {reason}",
            "--stdin",
            input=(
                f"start\nverify {branch.ref} {branch.oid}\n"
                f"create {ref} {branch.oid}\nprepare\ncommit\n"
            ),
        )
    except KeyboardInterrupt as exc:
        raise _interrupted(repo, branch, ref, exc) from exc
    return ref


def list_backups(repo: Repo) -> tuple[Backup, ...]:
    fmt = "%(refname)%09%(objectname)%09%(objecttype)%09%(symref)"
    rows = repo.git("for-each-ref", f"--format={fmt}", BACKUP_PREFIX).stdout
    return tuple(_parse_backup(row) for row in rows.splitlines())


def _parse_backup(row: str) -> Backup:
    ref, oid, kind, symbolic = row.split("\t")
    if kind != "commit" or symbolic:
        raise GitError(f"Invalid gtracker backup (requires a direct commit ref): {ref}")
    parts = ref.removeprefix(BACKUP_PREFIX).split("/", 1)
    if len(parts) != 2:
        raise GitError(f"Malformed gtracker backup ref: {ref}")
    return Backup(ref, parts[1], oid)


def _backup_time(backup: Backup) -> datetime:
    generation = backup.ref.removeprefix(BACKUP_PREFIX).split("/", 1)[0]
    stamp = generation.partition("-")[0]
    try:
        return datetime.strptime(stamp, "%Y%m%dT%H%M%S.%fZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise GitError(
            f"Cannot determine backup creation time: {backup.ref}; "
            "use --all for explicit cleanup instead of --keep"
        ) from exc


def _older_backups(backups: tuple[Backup, ...], keep: int) -> tuple[Backup, ...]:
    ordered = sorted(
        backups, key=lambda backup: (backup.branch, _backup_time(backup), backup.ref), reverse=True
    )
    return tuple(
        backup
        for _, history in groupby(ordered, key=lambda backup: backup.branch)
        for backup in tuple(history)[keep:]
    )


def _ensure_backups_unused(repo: Repo, backups: tuple[Backup, ...]) -> None:
    refs = frozenset(backup.ref for backup in backups)
    root_refs = (
        "HEAD",
        "FETCH_HEAD",
        "ORIG_HEAD",
        "MERGE_HEAD",
        "MERGE_AUTOSTASH",
        "REBASE_HEAD",
        "REVERT_HEAD",
        "CHERRY_PICK_HEAD",
        "BISECT_HEAD",
        "AUTO_MERGE",
    )
    for tree in repo.worktrees():
        checkout = Repo(tree.path, offline=True)
        for name in root_refs:
            result = checkout.git("symbolic-ref", "-q", name, check=False)
            if result.returncode not in (0, 1):
                raise GitError(result.stderr.strip() or f"Cannot inspect {name} at {tree.path}")
            target = result.stdout.removesuffix("\n")
            if target in refs:
                raise GitError(f"Backup is used by {name} at {tree.path}: {target}")
        rows = checkout.git("for-each-ref", "--format=%(refname)%09%(symref)").stdout
        for row in rows.splitlines():
            ref, target = row.split("\t")
            if target in refs:
                raise GitError(f"Backup is used by symbolic ref {ref} at {tree.path}: {target}")


def cleanup_backups(
    repo: Repo,
    names: tuple[str, ...] = (),
    *,
    keep: int | None,
    dry_run: bool = False,
) -> tuple[Backup, ...]:
    """Delete selected backup refs; keep=None explicitly selects all saved tips."""
    if keep is not None and keep < 1:
        raise GitError("--keep must be positive; use --all to remove every selected backup")
    offline = Repo(repo.path, offline=True)
    backups = list_backups(offline)
    selected = frozenset(names)
    unknown = selected - {backup.branch for backup in backups}
    if unknown:
        raise GitError(f"No gtracker backups for branch(es): {', '.join(sorted(unknown))}")
    matching = tuple(backup for backup in backups if not selected or backup.branch in selected)
    candidates = matching if keep is None else _older_backups(matching, keep)
    if not candidates:
        return ()
    offline.ensure_no_operations()
    _ensure_backups_unused(offline, candidates)
    if not dry_run:
        deletes = "".join(f"delete {backup.ref} {backup.oid}\n" for backup in candidates)
        offline.git(
            "update-ref", "--no-deref", "--stdin", input=f"start\n{deletes}prepare\ncommit\n"
        )
    return candidates


def _branch_tree(repo: Repo, branch: Branch) -> Worktree | None:
    trees = tuple(tree for tree in repo.worktrees() if tree.branch == branch.ref)
    if len(trees) > 1:
        raise GitError(f"Branch is checked out in multiple worktrees: {branch.name}")
    return trees[0] if trees else None


def _checked_tree(repo: Repo, branch: Branch, target: str) -> Repo | None:
    tree = _branch_tree(repo, branch)
    if tree is None:
        return None
    if tree.locked:
        raise GitError(f"Worktree is locked: {tree.path}")
    checkout = Repo.open(tree.path)
    checkout.ensure_clean(target)
    if checkout.current_branch() != branch.ref:
        raise GitError(f"Worktree checkout changed: {tree.path}")
    return checkout


def _replace(repo: Repo, branch: Branch, target: str, *, save: bool, reason: str) -> str | None:
    _require_direct(repo, branch)
    checkout = _checked_tree(repo, branch, target)
    if checkout:
        backup = create_backup(repo, branch, reason) if save else None
        try:
            _require_direct(repo, branch)
            checkout.git("reset", "--keep", target)
        except KeyboardInterrupt as exc:
            raise _interrupted(checkout, branch, backup, exc) from exc
        except GitError as exc:
            if backup:
                raise GitError(f"{exc}\nOriginal tip backup: {backup}") from exc
            raise
        return backup
    backup = _backup_ref(branch) if save else None
    commands = f"start\nupdate {branch.ref} {target} {branch.oid}\n"
    if backup:
        commands += f"create {backup} {branch.oid}\n"
    # Manual concurrent Git mutations are unsupported; recheck immediately before commit.
    if _branch_tree(repo, branch):
        raise GitError(f"Branch was checked out concurrently: {branch.name}")
    try:
        repo.git(
            "update-ref",
            "-m",
            f"gtracker {reason}",
            "--stdin",
            input=commands + "prepare\ncommit\n",
        )
    except KeyboardInterrupt as exc:
        raise _interrupted(repo, branch, backup, exc) from exc
    return backup


def _mirror_branch(repo: Repo, branch: Branch, fetched: FetchReport, dry_run: bool) -> Result:
    if branch.upstream_error:
        raise GitError(f"Invalid upstream for {branch.name}: {branch.upstream_error}")
    if not branch.upstream:
        return Result(branch.name, "skipped-local", "no configured upstream")
    target = (
        branch.upstream_oid if branch.remote == "." else fetched.oid(branch.remote, branch.upstream)
    )
    if target is None:
        if branch.upstream_oid is None:
            return Result(branch.name, "skipped-missing", f"upstream missing: {branch.upstream}")
        return Result(
            branch.name, "skipped-unfetched", f"upstream was not fetched: {branch.upstream}"
        )
    if branch.oid == target:
        return Result(branch.name, "unchanged")
    _require_direct(repo, branch)
    try:
        _checked_tree(repo, branch, target)
    except GitError as exc:
        return Result(branch.name, "skipped", str(exc))
    ahead = int(repo.git("rev-list", "--count", f"{target}..{branch.oid}").stdout)
    detail = f"{branch.oid[:12]} -> {target[:12]} ({branch.upstream})"
    if dry_run:
        suffix = "; saves old tip before replacement" if ahead else ""
        return Result(branch.name, "would-mirror", detail + suffix)
    backup = _replace(repo, branch, target, save=ahead > 0, reason="mirror")
    return Result(branch.name, "mirrored", detail, backup)


def sync_mirror(
    repo: Repo, names: tuple[str, ...] = (), dry_run: bool = False
) -> tuple[Result, ...]:
    available = repo.branches(names, history=False, require_valid_upstreams=True)
    for branch in available:
        if branch.upstream:
            _require_direct(repo, branch)
    repo.ensure_no_operations()
    fetched = repo.fetch_all()
    selected = repo.branches(names, history=False, require_valid_upstreams=True)
    # A branch that changed while fetching must not be overwritten.
    before = {branch.ref: branch.oid for branch in available}
    for branch in selected:
        if before.get(branch.ref) != branch.oid:
            raise GitError(f"Branch changed during fetch: {branch.name}; inspect and retry")
    results: tuple[Result, ...] = ()
    current: Branch | None = None
    try:
        for branch in selected:
            current = branch
            results = (*results, _mirror_branch(repo, branch, fetched, dry_run))
            current = None
    except UpdateInterrupted as exc:
        raise UpdateInterrupted(
            str(exc), (*results, *exc.results), recovery_safe=exc.recovery_safe
        ) from exc
    except KeyboardInterrupt as exc:
        pending = _interrupted(repo, current, None).results if current is not None else ()
        raise UpdateInterrupted(
            "Mirror interrupted; inspect Git state before retrying.", (*results, *pending)
        ) from exc
    except GitError as exc:
        name = current.name if current is not None else "batch"
        raise UpdateError(
            f"Mirror stopped at {name}: {exc}", (*results, Result(name, "failed", str(exc)))
        ) from exc
    return results


def restore(
    repo: Repo, backup_ref: str, branch_name: str | None = None, dry_run: bool = False
) -> Result:
    saved = next((backup for backup in list_backups(repo) if backup.ref == backup_ref), None)
    if saved is None:
        raise GitError(
            "Specify an exact existing refs/gtracker/backups/... ref from `gtracker backups`"
        )
    branch = repo.branch(branch_name if branch_name is not None else saved.branch)
    _require_direct(repo, branch)
    repo.ensure_no_operations()
    _checked_tree(repo, branch, saved.oid)
    if branch.oid == saved.oid:
        return Result(branch.name, "unchanged", "already at saved tip")
    detail = f"{branch.oid[:12]} -> {saved.oid[:12]}"
    if dry_run:
        return Result(branch.name, "would-restore", detail)
    backup = _replace(repo, branch, saved.oid, save=True, reason="restore")
    return Result(branch.name, "restored", detail, backup)
