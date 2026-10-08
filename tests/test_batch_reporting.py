from conftest import Sandbox

from gtracker.mirror import list_backups
from gtracker.repo import Repo


def test_failed_batch_reports_recovery_ref_for_completed_branch(sandbox: Sandbox) -> None:
    for name in ("first", "second"):
        sandbox.git("switch", "--no-track", "-c", name, "origin/main")
        sandbox.git("config", f"branch.{name}.remote", "origin")
        sandbox.git("config", f"branch.{name}.merge", "refs/heads/main")
        sandbox.commit(f"{name}.txt", name)
    sandbox.git("switch", "main")
    hook = sandbox.repo / ".git" / "hooks" / "pre-rebase"
    hook.write_text('#!/bin/sh\nbranch=$(git symbolic-ref --short HEAD)\n[ "$branch" != second ]\n')
    hook.chmod(0o755)
    target = sandbox.commit("upstream.txt", "remote", path=sandbox.remote)
    second = sandbox.git("rev-parse", "second").stdout.strip()
    result = sandbox.cli("sync", "--strategy", "rebase", "--branch", "first", "--branch", "second")
    assert result.returncode != 0
    saved = next(
        backup for backup in list_backups(Repo.open(sandbox.repo)) if backup.branch == "first"
    )
    assert saved.ref in result.stdout
    sandbox.git("merge-base", "--is-ancestor", target, "refs/heads/first")
    assert sandbox.git("rev-parse", "refs/heads/second").stdout.strip() == second
    assert sandbox.git("symbolic-ref", "HEAD").stdout.strip() == "refs/heads/main"
