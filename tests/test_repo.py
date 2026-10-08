from pathlib import Path

import pytest
from conftest import Sandbox

from gtracker.repo import GitError, Repo, locked


def test_shared_directory_lock_covers_linked_worktrees(sandbox: Sandbox, tmp_path: Path) -> None:
    sandbox.git("worktree", "add", "-b", "linked", str(tmp_path / "linked"))
    original = Repo.open(sandbox.repo)
    linked = Repo.open(tmp_path / "linked")
    with locked(original), pytest.raises(GitError, match="Another gtracker"):
        with locked(linked):
            pytest.fail("second mutation acquired the shared lock")
    with locked(linked):
        assert linked.current_branch() == "refs/heads/linked"


def test_dirty_check_preserves_untracked_files(sandbox: Sandbox) -> None:
    scratch = sandbox.repo / "scratch.txt"
    scratch.write_text("keep me")
    with pytest.raises(GitError, match="untracked changes"):
        Repo.open(sandbox.repo).ensure_clean()
    assert scratch.read_text() == "keep me"


def test_ignored_collision_is_refused(sandbox: Sandbox) -> None:
    sandbox.commit(".gitignore", "artifact\n")
    (sandbox.repo / "artifact").write_text("my build output")
    sandbox.commit("artifact", "upstream tracked content", path=sandbox.remote)
    sandbox.git("fetch", "origin")
    with pytest.raises(GitError, match="ignored file"):
        Repo.open(sandbox.repo).ensure_clean(Repo.open(sandbox.repo).oid("origin/main"))
    assert (sandbox.repo / "artifact").read_text() == "my build output"


def test_operations_in_detached_linked_tree_block_updates(sandbox: Sandbox, tmp_path: Path) -> None:
    linked = tmp_path / "linked"
    sandbox.git("worktree", "add", "--detach", str(linked))
    repository = Repo.open(linked)
    repository.git_path("rebase-merge").mkdir()
    with pytest.raises(GitError, match="operation in progress"):
        Repo.open(sandbox.repo).ensure_no_operations()


def test_discovery_uses_exact_refs_even_for_ambiguous_branch_names(sandbox: Sandbox) -> None:
    sandbox.git("branch", "origin/main")
    branches = {branch.name: branch for branch in Repo.open(sandbox.repo).branches()}
    assert branches["main"].upstream == "refs/remotes/origin/main"
    assert branches["origin/main"].upstream == ""


def test_non_repository_and_bare_repository_rejected(sandbox: Sandbox, tmp_path: Path) -> None:
    with pytest.raises(GitError):
        Repo.open(tmp_path)
    bare = tmp_path / "bare"
    sandbox.git("init", "--bare", str(bare))
    with pytest.raises(GitError, match="non-bare"):
        Repo.open(bare)
