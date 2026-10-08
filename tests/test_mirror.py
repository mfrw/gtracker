from pathlib import Path

import pytest
from conftest import Sandbox

from gtracker.mirror import list_backups, restore, sync_mirror
from gtracker.repo import GitError, Repo


def test_rewritten_upstream_replaces_old_history_and_can_be_restored(sandbox: Sandbox) -> None:
    base = sandbox.git("rev-parse", "HEAD").stdout.strip()
    old = sandbox.commit("old.txt", "obsolete history\n", path=sandbox.remote)
    sandbox.git("pull", "--ff-only")
    sandbox.git("reset", "--hard", base, path=sandbox.remote)
    target = sandbox.commit("new.txt", "rewritten history\n", path=sandbox.remote)
    repo = Repo.open(sandbox.repo)
    results = sync_mirror(repo)
    assert sandbox.git("rev-parse", "HEAD").stdout.strip() == target
    assert not (sandbox.repo / "old.txt").exists()
    assert (sandbox.repo / "new.txt").read_text() == "rewritten history\n"
    saved = list_backups(repo)
    assert tuple(item.oid for item in saved) == (old,)
    assert results[0].backup == saved[0].ref
    restore(repo, saved[0].ref)
    assert sandbox.git("rev-parse", "HEAD").stdout.strip() == old
    assert (sandbox.repo / "old.txt").read_text() == "obsolete history\n"
    assert not (sandbox.repo / "new.txt").exists()
    assert target in tuple(item.oid for item in list_backups(repo))


def test_ahead_tracking_tip_is_backed_up_before_replacement(sandbox: Sandbox) -> None:
    upstream = sandbox.git("rev-parse", "origin/main").stdout.strip()
    ahead = sandbox.commit("accidental.txt", "local tracking commit")
    repo = Repo.open(sandbox.repo)
    sync_mirror(repo)
    assert sandbox.git("rev-parse", "HEAD").stdout.strip() == upstream
    assert tuple(backup.oid for backup in list_backups(repo)) == (ahead,)


def test_dirty_tracking_tree_is_skipped_without_backup_or_file_changes(sandbox: Sandbox) -> None:
    original = sandbox.git("rev-parse", "HEAD").stdout.strip()
    (sandbox.repo / "base.txt").write_text("uncommitted work")
    sandbox.commit("new.txt", "remote", path=sandbox.remote)
    repo = Repo.open(sandbox.repo)
    results = sync_mirror(repo)
    assert results[0].action == "skipped"
    assert sandbox.git("rev-parse", "HEAD").stdout.strip() == original
    assert (sandbox.repo / "base.txt").read_text() == "uncommitted work"
    assert list_backups(repo) == ()


def test_linked_tracking_worktree_updates_without_switching_main_checkout(
    sandbox: Sandbox, tmp_path: Path
) -> None:
    sandbox.git("switch", "-c", "personal")
    original = sandbox.git("rev-parse", "HEAD").stdout.strip()
    linked = tmp_path / "linked tree"
    sandbox.git("worktree", "add", str(linked), "main")
    target = sandbox.commit("new.txt", "remote", path=sandbox.remote)
    sync_mirror(Repo.open(sandbox.repo))
    assert sandbox.git("rev-parse", "HEAD").stdout.strip() == original
    assert sandbox.git("symbolic-ref", "HEAD").stdout.strip() == "refs/heads/personal"
    assert sandbox.git("rev-parse", "HEAD", path=linked).stdout.strip() == target
    assert (linked / "new.txt").read_text() == "remote"


def test_locked_linked_worktree_is_skipped(sandbox: Sandbox, tmp_path: Path) -> None:
    sandbox.git("switch", "-c", "personal")
    linked = tmp_path / "linked"
    sandbox.git("worktree", "add", str(linked), "main")
    sandbox.git("worktree", "lock", str(linked))
    original = sandbox.git("rev-parse", "main").stdout.strip()
    sandbox.commit("new.txt", "remote", path=sandbox.remote)
    results = sync_mirror(Repo.open(sandbox.repo))
    assert next(item for item in results if item.branch == "main").action == "skipped"
    assert sandbox.git("rev-parse", "main").stdout.strip() == original


def test_deleted_upstream_is_pruned_but_local_branch_is_preserved(sandbox: Sandbox) -> None:
    sandbox.git("branch", "topic", path=sandbox.remote)
    sandbox.git("fetch", "origin")
    sandbox.git("branch", "--track", "tracking/topic", "origin/topic")
    original = sandbox.git("rev-parse", "refs/heads/tracking/topic").stdout.strip()
    sandbox.git("branch", "-D", "topic", path=sandbox.remote)
    results = sync_mirror(Repo.open(sandbox.repo))
    assert (
        next(item for item in results if item.branch == "tracking/topic").action
        == "skipped-missing"
    )
    assert sandbox.git("rev-parse", "refs/heads/tracking/topic").stdout.strip() == original
    assert (
        sandbox.git("rev-parse", "--verify", "refs/remotes/origin/topic", check=False).returncode
        != 0
    )


def test_fetch_failure_prevents_any_local_branch_update(sandbox: Sandbox) -> None:
    original = sandbox.git("rev-parse", "HEAD").stdout.strip()
    sandbox.commit("new.txt", "remote", path=sandbox.remote)
    sandbox.git("remote", "add", "broken", "/does/not/exist")
    with pytest.raises(GitError):
        sync_mirror(Repo.open(sandbox.repo))
    assert sandbox.git("rev-parse", "HEAD").stdout.strip() == original
    assert not (sandbox.repo / "new.txt").exists()
    assert list_backups(Repo.open(sandbox.repo)) == ()


def test_backup_names_preserve_nested_branch_names_and_restore_checks_dirty_tree(
    sandbox: Sandbox,
) -> None:
    sandbox.git("branch", "-m", "main", "tracking/nested/main")
    old = sandbox.commit("local.txt", "old")
    repo = Repo.open(sandbox.repo)
    sync_mirror(repo)
    backup = list_backups(repo)[0]
    assert backup.branch == "tracking/nested/main"
    assert backup.oid == old
    (sandbox.repo / "base.txt").write_text("do not lose me")
    with pytest.raises(GitError):
        restore(repo, backup.ref)
    assert (sandbox.repo / "base.txt").read_text() == "do not lose me"


def test_restore_dry_run_keeps_tips_and_backups(sandbox: Sandbox) -> None:
    old = sandbox.commit("local.txt", "old")
    repo = Repo.open(sandbox.repo)
    sync_mirror(repo)
    saved = list_backups(repo)
    original = sandbox.git("rev-parse", "HEAD").stdout.strip()
    restore(repo, saved[0].ref, dry_run=True)
    assert sandbox.git("rev-parse", "HEAD").stdout.strip() == original
    assert list_backups(repo) == saved
    assert original != old


def test_symbolic_local_branch_cannot_overwrite_personal_branch(sandbox: Sandbox) -> None:
    sandbox.git("switch", "-c", "personal")
    original = sandbox.commit("own.txt", "my work")
    sandbox.git("symbolic-ref", "refs/heads/alias", "refs/heads/personal")
    sandbox.git("config", "branch.alias.remote", "origin")
    sandbox.git("config", "branch.alias.merge", "refs/heads/main")
    with pytest.raises(GitError):
        sync_mirror(Repo.open(sandbox.repo), ("alias",))
    assert sandbox.git("rev-parse", "refs/heads/personal").stdout.strip() == original
