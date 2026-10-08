from conftest import Sandbox

from gtracker.mirror import sync_mirror
from gtracker.repo import Repo


def test_symbolic_orig_head_cannot_overwrite_personal_branch(sandbox: Sandbox) -> None:
    sandbox.git("switch", "-c", "personal")
    personal = sandbox.commit("local.txt", "my work")
    sandbox.git("switch", "main")
    original = sandbox.git("rev-parse", "HEAD").stdout.strip()
    sandbox.git("symbolic-ref", "ORIG_HEAD", "refs/heads/personal")
    sandbox.commit("new.txt", "remote", path=sandbox.remote)
    results = sync_mirror(Repo.open(sandbox.repo))
    assert results[0].action == "skipped"
    assert sandbox.git("rev-parse", "refs/heads/personal").stdout.strip() == personal
    assert sandbox.git("rev-parse", "HEAD").stdout.strip() == original
