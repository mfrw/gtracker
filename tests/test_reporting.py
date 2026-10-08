from __future__ import annotations

import re

import pytest
from conftest import Sandbox

from gtracker import cli
from gtracker.rebase import RebaseConflict
from gtracker.repo import Result, UpdateError, UpdateInterrupted


def summary_counts(output: str) -> dict[str, int]:
    summaries = tuple(line for line in output.splitlines() if line.startswith("Summary"))
    assert len(summaries) == 1, output
    return {
        category: int(count)
        for count, category in re.findall(
            r"(\d+) (updated|unchanged|skipped|failed|previewed)", summaries[0]
        )
    }


def test_status_reports_invalid_configuration_without_mutation(sandbox: Sandbox) -> None:
    original = sandbox.git("rev-parse", "HEAD").stdout.strip()
    sandbox.git("config", "--unset-all", "branch.main.merge")
    sandbox.git("remote", "set-url", "origin", "/does/not/exist")

    result = sandbox.cli("status", "--branch", "main")

    assert result.returncode == 1
    assert "invalid-upstream" in result.stdout
    assert "local-only" not in result.stdout.lower()
    assert "merge" in result.stdout
    assert sandbox.git("rev-parse", "HEAD").stdout.strip() == original
    assert sandbox.git("rev-parse", "origin/main").stdout.strip() == original
    assert sandbox.git("config", "--get", "branch.main.merge", check=False).returncode == 1


def test_mixed_completion_reports_totals_and_backups(
    capsys: pytest.CaptureFixture[str],
) -> None:
    backup = "refs/gtracker/backups/saved/topic"
    results = (
        Result("main", "mirrored"),
        Result("topic", "rebased", backup=backup),
        Result("saved", "restored"),
        Result("matching", "unchanged"),
        Result("local", "skipped-local"),
    )

    assert cli.show_results(results) == 0

    output = capsys.readouterr().out
    assert summary_counts(output) == {"updated": 3, "unchanged": 1, "skipped": 1, "failed": 0}
    for result in results:
        assert f"{result.branch}: {result.action}" in output
    assert backup in output


