import pytest
from conftest import Sandbox

from gtracker.rebase import pull_rebase, sync_rebase
from gtracker.repo import GitError, Repo


def test_orphan_root_replay_cannot_delete_ignored_private_contents(sandbox: Sandbox) -> None:
    sandbox.git("switch", "--orphan", "personal")
    sandbox.commit("private.txt", "root tracked content")
    sandbox.commit("own.txt", "local work")
    sandbox.git("rm", "private.txt")
    sandbox.git("commit", "-m", "remove private")
    (sandbox.repo / ".git" / "info" / "exclude").write_text("private.txt\n")
    (sandbox.repo / "private.txt").write_text("private ignored contents")
    original = sandbox.git("rev-parse", "HEAD").stdout.strip()
    with pytest.raises(GitError):
        pull_rebase(Repo.open(sandbox.repo), "origin/main")
    assert sandbox.git("rev-parse", "HEAD").stdout.strip() == original
    assert (sandbox.repo / "private.txt").read_text() == "private ignored contents"


@pytest.mark.parametrize("selected", [(), ("aa-topic", "main")])
def test_batch_updates_original_tracking_branch_after_switching_away(
    sandbox: Sandbox,
    selected: tuple[str, ...],
) -> None:
    sandbox.git("switch", "--no-track", "-c", "aa-topic")
    sandbox.git("config", "branch.aa-topic.remote", "origin")
    sandbox.git("config", "branch.aa-topic.merge", "refs/heads/main")
    sandbox.commit("own.txt", "local topic work")
    sandbox.git("switch", "main")
    target = sandbox.commit("upstream.txt", "remote work", path=sandbox.remote)
    sync_rebase(Repo.open(sandbox.repo), selected)
    assert sandbox.git("rev-parse", "refs/heads/main").stdout.strip() == target
    sandbox.git("merge-base", "--is-ancestor", target, "refs/heads/aa-topic")
    assert sandbox.git("symbolic-ref", "HEAD").stdout.strip() == "refs/heads/main"
    assert sandbox.git("show", "refs/heads/aa-topic:own.txt").stdout == "local topic work"


@pytest.mark.parametrize("operation", ["pull", "sync"])
def test_configured_squash_merge_cannot_leave_uncommitted_rebase_update(
    sandbox: Sandbox,
    operation: str,
) -> None:
    sandbox.git("config", "branch.main.mergeOptions", "--squash")
    target = sandbox.commit("upstream.txt", "remote work", path=sandbox.remote)
    repo = Repo.open(sandbox.repo)
    if operation == "pull":
        pull_rebase(repo)
    else:
        sync_rebase(repo)
    assert sandbox.git("rev-parse", "HEAD").stdout.strip() == target
    assert sandbox.git("status", "--porcelain").stdout == ""
    assert (sandbox.repo / "upstream.txt").read_text() == "remote work"
