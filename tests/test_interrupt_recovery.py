from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from conftest import Sandbox

from gtracker import rebase
from gtracker.mirror import list_backups
from gtracker.rebase import sync_rebase
from gtracker.repo import Repo, Result, UpdateInterrupted


def _batch(sandbox: Sandbox) -> tuple[str, dict[str, str]]:
    sandbox.git("switch", "--no-track", "-c", "personal")
    personal = sandbox.commit("personal.txt", "caller work\n")
    tips = {}
    for name in ("first", "second"):
        sandbox.git("switch", "--no-track", "-c", name, "origin/main")
        sandbox.git("branch", "--set-upstream-to", "origin/main", name)
        tips[name] = sandbox.commit(f"{name}.txt", f"{name} work\n")
    sandbox.git("switch", "personal")
    return personal, tips


def _signal_hook(sandbox: Sandbox, hook_name: str, branch: str, *, dirty: bool = False) -> None:
    hook = sandbox.repo / ".git" / "hooks" / hook_name
    marker = hook.with_suffix(".signalled")
    hook.write_text(
        f"#!{sys.executable}\n"
        "import os, select, signal, subprocess\n"
        "from pathlib import Path\n"
        "current = subprocess.run(['git', 'symbolic-ref', '--short', '-q', 'HEAD'], "
        "capture_output=True, text=True).stdout.strip()\n"
        f"marker = Path({str(marker)!r})\n"
        f"if current == {branch!r} and not marker.exists():\n"
        "    marker.touch()\n"
        f"    if {dirty!r}:\n"
        "        Path('interrupted.txt').write_text('leave this work intact\\n')\n"
        "    os.kill(int(os.environ['GTRACKER_TEST_PID']), signal.SIGINT)\n"
        "    select.select([], [], [], 15)\n"
    )
    hook.chmod(0o755)


def _cli(sandbox: Sandbox, *args: str, active: bool = False) -> subprocess.CompletedProcess[str]:
    # A real conflicting rebase has deterministic on-disk state. Signal at its
    # return boundary rather than race Git's writes using an arbitrary delay.
    program = (
        "import os, signal\n"
        "from gtracker.repo import Repo\n"
        "from gtracker.cli import main\n"
        "os.environ['GTRACKER_TEST_PID'] = str(os.getpid())\n"
        "original_git = Repo.git\n"
        "def interrupt_active(self, *args, **kwargs):\n"
        "    result = original_git(self, *args, **kwargs)\n"
        "    if 'rebase' in args and self.operation() in ('rebase-merge', 'rebase-apply'):\n"
        "        signal.raise_signal(signal.SIGINT)\n"
        "    return result\n"
        + ("Repo.git = interrupt_active\n" if active else "")
        + "raise SystemExit(main())\n"
    )
    return subprocess.run(
        [sys.executable, "-c", program, *args, "--repo", str(sandbox.repo)],
        text=True,
        capture_output=True,
        timeout=30,
    )


def _assert_backups_reported(sandbox: Sandbox, output: str, tips: dict[str, str]) -> None:
    backups = list_backups(Repo.open(sandbox.repo))
    assert {backup.branch for backup in backups} == set(tips)
    for backup in backups:
        assert backup.ref in output
        assert backup.oid == tips[backup.branch]


@pytest.mark.parametrize("detached", [False, True])
@pytest.mark.parametrize("phase", ["checkout", "pre-rebase"])
def test_batch_interrupt_before_active_rebase_reports_prior_updates_and_restores_caller(
    sandbox: Sandbox, detached: bool, phase: str
) -> None:
    personal, tips = _batch(sandbox)
    if detached:
        sandbox.git("switch", "--detach", personal)
    target = sandbox.commit("upstream.txt", "upstream\n", path=sandbox.remote)
    _signal_hook(sandbox, "post-checkout" if phase == "checkout" else "pre-rebase", "second")

    result = _cli(
        sandbox, "sync", "--strategy", "rebase", "--branch", "first", "--branch", "second"
    )

    assert result.returncode == 130, result.stdout + result.stderr
    assert "first: rebased" in result.stdout
    assert "second: interrupted" in result.stdout
    expected = tips if phase == "pre-rebase" else {"first": tips["first"]}
    _assert_backups_reported(sandbox, result.stdout + result.stderr, expected)
    assert sandbox.git("rev-parse", "HEAD").stdout.strip() == personal
    current = sandbox.git("symbolic-ref", "-q", "HEAD", check=False)
    assert current.returncode == (1 if detached else 0)
    if not detached:
        assert current.stdout.strip() == "refs/heads/personal"
    assert sandbox.git("rev-parse", "second").stdout.strip() == tips["second"]
    sandbox.git("merge-base", "--is-ancestor", target, "first")
    assert Repo.open(sandbox.repo).operation() is None
    assert str(sandbox.repo) in result.stderr and "manual" in result.stderr.lower()


