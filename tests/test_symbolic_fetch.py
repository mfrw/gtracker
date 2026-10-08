import pytest
from conftest import Sandbox

from gtracker.mirror import sync_mirror
from gtracker.repo import GitError, Repo


@pytest.mark.parametrize("destination", ["refs/remotes/origin/alias", "refs/tags/alias"])
def test_explicit_fetch_destination_cannot_dereference_into_personal_branch(
    sandbox: Sandbox,
    destination: str,
) -> None:
    sandbox.git("switch", "-c", "personal")
    personal = sandbox.commit("own.txt", "my work")
    sandbox.git("switch", "main")
    sandbox.git("symbolic-ref", destination, "refs/heads/personal")
    sandbox.git("config", "--add", "remote.origin.fetch", f"+refs/heads/main:{destination}")
    sandbox.commit("new.txt", "remote", path=sandbox.remote)
    with pytest.raises(GitError):
        sync_mirror(Repo.open(sandbox.repo), dry_run=True)
    assert sandbox.git("rev-parse", "refs/heads/personal").stdout.strip() == personal
