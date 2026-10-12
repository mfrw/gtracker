import subprocess
from pathlib import Path

import pytest
from conftest import Sandbox

from gtracker.mirror import list_backups, sync_mirror
from gtracker.repo import GitError, Repo, UpdateInterrupted


def local_changes(sandbox: Sandbox) -> tuple[str, str, str]:
    return (
        sandbox.git("status", "--porcelain", "--untracked-files=all").stdout,
        sandbox.git("diff", "--cached", "--binary").stdout,
        sandbox.git("diff", "--binary").stdout,
    )


def edit_worktree(sandbox: Sandbox) -> None:
    (sandbox.repo / "base.txt").write_text("staged first\noriginal second\n")
    sandbox.git("add", "base.txt")
    (sandbox.repo / "base.txt").write_text("staged first\nunstaged second\n")
    (sandbox.repo / "local.bin").write_bytes(b"\0staged binary\n")
    sandbox.git("add", "local.bin")
    (sandbox.repo / "local.bin").write_bytes(b"\0unstaged binary\n")
    (sandbox.repo / "untracked.txt").write_text("untracked work\n")


@pytest.mark.parametrize("rewritten", [False, True])
@pytest.mark.parametrize("linked", [False, True])
def test_mirror_preserves_partial_staging_and_untracked_files_across_history_updates(
    sandbox: Sandbox, tmp_path: Path, rewritten: bool, linked: bool
) -> None:
    sandbox.commit("delete-me.txt", "stage its deletion\n", path=sandbox.remote)
    sandbox.git("pull", "--ff-only")
    if linked:
        sandbox.git("switch", "--no-track", "-c", "personal")
        tree = tmp_path / "linked worktree"
        sandbox.git("worktree", "add", str(tree), "main")
        (sandbox.repo / "caller.txt").write_text("leave the caller alone\n")
    else:
        tree = sandbox.repo
    working = Sandbox(sandbox.remote, tree)
    if rewritten:
        working.commit("old-history.txt", "replaced integration history\n")
    original = working.git("rev-parse", "HEAD").stdout.strip()
    config = working.git("config", "--get-regexp", r"^branch\.main\.").stdout
    edit_worktree(working)
    working.git("rm", "delete-me.txt")
    changes = local_changes(working)
    target = sandbox.commit("upstream.txt", "new upstream\n", path=sandbox.remote)

    result = sandbox.cli("sync")

    assert result.returncode == 0, result.stdout + result.stderr
    assert working.git("rev-parse", "HEAD").stdout.strip() == target
    assert working.git("symbolic-ref", "HEAD").stdout.strip() == "refs/heads/main"
    assert working.git("config", "--get-regexp", r"^branch\.main\.").stdout == config
    assert local_changes(working) == changes
    assert (tree / "base.txt").read_text() == "staged first\nunstaged second\n"
    assert (tree / "local.bin").read_bytes() == b"\0unstaged binary\n"
    assert (tree / "untracked.txt").read_text() == "untracked work\n"
    assert not (tree / "delete-me.txt").exists()
    assert (tree / "upstream.txt").read_text() == "new upstream\n"
    assert not (tree / "old-history.txt").exists()
    assert working.git("stash", "list").stdout == ""
    assert tuple(backup.oid for backup in list_backups(Repo.open(tree))) == (
        (original,) if rewritten else ()
    )
    if linked:
        assert sandbox.git("symbolic-ref", "HEAD").stdout.strip() == "refs/heads/personal"
        assert (sandbox.repo / "caller.txt").read_text() == "leave the caller alone\n"


def test_dirty_mirror_preview_preserves_index_files_and_refs(sandbox: Sandbox) -> None:
    original = sandbox.commit("old-history.txt", "obsolete commit\n")
    edit_worktree(sandbox)
    changes = local_changes(sandbox)
    sandbox.commit("upstream.txt", "new upstream\n", path=sandbox.remote)
    index = Repo.open(sandbox.repo).git_path("index")
    index_before = index.read_bytes()

    result = sandbox.cli("sync", "--dry-run")

    assert index.read_bytes() == index_before
    assert result.returncode == 0, result.stdout + result.stderr
    assert "main: would-mirror" in result.stdout
    assert sandbox.git("rev-parse", "HEAD").stdout.strip() == original
    assert local_changes(sandbox) == changes
    assert (sandbox.repo / "base.txt").read_text() == "staged first\nunstaged second\n"
    assert (sandbox.repo / "local.bin").read_bytes() == b"\0unstaged binary\n"
    assert (sandbox.repo / "untracked.txt").read_text() == "untracked work\n"
    assert not (sandbox.repo / "upstream.txt").exists()
    assert list_backups(Repo.open(sandbox.repo)) == ()


