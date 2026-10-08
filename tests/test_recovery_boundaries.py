from pathlib import Path

import pytest
from conftest import Sandbox

from gtracker.mirror import restore
from gtracker.repo import GitError, Repo, locked


@pytest.mark.parametrize("object_ref", ["HEAD^{tree}", "HEAD:base.txt", "symbolic"])
def test_restore_rejects_non_direct_commit_backups(sandbox: Sandbox, object_ref: str) -> None:
    repo = Repo.open(sandbox.repo)
    original = sandbox.git("rev-parse", "HEAD").stdout.strip()
    backup = "refs/gtracker/backups/manual/main"
    if object_ref == "symbolic":
        sandbox.git("symbolic-ref", backup, "refs/heads/main")
    else:
        oid = sandbox.git("rev-parse", object_ref).stdout.strip()
        sandbox.git("update-ref", backup, oid)
    with pytest.raises(GitError):
        restore(repo, backup, dry_run=True)
    assert sandbox.git("rev-parse", "HEAD").stdout.strip() == original


def test_lock_refuses_symlink_instead_of_locking_unrelated_file(
    sandbox: Sandbox, tmp_path: Path
) -> None:
    repo = Repo.open(sandbox.repo)
    unrelated = tmp_path / "unrelated"
    unrelated.write_text("unchanged")
    (repo.common_dir() / "gtracker.lock").symlink_to(unrelated)
    with pytest.raises((GitError, OSError)):
        with locked(repo):
            pytest.fail("locked a symlink target")
    assert unrelated.read_text() == "unchanged"
