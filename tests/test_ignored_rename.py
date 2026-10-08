from conftest import Sandbox

from gtracker.mirror import list_backups, sync_mirror
from gtracker.repo import Repo


def test_renaming_tracked_file_cannot_overwrite_ignored_artifact(sandbox: Sandbox) -> None:
    original = sandbox.git("rev-parse", "HEAD").stdout.strip()
    (sandbox.repo / ".git" / "info" / "exclude").write_text("artifact.txt\n")
    (sandbox.repo / "artifact.txt").write_text("private ignored contents")
    sandbox.git("mv", "base.txt", "artifact.txt", path=sandbox.remote)
    sandbox.git("commit", "-m", "rename", path=sandbox.remote)
    repo = Repo.open(sandbox.repo)
    results = sync_mirror(repo)
    assert results[0].action == "skipped"
    assert sandbox.git("rev-parse", "HEAD").stdout.strip() == original
    assert (sandbox.repo / "artifact.txt").read_text() == "private ignored contents"
    assert list_backups(repo) == ()
