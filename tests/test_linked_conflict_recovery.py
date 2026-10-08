from pathlib import Path

import pytest
from conftest import Sandbox

from gtracker.rebase import RebaseConflict, sync_rebase
from gtracker.repo import Repo


def test_linked_conflict_restores_unaffected_caller_after_earlier_update(
    sandbox: Sandbox,
    tmp_path: Path,
) -> None:
    sandbox.git("switch", "--no-track", "-c", "personal")
    personal = sandbox.commit("personal.txt", "my work")
    sandbox.git("switch", "--no-track", "-c", "first", "origin/main")
    sandbox.git("config", "branch.first.remote", "origin")
    sandbox.git("config", "branch.first.merge", "refs/heads/main")
    sandbox.commit("first.txt", "topic work")
    sandbox.git("switch", "personal")
    linked = tmp_path / "linked"
    sandbox.git("worktree", "add", "-b", "conflict", str(linked), "origin/main")
    sandbox.commit("base.txt", "linked local version", path=linked)
    target = sandbox.commit("base.txt", "remote version", path=sandbox.remote)
    with pytest.raises(RebaseConflict) as caught:
        sync_rebase(Repo.open(sandbox.repo), ("first", "conflict"))
    assert caught.value.path == linked
    assert sandbox.git("symbolic-ref", "HEAD").stdout.strip() == "refs/heads/personal"
    assert sandbox.git("rev-parse", "HEAD").stdout.strip() == personal
    assert Repo.open(linked).operation() == "rebase-merge"
    assert (
        sandbox.git("diff", "--name-only", "--diff-filter=U", path=linked).stdout.strip()
        == "base.txt"
    )
    sandbox.git("merge-base", "--is-ancestor", target, "refs/heads/first")
