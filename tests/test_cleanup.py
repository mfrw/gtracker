import subprocess
from pathlib import Path

import pytest
from conftest import Sandbox

from gtracker import cli, mirror
from gtracker.mirror import BACKUP_PREFIX, Backup, list_backups
from gtracker.repo import GitError, Repo, locked


def save_backup(sandbox: Sandbox, stamp: str, branch: str = "main", oid: str = "HEAD") -> str:
    ref = f"{BACKUP_PREFIX}{stamp}-{'0' * 32}/{branch}"
    sandbox.git("update-ref", ref, oid)
    return ref


def all_refs(sandbox: Sandbox) -> str:
    return sandbox.git("for-each-ref", "--format=%(refname) %(objectname) %(symref)").stdout


def test_retention_orders_microseconds_and_keeps_each_branch_separately() -> None:
    older = Backup(f"{BACKUP_PREFIX}20251231T235959.999999Z-a/main", "main", "a" * 40)
    newer = Backup(f"{BACKUP_PREFIX}20260101T000000.000001Z-b/main", "main", "b" * 40)
    newest = Backup(f"{BACKUP_PREFIX}20260101T000000.000002Z-c/main", "main", "c" * 40)
    nested = Backup(f"{BACKUP_PREFIX}20250101T000000.000000Z-d/main/topic", "main/topic", "d" * 40)

    candidates = mirror._older_backups((newest, older, nested, newer), keep=2)

    assert candidates == (older,)