@pytest.mark.parametrize("state", ["unstaged", "staged", "untracked", "directory", "ignored"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_conflicting_local_paths_are_preserved_while_other_branches_can_update(
    sandbox: Sandbox, state: str, dry_run: bool
) -> None:
    original = sandbox.git("rev-parse", "HEAD").stdout.strip()
    sandbox.git("branch", "--track", "other", "origin/main")
    target_path = "base.txt" if state in {"unstaged", "staged"} else "new.txt"
    local_path = sandbox.repo / target_path
    if state == "directory":
        local_path.mkdir()
        local_path = local_path / "work.txt"
    local_path.write_text("local work must remain\n")
    if state == "staged":
        sandbox.git("add", target_path)
    if state == "ignored":
        (sandbox.repo / ".git/info/exclude").write_text("new.txt\n")
    changes = local_changes(sandbox)
    target = sandbox.commit(target_path, "conflicting upstream\n", path=sandbox.remote)

    rows = sync_mirror(Repo.open(sandbox.repo), dry_run=dry_run)

    assert next(row for row in rows if row.branch == "main").action == "skipped"
    assert sandbox.git("rev-parse", "refs/heads/main").stdout.strip() == original
    assert sandbox.git("rev-parse", "refs/heads/other").stdout.strip() == (
        original if dry_run else target
    )
    assert local_changes(sandbox) == changes
    assert local_path.read_text() == "local work must remain\n"
    assert list_backups(Repo.open(sandbox.repo)) == ()


@pytest.mark.parametrize("state", ["all-staged-deletions", "unstaged-deletion", "intent-to-add"])
def test_mirror_preserves_deletion_and_intent_to_add_index_states(
    sandbox: Sandbox, state: str
) -> None:
    if state == "all-staged-deletions":
        sandbox.git("rm", "base.txt")
    elif state == "unstaged-deletion":
        (sandbox.repo / "base.txt").unlink()
    else:
        (sandbox.repo / "intended.txt").write_text("intent to add\n")
        sandbox.git("add", "-N", "intended.txt")
    changes = local_changes(sandbox)
    target = sandbox.commit("new.txt", "upstream\n", path=sandbox.remote)

    rows = sync_mirror(Repo.open(sandbox.repo))

    assert rows[0].action == "mirrored"
    assert sandbox.git("rev-parse", "HEAD").stdout.strip() == target
    assert local_changes(sandbox) == changes
    if state == "intent-to-add":
        assert (sandbox.repo / "intended.txt").read_text() == "intent to add\n"
    else:
        assert not (sandbox.repo / "base.txt").exists()


@pytest.mark.parametrize(
    "kind", ["modified", "untracked", "commit", "staged-commit", "staged-deletion"]
)
def test_dirty_submodules_remain_protected_during_mirror(
    sandbox: Sandbox, tmp_path: Path, kind: str
) -> None:
    module_remote = tmp_path / "module-remote"
    sandbox.git("init", "-b", "main", str(module_remote))
    sandbox.commit("module.txt", "module base\n", path=module_remote)
    sandbox.git("submodule", "add", str(module_remote), "module", path=sandbox.remote)
    sandbox.git("commit", "-m", "add module", path=sandbox.remote)
    sandbox.git("pull", "--ff-only")
    sandbox.git("submodule", "update", "--init")
    module = sandbox.repo / "module"
    local = module / ("untracked.txt" if kind == "untracked" else "module.txt")
    local.write_text("local submodule work\n")
    if kind in {"commit", "staged-commit"}:
        sandbox.git("add", "module.txt", path=module)
        sandbox.git("commit", "-m", "local module commit", path=module)
    if kind == "staged-commit":
        sandbox.git("add", "module")
    elif kind == "staged-deletion":
        sandbox.git("update-index", "--force-remove", "module")
    original = sandbox.git("rev-parse", "HEAD").stdout.strip()
    module_head = sandbox.git("rev-parse", "HEAD", path=module).stdout.strip()
    sandbox.commit("upstream.txt", "root update\n", path=sandbox.remote)

    rows = sync_mirror(Repo.open(sandbox.repo))

    assert rows[0].action == "skipped"
    assert sandbox.git("rev-parse", "HEAD").stdout.strip() == original
    assert sandbox.git("rev-parse", "HEAD", path=module).stdout.strip() == module_head
    assert local.read_text() == "local submodule work\n"
    assert not (sandbox.repo / "upstream.txt").exists()


@pytest.mark.parametrize("name", ["index.lock", "HEAD.lock", "refs/heads/main.lock"])
@pytest.mark.parametrize("linked", [False, True])
def test_dirty_checkout_locks_block_updates_before_index_or_files_change(
    sandbox: Sandbox, tmp_path: Path, name: str, linked: bool
) -> None:
    working = sandbox
    if linked:
        sandbox.git("switch", "--no-track", "-c", "personal")
        tree = tmp_path / "linked"
        sandbox.git("worktree", "add", str(tree), "main")
        working = Sandbox(sandbox.remote, tree)
    edit_worktree(working)
    original = working.git("rev-parse", "HEAD").stdout.strip()
    changes = local_changes(working)
    sandbox.commit("upstream.txt", "new upstream\n", path=sandbox.remote)
    lock = Repo.open(working.repo).git_path(name)
    lock.write_text("existing Git lock\n")

    rows = sync_mirror(Repo.open(sandbox.repo))

    assert next(row for row in rows if row.branch == "main").action == "skipped"
    assert working.git("rev-parse", "HEAD").stdout.strip() == original
    assert local_changes(working) == changes
    assert (working.repo / "base.txt").read_text() == "staged first\nunstaged second\n"
    assert (working.repo / "local.bin").read_bytes() == b"\0unstaged binary\n"
    assert (working.repo / "untracked.txt").read_text() == "untracked work\n"
    assert not (working.repo / "upstream.txt").exists()
    assert lock.read_text() == "existing Git lock\n"
    assert list_backups(Repo.open(sandbox.repo)) == ()


@pytest.mark.parametrize("interrupted", [False, True])
def test_dirty_update_failure_or_interrupt_keeps_local_changes_and_old_tip_backup(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch, interrupted: bool
) -> None:
    original = sandbox.commit("old.txt", "old history\n")
    edit_worktree(sandbox)
    changes = local_changes(sandbox)
    target = sandbox.commit("new.txt", "new upstream\n", path=sandbox.remote)
    git = Repo.git

    def fault(
        repo: Repo,
        *args: str,
        check: bool = True,
        input: str | None = None,
        progress: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        if args[0] == "switch" and "-C" in args and not interrupted:
            (sandbox.repo / ".git/index.lock").write_text("another Git operation\n")
        result = git(repo, *args, check=check, input=input, progress=progress)
        if args[0] == "switch" and "-C" in args and interrupted:
            raise KeyboardInterrupt
        return result

    monkeypatch.setattr(Repo, "git", fault)

    with pytest.raises(UpdateInterrupted if interrupted else GitError) as caught:
        sync_mirror(Repo.open(sandbox.repo))

    backups = list_backups(Repo.open(sandbox.repo))
    assert tuple(backup.oid for backup in backups) == (original,)
    assert backups[0].ref in str(caught.value) or backups[0].ref in {
        row.backup for row in caught.value.results
    }
    assert sandbox.git("rev-parse", "HEAD").stdout.strip() == (target if interrupted else original)
    assert local_changes(sandbox) == changes
    assert (sandbox.repo / "base.txt").read_text() == "staged first\nunstaged second\n"
    assert (sandbox.repo / "local.bin").read_bytes() == b"\0unstaged binary\n"
    assert (sandbox.repo / "untracked.txt").read_text() == "untracked work\n"


def test_staged_rename_with_newline_filename_keeps_index_and_working_contents(
    sandbox: Sandbox,
) -> None:
    renamed = "1 renamed\nfile.txt"
    sandbox.git("mv", "base.txt", renamed)
    (sandbox.repo / renamed).write_text("unstaged renamed content\n")
    changes = local_changes(sandbox)
    target = sandbox.commit("upstream.txt", "new upstream\n", path=sandbox.remote)

    result = sandbox.cli("sync")

    assert result.returncode == 0, result.stdout + result.stderr
    assert sandbox.git("rev-parse", "HEAD").stdout.strip() == target
    assert local_changes(sandbox) == changes
    assert sandbox.git("show", f":{renamed}").stdout == "base\n"
    assert (sandbox.repo / renamed).read_text() == "unstaged renamed content\n"
    assert not (sandbox.repo / "base.txt").exists()


@pytest.mark.parametrize("replacement", ["deleted", "directory"])
def test_upstream_removal_or_directory_replacement_cannot_discard_dirty_file(
    sandbox: Sandbox, replacement: str
) -> None:
    original = sandbox.git("rev-parse", "HEAD").stdout.strip()
    (sandbox.repo / "base.txt").write_text("local file must stay\n")
    changes = local_changes(sandbox)
    sandbox.git("rm", "base.txt", path=sandbox.remote)
    if replacement == "directory":
        (sandbox.remote / "base.txt").mkdir()
        (sandbox.remote / "base.txt/child.txt").write_text("upstream child\n")
        sandbox.git("add", "base.txt/child.txt", path=sandbox.remote)
    sandbox.git("commit", "-m", "replace upstream path", path=sandbox.remote)

    rows = sync_mirror(Repo.open(sandbox.repo))

    assert rows[0].action == "skipped"
    assert sandbox.git("rev-parse", "HEAD").stdout.strip() == original
    assert local_changes(sandbox) == changes
    assert (sandbox.repo / "base.txt").read_text() == "local file must stay\n"
