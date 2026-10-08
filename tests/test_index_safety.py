import pytest
from conftest import Sandbox

from gtracker.mirror import sync_mirror
from gtracker.repo import Repo


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_hidden_index_flags_cannot_discard_local_work(sandbox: Sandbox, flag: str) -> None:
    original = sandbox.git("rev-parse", "HEAD").stdout.strip()
    sandbox.git("update-index", flag, "base.txt")
    (sandbox.repo / "base.txt").write_text("my hidden local work")
    sandbox.commit("base.txt", "remote replacement", path=sandbox.remote)
    result = sync_mirror(Repo.open(sandbox.repo))
    assert result[0].action == "skipped"
    assert sandbox.git("rev-parse", "HEAD").stdout.strip() == original
    assert (sandbox.repo / "base.txt").read_text() == "my hidden local work"
