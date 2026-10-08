import pytest
from conftest import Sandbox

from gtracker.mirror import list_backups, restore, sync_mirror
from gtracker.repo import GitError, Repo


@pytest.mark.parametrize("operation", ["mirror", "restore"])
def test_failed_checked_out_reset_reports_the_surviving_backup(
    sandbox: Sandbox,
    operation: str,
) -> None:
    sandbox.commit("own.txt", "tracking-local work")
    repo = Repo.open(sandbox.repo)
    if operation == "restore":
        sync_mirror(repo)
        saved = list_backups(repo)[0]
    before = frozenset(backup.ref for backup in list_backups(repo))
    original = sandbox.git("rev-parse", "HEAD").stdout.strip()
    (sandbox.repo / ".git" / "index.lock").write_text("existing Git lock")
    with pytest.raises(GitError) as caught:
        if operation == "mirror":
            sync_mirror(repo)
        else:
            restore(repo, saved.ref)
    remaining = tuple(backup for backup in list_backups(repo) if backup.ref not in before)
    assert len(remaining) == 1
    assert remaining[0].ref in str(caught.value)
    assert remaining[0].oid == original
    assert sandbox.git("rev-parse", "HEAD").stdout.strip() == original
