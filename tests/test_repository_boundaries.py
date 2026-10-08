import os
from pathlib import Path

import pytest
from conftest import Sandbox

from gtracker.mirror import sync_mirror
from gtracker.repo import Repo


def test_trailing_whitespace_path_cannot_redirect_updates_to_sibling_repository(
    sandbox: Sandbox,
    tmp_path: Path,
) -> None:
    ordinary, spaced = tmp_path / "project", tmp_path / "project "
    sandbox.git("clone", str(sandbox.remote), str(ordinary))
    sandbox.git("clone", str(sandbox.remote), str(spaced))
    original = sandbox.git("rev-parse", "HEAD", path=ordinary).stdout.strip()
    target = sandbox.commit("upstream.txt", "new remote", path=sandbox.remote)
    sync_mirror(Repo.open(spaced))
    assert sandbox.git("rev-parse", "HEAD", path=spaced).stdout.strip() == target
    assert sandbox.git("rev-parse", "HEAD", path=ordinary).stdout.strip() == original


def test_non_utf8_ignored_filename_is_preserved_without_decode_crash(sandbox: Sandbox) -> None:
    raw_path = os.fsencode(sandbox.repo) + b"/artifact-\xff"
    with open(raw_path, "wb") as handle:
        handle.write(b"private build contents")
    (sandbox.repo / ".git" / "info" / "exclude").write_bytes(b"artifact-*\n")
    target = sandbox.commit("upstream.txt", "new remote", path=sandbox.remote)
    sync_mirror(Repo.open(sandbox.repo))
    assert sandbox.git("rev-parse", "HEAD").stdout.strip() == target
    with open(raw_path, "rb") as handle:
        assert handle.read() == b"private build contents"


@pytest.mark.parametrize("command", ["status", "backups"])
def test_read_only_commands_do_not_lazy_fetch_missing_promisor_objects(
    sandbox: Sandbox,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    target = sandbox.commit("upstream.txt", "new remote", path=sandbox.remote)
    sandbox.git("fetch", "origin")
    backup = "refs/gtracker/backups/manual/main"
    if command == "backups":
        sandbox.git("update-ref", backup, target)
    object_path = sandbox.repo / ".git" / "objects" / target[:2] / target[2:]
    object_path.unlink()
    marker = tmp_path / "transport-used"
    helper = tmp_path / "git-remote-audit"
    helper.write_text(f"#!/bin/sh\nprintf attempted >> '{marker}'\nexit 1\n")
    helper.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file:audit")
    sandbox.git("config", "remote.origin.url", "audit::missing")
    sandbox.git("config", "remote.origin.promisor", "true")
    sandbox.git("config", "remote.origin.partialclonefilter", "blob:none")
    result = sandbox.cli(command)
    assert not marker.exists()
    assert result.returncode != 0
    assert "Traceback" not in result.stderr