@pytest.mark.parametrize("linked", [False, True])
def test_active_interruption_retains_state_and_restores_only_unaffected_caller(
    sandbox: Sandbox, tmp_path: Path, linked: bool
) -> None:
    personal, tips = _batch(sandbox)
    working = tmp_path / "linked interruption" if linked else sandbox.repo
    if linked:
        sandbox.git("worktree", "add", str(working), "second")
    else:
        sandbox.git("switch", "second")
    tips = {**tips, "second": sandbox.commit("base.txt", "local conflict\n", path=working)}
    if not linked:
        sandbox.git("switch", "personal")
    target = sandbox.commit("base.txt", "remote conflict\n", path=sandbox.remote)

    result = _cli(
        sandbox,
        "sync",
        "--strategy",
        "rebase",
        "--branch",
        "first",
        "--branch",
        "second",
        active=True,
    )

    assert result.returncode == 130, result.stdout + result.stderr
    assert "first: rebased" in result.stdout and "second: interrupted" in result.stdout
    _assert_backups_reported(sandbox, result.stdout + result.stderr, tips)
    assert Repo.open(working).operation() in ("rebase-merge", "rebase-apply")
    assert (
        sandbox.git("diff", "--name-only", "--diff-filter=U", path=working).stdout.strip()
        == "base.txt"
    )
    sandbox.git("merge-base", "--is-ancestor", target, "first")
    if linked:
        assert sandbox.git("symbolic-ref", "HEAD").stdout.strip() == "refs/heads/personal"
        assert sandbox.git("rev-parse", "HEAD").stdout.strip() == personal
    else:
        assert sandbox.git("symbolic-ref", "-q", "HEAD", check=False).returncode == 1
    assert str(working) in result.stderr
    assert "--continue" in result.stderr and "--abort" in result.stderr


def test_interrupt_during_restoration_reports_every_completed_update(sandbox: Sandbox) -> None:
    personal, tips = _batch(sandbox)
    sandbox.commit("upstream.txt", "upstream\n", path=sandbox.remote)
    _signal_hook(sandbox, "post-checkout", "personal")

    result = _cli(
        sandbox, "sync", "--strategy", "rebase", "--branch", "first", "--branch", "second"
    )

    assert result.returncode == 130, result.stdout + result.stderr
    assert "first: rebased" in result.stdout and "second: rebased" in result.stdout
    _assert_backups_reported(sandbox, result.stdout + result.stderr, tips)
    assert sandbox.git("symbolic-ref", "HEAD").stdout.strip() == "refs/heads/personal"
    assert sandbox.git("rev-parse", "HEAD").stdout.strip() == personal


@pytest.mark.parametrize("active", [False, True])
def test_pull_interrupt_reports_backup_and_preserves_recovery_state(
    sandbox: Sandbox, active: bool
) -> None:
    original = sandbox.commit("base.txt" if active else "local.txt", "local\n")
    sandbox.commit("base.txt" if active else "upstream.txt", "upstream\n", path=sandbox.remote)
    if not active:
        _signal_hook(sandbox, "pre-rebase", "main")

    result = _cli(sandbox, "pull", "--rebase", active=active)

    assert result.returncode == 130, result.stdout + result.stderr
    assert "main: interrupted" in result.stdout
    _assert_backups_reported(sandbox, result.stdout + result.stderr, {"main": original})
    assert str(sandbox.repo) in result.stderr
    if active:
        assert Repo.open(sandbox.repo).operation() in ("rebase-merge", "rebase-apply")
        assert sandbox.git("diff", "--name-only", "--diff-filter=U").stdout.strip() == "base.txt"
    else:
        assert sandbox.git("symbolic-ref", "HEAD").stdout.strip() == "refs/heads/main"
        assert sandbox.git("rev-parse", "HEAD").stdout.strip() == original


