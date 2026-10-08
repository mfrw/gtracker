from conftest import Sandbox


def test_status_is_offline_and_does_not_fetch(sandbox: Sandbox) -> None:
    original = sandbox.git("rev-parse", "origin/main").stdout.strip()
    sandbox.commit("new.txt", "remote changes", path=sandbox.remote)
    sandbox.git("remote", "set-url", "origin", "/does/not/exist")
    result = sandbox.cli("status")
    assert result.returncode == 0, result.stderr
    assert sandbox.git("rev-parse", "origin/main").stdout.strip() == original
    assert sandbox.git("rev-parse", "main").stdout.strip() == original


def test_unknown_selection_fails_before_fetch_or_ref_mutation(sandbox: Sandbox) -> None:
    original = sandbox.git("rev-parse", "origin/main").stdout.strip()
    sandbox.commit("new.txt", "remote changes", path=sandbox.remote)
    result = sandbox.cli("sync", "--branch", "absent")
    assert result.returncode != 0
    assert sandbox.git("rev-parse", "origin/main").stdout.strip() == original
    assert sandbox.git("rev-parse", "main").stdout.strip() == original


def test_restore_cannot_resolve_arbitrary_git_revisions(sandbox: Sandbox) -> None:
    original = sandbox.git("rev-parse", "HEAD").stdout.strip()
    sandbox.commit("new.txt", "remote changes", path=sandbox.remote)
    result = sandbox.cli("restore", "origin/main")
    assert result.returncode != 0
    assert sandbox.git("rev-parse", "HEAD").stdout.strip() == original


def test_pull_requires_explicit_rebase_flag(sandbox: Sandbox) -> None:
    original = sandbox.git("rev-parse", "HEAD").stdout.strip()
    sandbox.commit("new.txt", "remote changes", path=sandbox.remote)
    result = sandbox.cli("pull")
    assert result.returncode == 2
    assert sandbox.git("rev-parse", "HEAD").stdout.strip() == original