@pytest.mark.parametrize(
    "action", ["skipped", "skipped-missing", "skipped-unfetched", "skipped-invalid"]
)
def test_warning_skips_are_not_successful(action: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.show_results((Result("main", "mirrored"), Result("topic", action))) == 1

    counts = summary_counts(capsys.readouterr().out)
    assert counts == {"updated": 1, "unchanged": 0, "skipped": 1, "failed": 0}


def test_dry_run_reports_previews_not_updates(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        cli,
        "sync_mirror",
        lambda *_: (
            Result("main", "would-mirror"),
            Result("topic", "would-rebase"),
            Result("same", "unchanged"),
        ),
    )

    assert cli.main(["sync", "--dry-run", "--repo", str(sandbox.repo)]) == 0

    assert summary_counts(capsys.readouterr().out) == {
        "updated": 0,
        "unchanged": 1,
        "skipped": 0,
        "failed": 0,
        "previewed": 2,
    }


@pytest.mark.parametrize("includes_failed_row", [False, True])
def test_partial_failure_keeps_completed_rows_and_counts_failure_once(
    sandbox: Sandbox,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    includes_failed_row: bool,
) -> None:
    backup = "refs/gtracker/backups/saved/main"
    completed = (Result("main", "mirrored", backup=backup),)
    results = (*completed, Result("topic", "failed")) if includes_failed_row else completed

    def fail(*_):
        raise UpdateError("Cannot update topic: locked worktree", results)

    monkeypatch.setattr(cli, "sync_mirror", fail)

    assert cli.main(["sync", "--repo", str(sandbox.repo)]) == 1

    captured = capsys.readouterr()
    assert "main: mirrored" in captured.out
    assert backup in captured.out
    assert "locked worktree" in captured.err
    assert summary_counts(captured.out) == {"updated": 1, "unchanged": 0, "skipped": 0, "failed": 1}


@pytest.mark.parametrize("includes_conflict_row", [False, True])
def test_conflict_reports_failed_branch_and_backup_with_prior_success(
    sandbox: Sandbox,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    includes_conflict_row: bool,
) -> None:
    backup = "refs/gtracker/backups/saved/topic"
    completed = (Result("main", "rebased"),)
    results = (
        (*completed, Result("topic", "conflict", "content conflict", backup))
        if includes_conflict_row
        else completed
    )

    def conflict(*_):
        raise RebaseConflict("topic", sandbox.repo, backup, "content conflict", results)

    monkeypatch.setattr(cli, "sync_rebase", conflict)

    assert cli.main(["sync", "--strategy", "rebase", "--repo", str(sandbox.repo)]) == 1

    captured = capsys.readouterr()
    assert "main: rebased" in captured.out
    action = "conflict" if includes_conflict_row else "failed"
    assert f"topic: {action}" in captured.out
    assert captured.out.count("topic: ") == 1
    assert backup in captured.out
    assert "--continue" in captured.err
    assert "--abort" in captured.err
    assert summary_counts(captured.out)["failed"] == 1
    assert summary_counts(captured.out)["updated"] == 1


def test_interruption_keeps_completed_rows_current_attempt_and_recovery(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    backup = "refs/gtracker/backups/saved/topic"

    def interrupt(*_):
        raise UpdateInterrupted(
            "Interrupted; restored original checkout main",
            (
                Result("main", "mirrored"),
                Result("topic", "interrupted", "inspect saved tip before retrying", backup),
            ),
        )

    monkeypatch.setattr(cli, "sync_mirror", interrupt)

    assert cli.main(["sync", "--repo", str(sandbox.repo)]) == 130

    captured = capsys.readouterr()
    assert "main: mirrored" in captured.out
    assert "topic: interrupted" in captured.out
    assert backup in captured.out
    assert "restored original checkout main" in captured.err
    assert summary_counts(captured.out) == {"updated": 1, "unchanged": 0, "skipped": 0, "failed": 1}


def test_unknown_interrupt_does_not_claim_no_updates(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def interrupt(*_):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "sync_mirror", interrupt)

    assert cli.main(["sync", "--repo", str(sandbox.repo)]) == 130

    captured = capsys.readouterr()
    assert "inspect Git state" in captured.err
    assert "no local" not in captured.err.lower()


def test_sync_mixed_outcomes_preserve_success_and_warning_exit(sandbox: Sandbox) -> None:
    sandbox.git("branch", "matching", "main")
    sandbox.git("branch", "--set-upstream-to", "origin/main", "matching")
    sandbox.git("branch", "local", "main")
    sandbox.git("branch", "missing", "main")
    sandbox.git("config", "branch.missing.remote", "origin")
    sandbox.git("config", "branch.missing.merge", "refs/heads/deleted")
    sandbox.git("branch", "topic", "main", path=sandbox.remote)
    sandbox.git("fetch", "origin")
    sandbox.git("branch", "--set-upstream-to", "origin/topic", "matching")
    new_tip = sandbox.commit("new.txt", "remote update", path=sandbox.remote)

    result = sandbox.cli("sync")

    assert result.returncode == 1, result.stderr
    assert sandbox.git("rev-parse", "main").stdout.strip() == new_tip
    assert summary_counts(result.stdout) == {
        "updated": 1,
        "unchanged": 1,
        "skipped": 2,
        "failed": 0,
    }
    assert "main: mirrored" in result.stdout
    assert "matching: unchanged" in result.stdout
    assert "local: skipped-local" in result.stdout
    assert "missing: skipped-missing" in result.stdout


def test_fetch_failure_reports_zero_updates_and_one_failure(sandbox: Sandbox) -> None:
    original = sandbox.git("rev-parse", "HEAD").stdout.strip()
    sandbox.commit("upstream.txt", "new remote", path=sandbox.remote)
    sandbox.git("remote", "add", "broken", str(sandbox.repo.parent / "missing-remote"))

    result = sandbox.cli("sync")

    assert result.returncode == 1, result.stdout + result.stderr
    assert summary_counts(result.stdout) == {
        "updated": 0,
        "unchanged": 0,
        "skipped": 0,
        "failed": 1,
    }
    assert sandbox.git("rev-parse", "HEAD").stdout.strip() == original


@pytest.mark.parametrize("width", [40, 240])
def test_status_keeps_full_names_and_worktree_locations(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch, width: int
) -> None:
    branch = "topic/" + "long-descriptive-branch-name-" * 2
    linked = sandbox.repo.parent / "linked worktree"
    sandbox.git("branch", "topic", "main", path=sandbox.remote)
    sandbox.git("fetch", "origin")
    sandbox.git("branch", branch, "origin/topic")
    sandbox.git("worktree", "add", str(linked), branch)
    sandbox.git("branch", "local", "main")
    sandbox.commit("ahead.txt", "local change")
    monkeypatch.setenv("COLUMNS", str(width))

    result = sandbox.cli("status")

    assert result.returncode == 0, result.stderr
    assert branch in result.stdout
    assert "origin/topic" in result.stdout
    assert str(linked) in result.stdout
    assert "ahead" in result.stdout
    assert "matching" in result.stdout
    assert "local-only" in result.stdout
    assert f"+ {branch}" in result.stdout
    assert "* main" in result.stdout
