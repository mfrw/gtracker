from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path

from .mirror import create_backup
from .repo import Branch, GitError, Repo, Result, UpdateError, UpdateInterrupted, Worktree


class RebaseConflict(GitError):
    """A stopped rebase whose checkout and recovery ref must remain intact."""

    def __init__(
        self,
        branch: str,
        path: Path,
        backup: str,
        detail: str,
        results: tuple[Result, ...] = (),
    ) -> None:
        self.branch = branch
        self.path = path
        self.backup = backup
        self.detail = detail
        self.results = results
        command = f"git -C {shlex.quote(str(path))} rebase"
        super().__init__(
            f"Rebase stopped on {branch} at {path}. Original tip: {backup}.\n"
            f"{detail}\nResolve manually, then {command} --continue; "
            f"or {command} --abort."
        )


@dataclass(frozen=True)
class _Upstream:
    remote: str
    remote_ref: str
    tracking_ref: str
    old_oid: str | None
    fork_point: str | None


@dataclass(frozen=True)
class _Plan:
    branch: Branch
    upstream: _Upstream


def _valid_ref(repo: Repo, ref: str, namespace: str) -> None:
    if not ref.startswith(namespace) or repo.git("check-ref-format", ref, check=False).returncode:
        raise GitError(f"Invalid upstream ref: {ref}")


def _remotes(repo: Repo) -> tuple[str, ...]:
    return tuple(repo.git("remote").stdout.splitlines())


def _validate_branch(repo: Repo, branch: Branch) -> None:
    _valid_ref(repo, branch.ref, "refs/heads/")
    if repo.git("symbolic-ref", "-q", branch.ref, check=False).returncode == 0:
        raise GitError(f"Refusing symbolic local branch: {branch.name}")
    _guard_tip(repo, branch)


def _fetch_specs(repo: Repo, remote: str) -> tuple[str, ...]:
    result = repo.git("config", "--get-all", f"remote.{remote}.fetch", check=False)
    return tuple(result.stdout.splitlines())


def _match_pattern(pattern: str, ref: str) -> str | None:
    if "*" not in pattern:
        return "" if pattern == ref else None
    prefix, suffix = pattern.split("*", 1)
    if not ref.startswith(prefix) or not ref.endswith(suffix):
        return None
    end = len(ref) - len(suffix) if suffix else len(ref)
    return ref[len(prefix) : end] if end >= len(prefix) else None


def _tracking_ref(repo: Repo, remote: str, remote_ref: str) -> str:
    destinations = []
    for spec in _fetch_specs(repo, remote):
        if spec.startswith("^"):
            if _match_pattern(spec[1:], remote_ref) is not None:
                raise GitError(
                    f"Upstream is excluded by remote {remote} fetch configuration: {remote_ref}"
                )
            continue
        source, separator, destination = spec.removeprefix("+").partition(":")
        match = _match_pattern(source, remote_ref)
        if separator and destination and match is not None:
            destinations.append(destination.replace("*", match))
    unique = tuple(dict.fromkeys(destinations))
    if len(unique) != 1:
        raise GitError(
            f"Upstream has no unambiguous tracking ref for remote {remote}: {remote_ref}"
        )
    _valid_ref(repo, unique[0], "refs/remotes/")
    return unique[0]


def _upstream(repo: Repo, branch: Branch, explicit: str | None = None) -> _Upstream:
    remotes = _remotes(repo)
    if explicit is not None:
        candidates = tuple(remote for remote in remotes if explicit.startswith(f"{remote}/"))
        if not candidates:
            raise GitError(f"Invalid upstream; expected a configured remote/branch: {explicit}")
        remote = max(candidates, key=len)
        remote_ref = f"refs/heads/{explicit[len(remote) + 1 :]}"
        _valid_ref(repo, remote_ref, "refs/heads/")
        tracking_ref = _tracking_ref(repo, remote, remote_ref)
        if repo.oid(tracking_ref) is None:
            raise GitError(f"Unknown upstream: {explicit}; fetch its tracking ref first")
    else:
        if branch.upstream_error:
            raise GitError(f"Invalid upstream for {branch.name}: {branch.upstream_error}")
        if not branch.upstream:
            raise GitError(f"Branch {branch.name} has no configured upstream")
        remote, remote_ref, tracking_ref = branch.remote, branch.remote_ref, branch.upstream
        merges = repo.git("config", "--get-all", f"branch.{branch.name}.merge", check=False)
        if tuple(merges.stdout.splitlines()) != (remote_ref,):
            raise GitError(f"Branch {branch.name} must have exactly one configured merge upstream")
        _valid_ref(repo, remote_ref, "refs/heads/")
        if remote == ".":
            _valid_ref(repo, tracking_ref, "refs/heads/")
            if tracking_ref != remote_ref:
                raise GitError(f"Invalid local upstream for {branch.name}")
        elif remote in remotes:
            if _tracking_ref(repo, remote, remote_ref) != tracking_ref:
                raise GitError(f"Inconsistent upstream configuration for {branch.name}")
        else:
            raise GitError(f"Unknown configured upstream remote for {branch.name}: {remote}")
    old_oid = repo.oid(tracking_ref)
    fork = (
        repo.git("merge-base", "--fork-point", tracking_ref, branch.oid, check=False)
        if old_oid
        else None
    )
    fork_point = fork.stdout.strip() if fork is not None and fork.returncode == 0 else None
    return _Upstream(remote, remote_ref, tracking_ref, old_oid, fork_point)


