import pytest
from conftest import Sandbox

from gtracker.mirror import list_backups, sync_mirror
from gtracker.repo import Repo


@pytest.mark.parametrize("strategy", ["mirror", "rebase"])
@pytest.mark.parametrize("setting", ["skipFetchAll", "skipDefaultUpdate"])
def test_skipped_remote_cannot_supply_a_cached_update(
    sandbox: Sandbox, strategy: str, setting: str
) -> None:
    cached = sandbox.git("rev-parse", "origin/main").stdout.strip()
    local = sandbox.commit("local.txt", "new local tip\n")
    sandbox.commit("remote.txt", "new remote tip\n", path=sandbox.remote)
    sandbox.git("config", f"remote.origin.{setting}", "true")

    result = sandbox.cli("sync", "--strategy", strategy)

    assert result.returncode == 1, result.stdout + result.stderr
    assert sandbox.git("rev-parse", "main").stdout.strip() == local
    assert sandbox.git("rev-parse", "origin/main").stdout.strip() == cached
    assert (sandbox.repo / "local.txt").read_text() == "new local tip\n"
    assert list_backups(Repo.open(sandbox.repo)) == ()


@pytest.mark.parametrize(
    ("earlier", "later"),
    [("skipDefaultUpdate", "skipFetchAll"), ("skipFetchAll", "skipDefaultUpdate")],
)
def test_last_skip_setting_wins_across_deprecated_aliases(
    sandbox: Sandbox, earlier: str, later: str
) -> None:
    sandbox.git("config", f"remote.origin.{earlier}", "true")
    sandbox.git("config", f"remote.origin.{later}", "false")
    target = sandbox.commit("remote.txt", "fresh\n", path=sandbox.remote)

    result = sandbox.cli("sync")

    assert result.returncode == 0, result.stdout + result.stderr
    assert sandbox.git("rev-parse", "main").stdout.strip() == target
    assert (sandbox.repo / "remote.txt").read_text() == "fresh\n"


def test_negative_refspec_cannot_rewind_to_an_excluded_cached_ref(sandbox: Sandbox) -> None:
    cached = sandbox.git("rev-parse", "origin/main").stdout.strip()
    local = sandbox.commit("local.txt", "keep local\n")
    sandbox.commit("remote.txt", "unfetched\n", path=sandbox.remote)
    sandbox.git("config", "--add", "remote.origin.fetch", "^refs/heads/main")

    result = sandbox.cli("sync")

    assert result.returncode == 1, result.stdout + result.stderr
    assert sandbox.git("rev-parse", "main").stdout.strip() == local
    assert sandbox.git("rev-parse", "origin/main").stdout.strip() == cached
    assert list_backups(Repo.open(sandbox.repo)) == ()


def test_update_uses_its_remote_snapshot_when_fetch_destinations_overlap(sandbox: Sandbox) -> None:
    other = sandbox.remote.parent / "other"
    sandbox.git("clone", str(sandbox.remote), str(other))
    wanted = sandbox.commit("wanted.txt", "configured upstream\n", path=sandbox.remote)
    unwanted = sandbox.commit("wrong.txt", "different remote\n", path=other)
    sandbox.git("remote", "add", "z-other", str(other))
    sandbox.git("config", "remote.z-other.fetch", "+refs/heads/*:refs/remotes/origin/*")

    sync_mirror(Repo.open(sandbox.repo))

    assert sandbox.git("rev-parse", "refs/remotes/origin/main").stdout.strip() == unwanted
    assert sandbox.git("rev-parse", "refs/heads/main").stdout.strip() == wanted
    assert (sandbox.repo / "wanted.txt").read_text() == "configured upstream\n"
    assert not (sandbox.repo / "wrong.txt").exists()


def test_configured_local_upstream_does_not_require_remote_fetch(sandbox: Sandbox) -> None:
    sandbox.git("switch", "--no-track", "-c", "integration")
    target = sandbox.commit("local-upstream.txt", "local integration\n")
    sandbox.git("switch", "main")
    sandbox.git("config", "branch.main.remote", ".")
    sandbox.git("config", "branch.main.merge", "refs/heads/integration")
    sandbox.git("config", "remote.origin.skipFetchAll", "true")

    result = sandbox.cli("sync", "--branch", "main")

    assert result.returncode == 0, result.stdout + result.stderr
    assert sandbox.git("rev-parse", "main").stdout.strip() == target
    assert (sandbox.repo / "local-upstream.txt").read_text() == "local integration\n"