def test_cleanup_retains_by_backup_time_not_commit_date_and_kept_refs_restore(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = sandbox.git("rev-parse", "HEAD").stdout.strip()
    monkeypatch.setenv("GIT_AUTHOR_DATE", "2000-01-01T00:00:00+00:00")
    monkeypatch.setenv("GIT_COMMITTER_DATE", "2000-01-01T00:00:00+00:00")
    latest = sandbox.commit("local.txt", "local history\n")
    old_main = save_backup(sandbox, "20260101T000000.000000Z", oid=original)
    new_main = save_backup(sandbox, "20260102T000000.000000Z", oid=latest)
    old_topic = save_backup(sandbox, "20260101T000000.000000Z", "topic/sub", latest)
    new_topic = save_backup(sandbox, "20260103T000000.000000Z", "topic/sub", original)
    before = all_refs(sandbox)

    preview = sandbox.cli("cleanup", "--keep", "1", "--dry-run")

    assert preview.returncode == 0, preview.stderr
    assert old_main in preview.stdout and old_topic in preview.stdout
    assert new_main not in preview.stdout and new_topic not in preview.stdout
    assert all_refs(sandbox) == before

    applied = sandbox.cli("cleanup", "--keep", "1", "--yes")

    assert applied.returncode == 0, applied.stderr
    assert {backup.ref for backup in list_backups(Repo.open(sandbox.repo))} == {
        new_main,
        new_topic,
    }
    assert sandbox.git("rev-parse", "HEAD").stdout.strip() == latest
    restored = sandbox.cli("restore", new_topic, "--branch", "main")
    assert restored.returncode == 0, restored.stderr
    assert sandbox.git("rev-parse", "HEAD").stdout.strip() == original
    assert not (sandbox.repo / "local.txt").exists()


def test_cleanup_all_filters_exact_deleted_branch_names_and_preserves_dirty_worktree(
    sandbox: Sandbox,
) -> None:
    selected = save_backup(sandbox, "20260101T000000.000000Z", "deleted/topic")
    untouched = save_backup(sandbox, "20260102T000000.000000Z", "deleted/topic/sub")
    sandbox.git("tag", "keep-tag")
    sandbox.git("update-ref", "refs/gtracker/other/keep", "HEAD")
    sandbox.git("update-ref", "refs/gtracker/backups-other/keep", "HEAD")
    sandbox.git("remote", "set-url", "origin", "/does/not/exist")
    (sandbox.repo / "base.txt").write_text("staged changes\n")
    sandbox.git("add", "base.txt")
    (sandbox.repo / "base.txt").write_text("unstaged changes\n")
    (sandbox.repo / "untracked.txt").write_text("keep this too\n")
    before = all_refs(sandbox)
    status = sandbox.git("status", "--porcelain").stdout
    index = sandbox.git("show", ":base.txt").stdout
    sandbox.git("pack-refs", "--all")

    result = sandbox.cli(
        "cleanup", "--all", "--yes", "--branch", "deleted/topic", "--branch", "deleted/topic"
    )

    assert result.returncode == 0, result.stderr
    assert all_refs(sandbox) == "".join(
        line for line in before.splitlines(keepends=True) if not line.startswith(selected + " ")
    )
    assert tuple(backup.ref for backup in list_backups(Repo.open(sandbox.repo))) == (untouched,)
    assert sandbox.git("status", "--porcelain").stdout == status
    assert sandbox.git("show", ":base.txt").stdout == index
    assert (sandbox.repo / "base.txt").read_text() == "unstaged changes\n"
    assert (sandbox.repo / "untracked.txt").read_text() == "keep this too\n"


@pytest.mark.parametrize(
    "args",
    [
        (),
        ("--all",),
        ("--yes",),
        ("--keep", "1", "--all", "--yes"),
        ("--all", "--yes", "--dry-run"),
        ("--keep", "0", "--yes"),
        ("--keep", "-1", "--yes"),
        ("--keep", "invalid", "--yes"),
    ],
)
def test_cleanup_requires_explicit_valid_policy_and_deletion_mode(
    sandbox: Sandbox, args: tuple[str, ...]
) -> None:
    save_backup(sandbox, "20260101T000000.000000Z")
    before = all_refs(sandbox)

    result = sandbox.cli("cleanup", *args)

    assert result.returncode == 2
    assert all_refs(sandbox) == before


def test_cleanup_unknown_branch_aborts_before_deleting_valid_selection(sandbox: Sandbox) -> None:
    save_backup(sandbox, "20260101T000000.000000Z")
    before = all_refs(sandbox)

    result = sandbox.cli("cleanup", "--all", "--yes", "--branch", "main", "--branch", "typo")

    assert result.returncode == 1
    assert "typo" in result.stderr
    assert all_refs(sandbox) == before


def test_keep_filters_each_exact_branch_before_interpreting_unselected_backup_ages(
    sandbox: Sandbox,
) -> None:
    first = save_backup(sandbox, "20260101T000000.000000Z", "deleted/topic")
    save_backup(sandbox, "20260102T000000.000000Z", "deleted/topic")
    second = save_backup(sandbox, "20260101T000000.000000Z", "other")
    save_backup(sandbox, "20260102T000000.000000Z", "other")
    save_backup(sandbox, "20260103T000000.000000Z", "deleted/topic/sub")
    sandbox.git("update-ref", f"{BACKUP_PREFIX}manual/unselected", "HEAD")
    before = all_refs(sandbox)

    result = sandbox.cli(
        "cleanup", "--keep", "1", "--yes", "--branch", "deleted/topic", "--branch", "other"
    )

    assert result.returncode == 0, result.stderr
    assert all_refs(sandbox) == "".join(
        line
        for line in before.splitlines(keepends=True)
        if line.split(" ", 1)[0] not in {first, second}
    )


@pytest.mark.parametrize("generation", ["manual", "20260230T000000.000000Z-invalid"])
def test_keep_rejects_unknown_backup_age_but_all_can_explicitly_remove_it(
    sandbox: Sandbox, generation: str
) -> None:
    invalid = f"{BACKUP_PREFIX}{generation}/main"
    sandbox.git("update-ref", invalid, "HEAD")
    save_backup(sandbox, "20260101T000000.000000Z")
    before = all_refs(sandbox)

    refused = sandbox.cli("cleanup", "--keep", "1", "--yes")

    assert refused.returncode == 1
    assert invalid in refused.stderr
    assert all_refs(sandbox) == before
    deleted = sandbox.cli("cleanup", "--all", "--yes")
    assert deleted.returncode == 0, deleted.stderr
    assert list_backups(Repo.open(sandbox.repo)) == ()


@pytest.mark.parametrize("kind", ["symbolic", "blob", "malformed"])
def test_cleanup_rejects_invalid_backups_without_partial_deletion(
    sandbox: Sandbox, kind: str
) -> None:
    save_backup(sandbox, "20260101T000000.000000Z")
    invalid = f"{BACKUP_PREFIX}invalid/main"
    if kind == "symbolic":
        sandbox.git("symbolic-ref", invalid, "refs/heads/main")
    elif kind == "blob":
        sandbox.git("update-ref", invalid, sandbox.git("rev-parse", "HEAD:base.txt").stdout.strip())
    else:
        sandbox.git("update-ref", f"{BACKUP_PREFIX}malformed", "HEAD")
    before = all_refs(sandbox)

    result = sandbox.cli("cleanup", "--all", "--yes")

    assert result.returncode == 1
    assert all_refs(sandbox) == before


@pytest.mark.parametrize(
    ("linked", "reference"),
    [
        (False, "HEAD"),
        (False, "refs/heads/alias"),
        (False, "ORIG_HEAD"),
        (False, "MERGE_AUTOSTASH"),
        (False, "BISECT_HEAD"),
        (True, "HEAD"),
        (True, "REBASE_HEAD"),
        (True, "MERGE_AUTOSTASH"),
        (True, "AUTO_MERGE"),
        (True, "refs/worktree/alias"),
        (True, "refs/bisect/alias"),
        (True, "refs/rewritten/alias"),
    ],
)
def test_cleanup_refuses_backups_used_by_symbolic_refs_or_worktree_heads(
    sandbox: Sandbox, tmp_path: Path, linked: bool, reference: str
) -> None:
    saved = save_backup(sandbox, "20260101T000000.000000Z")
    original = sandbox.git("rev-parse", "HEAD").stdout.strip()
    tree = tmp_path / "linked" if linked else sandbox.repo
    if linked:
        sandbox.git("worktree", "add", "--detach", str(tree))
    sandbox.git("symbolic-ref", reference, saved, path=tree)
    before = all_refs(sandbox)

    result = sandbox.cli("cleanup", "--all", "--yes")

    assert result.returncode == 1
    assert all_refs(sandbox) == before
    assert sandbox.git("rev-parse", "HEAD").stdout.strip() == original
    assert sandbox.git("symbolic-ref", reference, path=tree).stdout.strip() == saved
    assert sandbox.git("rev-parse", reference, path=tree).stdout.strip() == original


def test_cleanup_compare_and_swap_failure_preserves_entire_batch(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = save_backup(sandbox, "20260101T000000.000000Z")
    second = save_backup(sandbox, "20260102T000000.000000Z")
    changed_oid = sandbox.commit("new.txt", "new tip\n")
    repo = Repo.open(sandbox.repo)
    snapshot = list_backups(repo)
    sandbox.git("update-ref", second, changed_oid)
    before = all_refs(sandbox)
    monkeypatch.setattr(mirror, "list_backups", lambda repo: snapshot)

    with pytest.raises(GitError):
        mirror.cleanup_backups(repo, keep=None)

    assert all_refs(sandbox) == before
    assert sandbox.git("rev-parse", first).stdout.strip() == snapshot[0].oid


def test_symbolic_backup_race_cannot_delete_the_referenced_local_branch(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = save_backup(sandbox, "20260101T000000.000000Z")
    second = save_backup(sandbox, "20260102T000000.000000Z")
    original = sandbox.git("rev-parse", "refs/heads/main").stdout.strip()
    git = Repo.git

    def race(
        repo: Repo,
        *args: str,
        check: bool = True,
        input: str | None = None,
        progress: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        if args[0] == "update-ref":
            sandbox.git("symbolic-ref", second, "refs/heads/main")
        return git(repo, *args, check=check, input=input, progress=progress)

    monkeypatch.setattr(Repo, "git", race)

    result = cli.main(["cleanup", "--all", "--yes", "--repo", str(sandbox.repo)])

    assert result in (0, 1)
    assert sandbox.git("rev-parse", "refs/heads/main").stdout.strip() == original
    refs = sandbox.git("for-each-ref", "--format=%(refname)", BACKUP_PREFIX).stdout.splitlines()
    assert set(refs) == (set() if result == 0 else {first, second})


def test_cleanup_lock_failure_preserves_entire_batch(sandbox: Sandbox) -> None:
    save_backup(sandbox, "20260101T000000.000000Z")
    blocked = save_backup(sandbox, "20260102T000000.000000Z")
    repo = Repo.open(sandbox.repo)
    repo.git_path(blocked + ".lock").touch()
    before = all_refs(sandbox)

    result = sandbox.cli("cleanup", "--all", "--yes")

    assert result.returncode == 1
    assert all_refs(sandbox) == before


def test_cleanup_obeys_shared_lock_from_a_linked_worktree(sandbox: Sandbox, tmp_path: Path) -> None:
    save_backup(sandbox, "20260101T000000.000000Z")
    tree = tmp_path / "linked"
    sandbox.git("worktree", "add", "--detach", str(tree))
    before = all_refs(sandbox)

    with locked(Repo.open(sandbox.repo)):
        result = Sandbox(sandbox.remote, tree).cli("cleanup", "--all", "--yes")

    assert result.returncode == 1
    assert all_refs(sandbox) == before


def test_cleanup_preserves_backups_during_an_active_linked_operation(
    sandbox: Sandbox, tmp_path: Path
) -> None:
    save_backup(sandbox, "20260101T000000.000000Z")
    tree = tmp_path / "linked"
    sandbox.git("worktree", "add", "--detach", str(tree))
    Repo.open(tree).git_path("rebase-merge").mkdir()
    before = all_refs(sandbox)

    result = sandbox.cli("cleanup", "--all", "--yes")

    assert result.returncode == 1
    assert all_refs(sandbox) == before
    assert Repo.open(tree).git_path("rebase-merge").is_dir()


def test_empty_cleanup_and_retention_above_count_are_safe_noops(sandbox: Sandbox) -> None:
    empty = sandbox.cli("cleanup", "--all", "--yes")
    assert empty.returncode == 0, empty.stderr
    save_backup(sandbox, "20260101T000000.000000Z")
    before = all_refs(sandbox)

    retained = sandbox.cli("cleanup", "--keep", "999999999999999999999", "--yes")

    assert retained.returncode == 0, retained.stderr
    assert all_refs(sandbox) == before