def test_nested_backup_interruption_preserves_current_and_completed_results(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    personal, tips = _batch(sandbox)
    sandbox.commit("upstream.txt", "upstream\n", path=sandbox.remote)
    original_create = rebase.create_backup

    def interrupt_backup(repo: Repo, branch, reason: str = "update") -> str:
        backup = original_create(repo, branch, reason)
        if branch.name == "second":
            raise UpdateInterrupted(
                "Interrupted after creating recovery ref",
                (Result(branch.name, "interrupted", str(repo.path), backup),),
            )
        return backup

    monkeypatch.setattr(rebase, "create_backup", interrupt_backup)
    with pytest.raises(UpdateInterrupted) as caught:
        sync_rebase(Repo.open(sandbox.repo), ("first", "second"))

    assert [(row.branch, row.action) for row in caught.value.results] == [
        ("first", "rebased"),
        ("second", "interrupted"),
    ]
    for row in caught.value.results:
        assert (
            row.backup and sandbox.git("rev-parse", row.backup).stdout.strip() == tips[row.branch]
        )
    assert sandbox.git("rev-parse", "HEAD").stdout.strip() == personal


def test_batch_does_not_integrate_cached_ref_of_skipped_remote(sandbox: Sandbox) -> None:
    base = sandbox.git("rev-parse", "main").stdout.strip()
    sandbox.commit("upstream.txt", "cached upstream\n", path=sandbox.remote)
    sandbox.git("fetch", "origin")
    sandbox.git("config", "remote.origin.skipFetchAll", "true")
    sandbox.git("switch", "--no-track", "-c", "personal")

    rows = sync_rebase(Repo.open(sandbox.repo), ("main",))

    assert [(row.branch, row.action) for row in rows] == [("main", "skipped-unfetched")]
    assert sandbox.git("rev-parse", "main").stdout.strip() == base
    assert sandbox.git("symbolic-ref", "HEAD").stdout.strip() == "refs/heads/personal"
    assert not list_backups(Repo.open(sandbox.repo))


def test_dirty_interrupted_checkout_is_not_switched_or_discarded(sandbox: Sandbox) -> None:
    _, tips = _batch(sandbox)
    sandbox.commit("upstream.txt", "upstream\n", path=sandbox.remote)
    _signal_hook(sandbox, "pre-rebase", "second", dirty=True)

    result = _cli(
        sandbox, "sync", "--strategy", "rebase", "--branch", "first", "--branch", "second"
    )

    assert result.returncode == 130, result.stdout + result.stderr
    _assert_backups_reported(sandbox, result.stdout + result.stderr, tips)
    assert sandbox.git("symbolic-ref", "HEAD").stdout.strip() == "refs/heads/second"
    assert (sandbox.repo / "interrupted.txt").read_text() == "leave this work intact\n"
    assert "restore" in result.stderr.lower() and "manual" in result.stderr.lower()


def test_batch_integrates_exact_fetched_oid_not_subsequently_changed_tracking_ref(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = sandbox.git("rev-parse", "main").stdout.strip()
    sandbox.git("switch", "--no-track", "-c", "personal")
    target = sandbox.commit("upstream.txt", "upstream\n", path=sandbox.remote)
    original_fetch = Repo.fetch_all

    def move_tracking_after_fetch(self: Repo):
        report = original_fetch(self)
        self.git("update-ref", "refs/remotes/origin/main", before)
        return report

    monkeypatch.setattr(Repo, "fetch_all", move_tracking_after_fetch)
    rows = sync_rebase(Repo.open(sandbox.repo), ("main",))

    assert rows[0].action == "rebased"
    assert sandbox.git("rev-parse", "main").stdout.strip() == target
    assert sandbox.git("rev-parse", "origin/main").stdout.strip() == before
    assert sandbox.git("symbolic-ref", "HEAD").stdout.strip() == "refs/heads/personal"


def test_failed_attempt_keeps_known_backup_without_interruptible_deletion(sandbox: Sandbox) -> None:
    personal, tips = _batch(sandbox)
    sandbox.commit("upstream.txt", "upstream\n", path=sandbox.remote)
    hook = sandbox.repo / ".git" / "hooks" / "pre-rebase"
    hook.write_text('#!/bin/sh\n[ "$(git symbolic-ref --short HEAD)" != second ]\n')
    hook.chmod(0o755)

    result = _cli(
        sandbox, "sync", "--strategy", "rebase", "--branch", "first", "--branch", "second"
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "first: rebased" in result.stdout and "second: failed" in result.stdout
    _assert_backups_reported(sandbox, result.stdout + result.stderr, tips)
    assert sandbox.git("rev-parse", "HEAD").stdout.strip() == personal
