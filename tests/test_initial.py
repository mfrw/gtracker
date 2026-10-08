from conftest import Sandbox


def test_mirror_advances_tracking_branch_without_touching_local_work(sandbox: Sandbox) -> None:
    sandbox.git("switch", "-c", "personal")
    local = sandbox.commit("local.txt", "my work\n")
    target = sandbox.commit("upstream.txt", "new upstream\n", path=sandbox.remote)
    result = sandbox.cli("sync")
    assert result.returncode == 0, result.stderr
    assert sandbox.git("rev-parse", "main").stdout.strip() == target
    assert sandbox.git("rev-parse", "HEAD").stdout.strip() == local
    assert (sandbox.repo / "local.txt").read_text() == "my work\n"


def test_dry_run_fetches_but_does_not_advance_local_branches(sandbox: Sandbox) -> None:
    original = sandbox.git("rev-parse", "main").stdout.strip()
    target = sandbox.commit("upstream.txt", "new upstream\n", path=sandbox.remote)
    result = sandbox.cli("sync", "--dry-run")
    assert result.returncode == 0, result.stderr
    assert sandbox.git("rev-parse", "main").stdout.strip() == original
    assert sandbox.git("rev-parse", "origin/main").stdout.strip() == target
    assert not (sandbox.repo / "upstream.txt").exists()