def _is_ancestor(repo: Repo, older: str, newer: str) -> bool:
    result = repo.git("merge-base", "--is-ancestor", older, newer, check=False)
    if result.returncode not in (0, 1):
        raise GitError(result.stderr.strip() or "Cannot determine commit ancestry")
    return result.returncode == 0


def _boundary(repo: Repo, upstream: _Upstream, target: str) -> str:
    # Match pull --rebase: discard the old integration history only when the
    # pre-fetch fork point is no longer contained in the fetched integration.
    fork = upstream.fork_point
    return fork if fork and not _is_ancestor(repo, fork, target) else target


def _protect_replay(repo: Repo, branch: Branch, boundary: str) -> None:
    if not repo.git("ls-files", "--others", "--ignored", "--exclude-standard", "-z").stdout:
        return
    commits = repo.git("rev-list", f"{boundary}..{branch.oid}", "--").stdout
    if not commits:
        return
    added = repo.git(
        "diff-tree",
        "--stdin",
        "--root",
        "-r",
        "-m",
        "--no-commit-id",
        "--name-only",
        "-z",
        "--no-renames",
        "--diff-filter=A",
        input=commits,
    ).stdout
    repo.protect_paths(tuple(filter(None, added.split("\0"))))


def _guard_tip(repo: Repo, branch: Branch) -> None:
    if repo.oid(branch.ref) != branch.oid:
        raise GitError(
            f"Branch changed during operation: {branch.name}; retry after Git activity stops"
        )


def _finish(repo: Repo, branch: Branch, backup: str) -> Result:
    if repo.oid(branch.ref) == branch.oid:
        return Result(branch.name, "unchanged", str(repo.path), backup)
    return Result(branch.name, "rebased", str(repo.path), backup)


def _interrupted(
    repo: Repo, branch: str | None, exc: KeyboardInterrupt, backup: str | None = None
) -> UpdateInterrupted:
    command = f"git -C {shlex.quote(str(repo.path))}"
    detail = (
        f"Interrupted at worktree {repo.path}. No operation was aborted or changes discarded. "
        f"Inspect manually with {command} status; for an active rebase, resolve and run "
        f"{command} rebase --continue, or manually choose {command} rebase --abort."
    )
    if backup:
        detail += f" Original tip backup: {backup}."
    results = exc.results if isinstance(exc, UpdateInterrupted) else ()
    if branch and not any(row.branch == branch for row in results):
        results = (*results, Result(branch, "interrupted", str(repo.path), backup))
    message = f"{exc}\n{detail}" if str(exc) else detail
    safe = exc.recovery_safe if isinstance(exc, UpdateInterrupted) else True
    return UpdateInterrupted(message, results, recovery_safe=safe)


