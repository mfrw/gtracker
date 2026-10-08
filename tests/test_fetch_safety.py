import pytest
from conftest import Sandbox

from gtracker.mirror import sync_mirror
from gtracker.repo import GitError, Repo


def test_fetch_mapping_cannot_overwrite_untracked_personal_branch(sandbox: Sandbox) -> None:
    sandbox.git("switch", "-c", "personal")
    personal = sandbox.commit("local.txt", "my local work")
    sandbox.git("switch", "main")
    sandbox.commit("new.txt", "remote work", path=sandbox.remote)
    sandbox.git("config", "--add", "remote.origin.fetch", "+refs/heads/main:refs/heads/personal")
    with pytest.raises(GitError):
        sync_mirror(Repo.open(sandbox.repo))
    assert sandbox.git("rev-parse", "refs/heads/personal").stdout.strip() == personal