def _integrate(repo: Repo, plan: _Plan, target: str, dry_run: bool) -> Result:
    branch = plan.branch
    backup: str | None = None
    try:
        _guard_tip(repo, branch)
        boundary = _boundary(repo, plan.upstream, target)
        if boundary == target and _is_ancestor(repo, target, branch.oid):
            return Result(branch.name, "unchanged")
        _protect_replay(repo, branch, boundary)
        if dry_run:
            return Result(branch.name, "would-rebase", f"onto {target} at {repo.path}")
        if repo.current_branch() != branch.ref:
            raise GitError(f"Expected {branch.name} checked out at {repo.path}")
        repo.ensure_clean(target)
        backup = create_backup(repo, branch, "rebase")
        if _is_ancestor(repo, branch.oid, target):
            result = repo.git(
                "merge",
                "--ff-only",
                "--no-squash",
                "--no-autostash",
                "--no-edit",
                target,
                check=False,
            )
        else:
            result = repo.git(
                "-c",
                "rebase.updateRefs=false",
                "rebase",
                "--no-autostash",
                "--no-fork-point",
                "--onto",
                target,
                boundary,
                check=False,
            )
        if result.returncode:
            detail = result.stderr.strip() or result.stdout.strip() or "Git rebase failed"
            if repo.operation() in ("rebase-merge", "rebase-apply"):
                raise RebaseConflict(branch.name, repo.path, backup, detail)
            raise GitError(
                f"Cannot rebase {branch.name} at {repo.path}: {detail}; original tip: {backup}"
            )
        return _finish(repo, branch, backup)
    except KeyboardInterrupt as exc:
        raise _interrupted(repo, branch.name, exc, backup) from exc
    except RebaseConflict:
        raise
    except GitError as exc:
        raise UpdateError(
            str(exc), (Result(branch.name, "failed", str(repo.path), backup),)
        ) from exc


def pull_rebase(repo: Repo, upstream: str | None = None, dry_run: bool = False) -> Result:
    """Rebase the current branch without changing its tracking configuration."""
    name: str | None = None
    try:
        current = repo.current_branch()
        if current is None:
            raise GitError("Pull with rebase requires a current local branch, not detached HEAD")
        name = current.removeprefix("refs/heads/")
        branch = repo.branches((name,), history=False, require_valid_upstreams=upstream is None)[0]
        _validate_branch(repo, branch)
        source = _upstream(repo, branch, upstream)
        if source.old_oid is None:
            raise GitError(f"Missing upstream for {branch.name}: {source.tracking_ref}")
        repo.ensure_safe_fetch()
        repo.ensure_no_operations()
        repo.ensure_clean()
        if any(tree.path == repo.path and tree.locked for tree in repo.worktrees()):
            raise GitError(f"Worktree is locked: {repo.path}")
        # Capture the replay boundary above, before Git updates the tracking ref.
        # Pin FETCH_HEAD so a remote rewrite cannot change the checked target again.
        repo.git(
            "fetch",
            "--prune",
            "--progress",
            "--",
            source.remote,
            source.remote_ref,
            progress=True,
        )
        target = repo.oid("FETCH_HEAD")
        if target is None:
            raise GitError(f"No fetched upstream commit for {branch.name}")
        repo.ensure_clean(target)
        return _integrate(repo, _Plan(branch, source), target, dry_run)
    except UpdateInterrupted:
        raise
    except KeyboardInterrupt as exc:
        raise _interrupted(repo, name, exc) from exc
    except (RebaseConflict, UpdateError):
        raise
    except GitError as exc:
        if name is None:
            raise
        raise UpdateError(str(exc), (Result(name, "failed", str(exc)),)) from exc


def _select(repo: Repo, names: tuple[str, ...]) -> tuple[_Plan, ...]:
    branches = repo.branches(names, history=False, require_valid_upstreams=True)
    selected = branches if names else tuple(branch for branch in branches if branch.upstream)
    for branch in selected:
        _validate_branch(repo, branch)
    return tuple(_Plan(branch, _upstream(repo, branch)) for branch in selected)


def _checkout_repo(
    repo: Repo,
    plan: _Plan,
    target: str,
    trees: tuple[Worktree, ...],
    dry_run: bool,
) -> Repo | Result:
    branch = plan.branch
    matching = tuple(tree for tree in trees if tree.branch == branch.ref)
    if len(matching) > 1:
        raise GitError(f"Branch is checked out in multiple worktrees: {branch.name}")
    tree = matching[0] if matching else None
    calling = next((item for item in trees if item.path == repo.path), None)
    locked_tree = tree if tree else calling
    if locked_tree and locked_tree.locked:
        return Result(branch.name, "skipped", f"locked worktree: {locked_tree.path}")
    working = Repo(tree.path) if tree else repo
    try:
        working.ensure_clean(target)
        if not tree:
            working.ensure_clean(branch.oid)
        _protect_replay(working, branch, _boundary(working, plan.upstream, target))
    except GitError as exc:
        return Result(branch.name, "skipped", f"dirty or unsafe worktree at {working.path}: {exc}")
    except KeyboardInterrupt as exc:
        raise _interrupted(working, branch.name, exc) from exc
    if not tree and not dry_run:
        try:
            working.git("switch", "--no-overwrite-ignore", "--no-guess", "--", branch.name)
        except KeyboardInterrupt as exc:
            raise _interrupted(working, branch.name, exc) from exc
    return working


def _restore_checkout(repo: Repo, original: str | None, original_oid: str) -> None:
    # Only the caller matters here: an active linked rebase must remain intact
    # without preventing restoration of this otherwise unaffected worktree.
    operation = repo.operation()
    if operation:
        raise GitError(f"Worktree {repo.path} retains active {operation}; recover manually")
    if repo.current_branch() == original and (
        original is not None or repo.oid("HEAD") == original_oid
    ):
        return
    target = repo.oid(original) if original else original_oid
    if target is None:
        raise GitError(f"Original checkout is unavailable: {original}")
    repo.ensure_clean(target)
    if original:
        repo.git(
            "switch",
            "--no-overwrite-ignore",
            "--no-guess",
            "--",
            original.removeprefix("refs/heads/"),
        )
    else:
        repo.git("switch", "--no-overwrite-ignore", "--detach", original_oid)


def _restore_note(repo: Repo, original: str | None, original_oid: str) -> str:
    try:
        _restore_checkout(repo, original, original_oid)
    except GitError as exc:
        return f"\nCannot restore caller checkout at {repo.path}; recover manually: {exc}"
    return ""


def sync_rebase(
    repo: Repo, names: tuple[str, ...] = (), dry_run: bool = False
) -> tuple[Result, ...]:
    """Fetch all remotes, then rebase tracking branches in selection order."""
    plans = _select(repo, names)
    repo.ensure_no_operations()
    original, original_oid = repo.current_branch(), repo.oid("HEAD")
    if original_oid is None:
        raise GitError("Sync with rebase requires an existing checkout commit")
    results: tuple[Result, ...] = ()
    plan: _Plan | None = None
    try:
        try:
            fetched = repo.fetch_all()
            for plan in plans:
                _guard_tip(repo, plan.branch)
                source = plan.upstream
                target = (
                    repo.oid(source.tracking_ref)
                    if source.remote == "."
                    else fetched.oid(source.remote, source.tracking_ref)
                )
                if target is None:
                    cached = repo.oid(source.tracking_ref)
                    action = "skipped-unfetched" if cached else "skipped-missing"
                    reason = "upstream not fetched" if cached else "missing upstream"
                    results = (
                        *results,
                        Result(plan.branch.name, action, f"{reason}: {source.tracking_ref}"),
                    )
                    continue
                working = _checkout_repo(repo, plan, target, repo.worktrees(), dry_run)
                if isinstance(working, Result):
                    results = (*results, working)
                    continue
                results = (*results, _integrate(working, plan, target, dry_run))
        except RebaseConflict as exc:
            current = exc.results or (Result(exc.branch, "conflict", str(exc.path), exc.backup),)
            results = (*results, *current)
            plan = None
            detail = exc.detail
            if exc.path.resolve() != repo.path.resolve() and not dry_run:
                detail += _restore_note(repo, original, original_oid)
            raise RebaseConflict(exc.branch, exc.path, exc.backup, detail, results) from exc
        except GitError as exc:
            current = exc.results if isinstance(exc, UpdateError) else ()
            if plan is not None and not current:
                current = (Result(plan.branch.name, "failed", str(exc)),)
            results = (*results, *current)
            plan = None
            recovery = "" if dry_run else _restore_note(repo, original, original_oid)
            raise UpdateError(f"{exc}{recovery}", results) from exc
        plan = None
        if not dry_run:
            try:
                _restore_checkout(repo, original, original_oid)
            except GitError as exc:
                raise UpdateError(f"Cannot restore initial checkout: {exc}", results) from exc
    except KeyboardInterrupt as exc:
        branch = plan.branch.name if plan else None
        if any(row.branch == branch for row in results):
            branch = None
        interrupted = exc if isinstance(exc, UpdateInterrupted) else _interrupted(repo, branch, exc)
        recovery = ""
        safe = interrupted.recovery_safe
        if not dry_run and safe:
            try:
                recovery = _restore_note(repo, original, original_oid)
            except KeyboardInterrupt as recovery_error:
                recovery = (
                    f"\nCaller recovery interrupted at {repo.path}; inspect manually."
                    f"\n{recovery_error}"
                )
                if isinstance(recovery_error, UpdateInterrupted):
                    safe = safe and recovery_error.recovery_safe
        raise UpdateInterrupted(
            f"{interrupted}{recovery}",
            (*results, *interrupted.results),
            recovery_safe=safe,
        ) from exc
    return results
