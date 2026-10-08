from __future__ import annotations

from pathlib import Path

import pytest
from conftest import Sandbox

from gtracker.rebase import RebaseConflict, pull_rebase, sync_rebase
from gtracker.repo import GitError, Repo


def local_refs(box: Sandbox) -> str:
    rows = box.git("for-each-ref", "--format=%(refname) %(objectname)").stdout.splitlines()
    return "\n".join(row for row in rows if not row.startswith("refs/remotes/"))


def configure_tracking(box: Sandbox, name: str, upstream: str = "origin/main") -> None:
    box.git("branch", "--set-upstream-to", upstream, name)


def assert_preserved(
    box: Sandbox, upstream: str, filename: str, text: str, *, ref: str = "HEAD"
) -> None:
    box.git("merge-base", "--is-ancestor", upstream, ref)
    assert box.git("show", f"{ref}:{filename}").stdout == text


def test_pull_configured_upstream_preserves_local_commit_and_backup(sandbox: Sandbox) -> None:
    original = sandbox.commit("local.txt", "local work\n")
    upstream = sandbox.commit("upstream.txt", "upstream work\n", path=sandbox.remote)
    result = pull_rebase(Repo.open(sandbox.repo))
    assert_preserved(sandbox, upstream, "local.txt", "local work\n")
    assert result.backup and sandbox.git("rev-parse", result.backup).stdout.strip() == original
    assert sandbox.git("symbolic-ref", "HEAD").stdout.strip() == "refs/heads/main"
    assert sandbox.git("config", "--get", "branch.main.remote").stdout.strip() == "origin"


def test_explicit_pull_does_not_enroll_personal_branch(sandbox: Sandbox) -> None:
    sandbox.git("switch", "--no-track", "-c", "personal")
    original = sandbox.commit("local.txt", "personal work\n")
    upstream = sandbox.commit("upstream.txt", "upstream work\n", path=sandbox.remote)
    result = pull_rebase(Repo.open(sandbox.repo), "origin/main")
    assert_preserved(sandbox, upstream, "local.txt", "personal work\n")
    assert result.backup and sandbox.git("rev-parse", result.backup).stdout.strip() == original
    assert sandbox.git("config", "--get", "branch.personal.remote", check=False).returncode == 1
    assert sandbox.git("config", "--get", "branch.personal.merge", check=False).returncode == 1


def test_explicit_pull_honors_custom_fetch_mapping(sandbox: Sandbox) -> None:
    sandbox.git(
        "config", "--replace-all", "remote.origin.fetch", "+refs/heads/*:refs/remotes/kernel/*"
    )
    sandbox.git("fetch", "origin")
    sandbox.git("switch", "--no-track", "-c", "personal")
    sandbox.commit("local.txt", "personal work\n")
    upstream = sandbox.commit("upstream.txt", "upstream\n", path=sandbox.remote)
    pull_rebase(Repo.open(sandbox.repo), "origin/main")
    assert_preserved(sandbox, upstream, "local.txt", "personal work\n")


@pytest.mark.parametrize("operation", ["pull", "sync"])
def test_upstream_rewrite_does_not_resurrect_removed_integration_commits(
    sandbox: Sandbox, operation: str
) -> None:
    base = sandbox.git("rev-parse", "HEAD").stdout.strip()
    sandbox.commit("obsolete.txt", "obsolete integration\n", path=sandbox.remote)
    sandbox.git("pull", "--ff-only")
    sandbox.commit("local.txt", "my work\n")
    sandbox.git("reset", "--hard", base, path=sandbox.remote)
    upstream = sandbox.commit("replacement.txt", "replacement integration\n", path=sandbox.remote)
    repo = Repo.open(sandbox.repo)
    if operation == "pull":
        pull_rebase(repo)
    else:
        sync_rebase(repo)
    assert_preserved(sandbox, upstream, "local.txt", "my work\n")
    assert not (sandbox.repo / "obsolete.txt").exists()
    assert sandbox.git("rev-list", "--count", f"{upstream}..HEAD").stdout.strip() == "1"


@pytest.mark.parametrize("detached", [False, True])
def test_selected_sync_rebases_sequentially_and_restores_checkout(
    sandbox: Sandbox, detached: bool
) -> None:
    sandbox.git("switch", "--no-track", "-c", "personal")
    initial = sandbox.commit("personal.txt", "leave alone\n")
    for name in ("first", "second"):
        sandbox.git("switch", "--no-track", "-c", name, "origin/main")
        configure_tracking(sandbox, name)
        sandbox.commit(f"{name}.txt", f"{name} work\n")
    sandbox.git("switch", "personal")
    if detached:
        sandbox.git("switch", "--detach", initial)
    upstream = sandbox.commit("upstream.txt", "upstream\n", path=sandbox.remote)
    results = sync_rebase(Repo.open(sandbox.repo), ("second", "first"))
    assert tuple(result.branch for result in results) == ("second", "first")
    for name in ("first", "second"):
        assert_preserved(
            sandbox, upstream, f"{name}.txt", f"{name} work\n", ref=f"refs/heads/{name}"
        )
    assert sandbox.git("rev-parse", "HEAD").stdout.strip() == initial
    current = sandbox.git("symbolic-ref", "-q", "HEAD", check=False)
    assert current.returncode == (1 if detached else 0)
    if not detached:
        assert current.stdout.strip() == "refs/heads/personal"


def test_default_sync_leaves_untracked_personal_branches_untouched(sandbox: Sandbox) -> None:
    sandbox.git("switch", "--no-track", "-c", "personal")
    personal = sandbox.commit("personal.txt", "personal\n")
    upstream = sandbox.commit("upstream.txt", "upstream\n", path=sandbox.remote)
    results = sync_rebase(Repo.open(sandbox.repo))
    assert tuple(result.branch for result in results) == ("main",)
    assert sandbox.git("rev-parse", "personal").stdout.strip() == personal
    assert sandbox.git("rev-parse", "main").stdout.strip() == upstream
    assert sandbox.git("symbolic-ref", "HEAD").stdout.strip() == "refs/heads/personal"


def test_conflict_leaves_checkout_and_backup_and_stops_batch(sandbox: Sandbox) -> None:
    sandbox.git("switch", "--no-track", "-c", "first")
    configure_tracking(sandbox, "first")
    original = sandbox.commit("base.txt", "local version\n")
    sandbox.git("switch", "--no-track", "-c", "second", "origin/main")
    configure_tracking(sandbox, "second")
    second = sandbox.commit("second.txt", "later work\n")
    sandbox.git("switch", "main")
    sandbox.commit("base.txt", "upstream version\n", path=sandbox.remote)
    with pytest.raises(RebaseConflict) as caught:
        sync_rebase(Repo.open(sandbox.repo), ("first", "second"))
    error = caught.value
    assert error.branch == "first"
    assert error.path == sandbox.repo
    assert sandbox.git("rev-parse", error.backup).stdout.strip() == original
    assert sandbox.git("rev-parse", "second").stdout.strip() == second
    assert Repo.open(sandbox.repo).operation() in ("rebase-merge", "rebase-apply")
    assert sandbox.git("status", "--porcelain").stdout.startswith("UU base.txt")
    assert str(sandbox.repo) in str(error)
    assert "rebase --continue" in str(error) and "rebase --abort" in str(error)
    sandbox.git("rebase", "--abort")
    assert sandbox.git("symbolic-ref", "HEAD").stdout.strip() == "refs/heads/first"
    assert sandbox.git("rev-parse", "HEAD").stdout.strip() == original


def test_linked_worktree_conflict_does_not_change_calling_checkout(
    sandbox: Sandbox, tmp_path: Path
) -> None:
    linked = tmp_path / "linked conflict"
    sandbox.git("branch", "topic", "origin/main")
    configure_tracking(sandbox, "topic")
    sandbox.git("worktree", "add", str(linked), "topic")
    original = sandbox.commit("base.txt", "local\n", path=linked)
    sandbox.commit("base.txt", "upstream\n", path=sandbox.remote)
    with pytest.raises(RebaseConflict) as caught:
        sync_rebase(Repo.open(sandbox.repo), ("topic",))
    assert caught.value.path == linked
    assert sandbox.git("rev-parse", caught.value.backup).stdout.strip() == original
    assert sandbox.git("symbolic-ref", "HEAD").stdout.strip() == "refs/heads/main"
    assert Repo.open(linked).operation() in ("rebase-merge", "rebase-apply")
    sandbox.git("rebase", "--abort", path=linked)


@pytest.mark.parametrize("state", ["clean", "dirty", "locked"])
def test_linked_worktrees_are_used_only_when_clean_and_unlocked(
    sandbox: Sandbox, tmp_path: Path, state: str
) -> None:
    linked = tmp_path / "linked"
    sandbox.git("branch", "topic", "origin/main")
    configure_tracking(sandbox, "topic")
    sandbox.git("worktree", "add", str(linked), "topic")
    original = sandbox.commit("local.txt", "local\n", path=linked)
    if state == "dirty":
        (linked / "dirty.txt").write_text("leave alone\n")
    elif state == "locked":
        sandbox.git("worktree", "lock", str(linked))
    upstream = sandbox.commit("upstream.txt", "upstream\n", path=sandbox.remote)
    results = sync_rebase(Repo.open(sandbox.repo), ("topic",))
    if state == "clean":
        assert_preserved(sandbox, upstream, "local.txt", "local\n", ref="topic")
        assert (linked / "upstream.txt").read_text() == "upstream\n"
    else:
        assert sandbox.git("rev-parse", "topic").stdout.strip() == original
        assert results[0].action == "skipped" and state in results[0].detail.lower()
        if state == "dirty":
            assert (linked / "dirty.txt").read_text() == "leave alone\n"
    assert sandbox.git("symbolic-ref", "HEAD").stdout.strip() == "refs/heads/main"


@pytest.mark.parametrize(
    "upstream",
    [
        "missing/main",
        "origin/missing",
        "origin/../main",
        "origin/main~1",
        "--upload-pack=bad/main",
        "",
        "origin/main:refs/heads/personal",
    ],
)
def test_invalid_explicit_upstream_is_rejected_before_fetch(
    sandbox: Sandbox, upstream: str
) -> None:
    original = sandbox.git("rev-parse", "origin/main").stdout.strip()
    before = local_refs(sandbox)
    sandbox.commit("upstream.txt", "upstream\n", path=sandbox.remote)
    with pytest.raises(GitError):
        pull_rebase(Repo.open(sandbox.repo), upstream)
    assert local_refs(sandbox) == before
    assert sandbox.git("rev-parse", "origin/main").stdout.strip() == original
    assert not (sandbox.repo / "upstream.txt").exists()


def test_pull_requires_current_branch_and_upstream(sandbox: Sandbox) -> None:
    sandbox.git("switch", "--no-track", "-c", "personal")
    with pytest.raises(GitError, match="upstream"):
        pull_rebase(Repo.open(sandbox.repo))
    sandbox.git("switch", "--detach")
    with pytest.raises(GitError, match="branch"):
        pull_rebase(Repo.open(sandbox.repo), "origin/main")


def test_dirty_pull_is_rejected_without_stashing_or_fetching(sandbox: Sandbox) -> None:
    old_upstream = sandbox.git("rev-parse", "origin/main").stdout.strip()
    sandbox.git("config", "rebase.autoStash", "true")
    (sandbox.repo / "base.txt").write_text("uncommitted\n")
    sandbox.commit("upstream.txt", "upstream\n", path=sandbox.remote)
    with pytest.raises(GitError, match="uncommitted"):
        pull_rebase(Repo.open(sandbox.repo))
    assert sandbox.git("rev-parse", "origin/main").stdout.strip() == old_upstream
    assert (sandbox.repo / "base.txt").read_text() == "uncommitted\n"
    assert sandbox.git("stash", "list").stdout == ""


@pytest.mark.parametrize("operation", ["pull", "sync"])
def test_dry_run_fetches_but_never_changes_local_refs_or_contents(
    sandbox: Sandbox, operation: str
) -> None:
    sandbox.commit("local.txt", "local\n")
    before = local_refs(sandbox)
    contents = (sandbox.repo / "local.txt").read_text()
    upstream = sandbox.commit("upstream.txt", "upstream\n", path=sandbox.remote)
    repo = Repo.open(sandbox.repo)
    if operation == "pull":
        pull_rebase(repo, dry_run=True)
    else:
        sync_rebase(repo, dry_run=True)
    assert local_refs(sandbox) == before
    assert (sandbox.repo / "local.txt").read_text() == contents
    assert not (sandbox.repo / "upstream.txt").exists()
    assert sandbox.git("rev-parse", "origin/main").stdout.strip() == upstream
    assert sandbox.git("symbolic-ref", "HEAD").stdout.strip() == "refs/heads/main"


def test_fetch_failure_prevents_all_sync_local_mutations(sandbox: Sandbox, tmp_path: Path) -> None:
    sandbox.commit("local.txt", "local\n")
    sandbox.git("remote", "add", "broken", str(tmp_path / "does-not-exist"))
    sandbox.commit("upstream.txt", "upstream\n", path=sandbox.remote)
    before = local_refs(sandbox)
    with pytest.raises(GitError):
        sync_rebase(Repo.open(sandbox.repo))
    assert local_refs(sandbox) == before
    assert not (sandbox.repo / "upstream.txt").exists()


def test_invalid_batch_selection_is_validated_before_fetch(sandbox: Sandbox) -> None:
    old_upstream = sandbox.git("rev-parse", "origin/main").stdout.strip()
    sandbox.commit("upstream.txt", "upstream\n", path=sandbox.remote)
    with pytest.raises(GitError):
        sync_rebase(Repo.open(sandbox.repo), ("main", "missing"))
    assert sandbox.git("rev-parse", "origin/main").stdout.strip() == old_upstream
    with pytest.raises(GitError):
        sync_rebase(Repo.open(sandbox.repo), ("main", "main~1"))
    assert sandbox.git("rev-parse", "origin/main").stdout.strip() == old_upstream


def test_selected_personal_branch_is_not_enrolled(sandbox: Sandbox) -> None:
    sandbox.git("branch", "personal")
    before = local_refs(sandbox)
    with pytest.raises(GitError, match="upstream"):
        sync_rebase(Repo.open(sandbox.repo), ("personal",))
    assert local_refs(sandbox) == before
    assert sandbox.git("config", "--get", "branch.personal.remote", check=False).returncode == 1


def test_missing_upstream_is_reported_without_deleting_local_branch(sandbox: Sandbox) -> None:
    original = sandbox.git("rev-parse", "main").stdout.strip()
    sandbox.git("symbolic-ref", "HEAD", "refs/heads/replacement", path=sandbox.remote)
    sandbox.git("update-ref", "refs/heads/replacement", original, path=sandbox.remote)
    sandbox.git("update-ref", "-d", "refs/heads/main", path=sandbox.remote)
    results = sync_rebase(Repo.open(sandbox.repo))
    assert "missing" in results[0].action or "missing" in results[0].detail.lower()
    assert sandbox.git("rev-parse", "main").stdout.strip() == original
    assert not results[0].backup


@pytest.mark.parametrize("operation", ["pull", "sync"])
def test_unchanged_branches_leave_no_backup(sandbox: Sandbox, operation: str) -> None:
    sandbox.commit("local.txt", "already ahead\n")
    before = local_refs(sandbox)
    repo = Repo.open(sandbox.repo)
    result = pull_rebase(repo) if operation == "pull" else sync_rebase(repo)[0]
    assert not result.backup
    assert local_refs(sandbox) == before


@pytest.mark.parametrize("operation", ["pull", "sync"])
def test_rebase_preserves_ignored_file_collision(sandbox: Sandbox, operation: str) -> None:
    sandbox.git("config", "core.excludesFile", str(sandbox.repo.parent / "ignore"))
    (sandbox.repo.parent / "ignore").write_text("collision.txt\n")
    (sandbox.repo / "collision.txt").write_text("private ignored data\n")
    sandbox.commit("collision.txt", "upstream data\n", path=sandbox.remote)
    before = local_refs(sandbox)
    repo = Repo.open(sandbox.repo)
    if operation == "pull":
        with pytest.raises(GitError, match="ignored"):
            pull_rebase(repo)
    else:
        result = sync_rebase(repo)[0]
        assert result.action == "skipped" and "ignored" in result.detail
    assert (sandbox.repo / "collision.txt").read_text() == "private ignored data\n"
    assert local_refs(sandbox) == before


def test_switch_failure_restores_original_detached_checkout(sandbox: Sandbox) -> None:
    initial = sandbox.git("rev-parse", "HEAD").stdout.strip()
    sandbox.git("switch", "--no-track", "-c", "first")
    configure_tracking(sandbox, "first")
    sandbox.commit("first.txt", "first\n")
    sandbox.git("switch", "--no-track", "-c", "second", "origin/main")
    configure_tracking(sandbox, "second")
    sandbox.commit("collision.txt", "tracked on second\n")
    sandbox.git("switch", "--detach", initial)
    (sandbox.repo / ".git" / "info" / "exclude").write_text("collision.txt\n")
    (sandbox.repo / "collision.txt").write_text("ignored private\n")
    sandbox.commit("upstream.txt", "upstream\n", path=sandbox.remote)
    results = sync_rebase(Repo.open(sandbox.repo), ("first", "second"))
    assert results[0].backup
    assert results[1].action == "skipped" and "ignored" in results[1].detail
    assert sandbox.git("rev-parse", "HEAD").stdout.strip() == initial
    assert sandbox.git("symbolic-ref", "-q", "HEAD", check=False).returncode == 1
    assert (sandbox.repo / "collision.txt").read_text() == "ignored private\n"


def test_normal_git_failure_restores_original_checkout(sandbox: Sandbox) -> None:
    for name in ("first", "second"):
        sandbox.git("switch", "--no-track", "-c", name, "origin/main")
        configure_tracking(sandbox, name)
        sandbox.commit(f"{name}.txt", f"{name}\n")
    sandbox.git("switch", "main")
    hook = sandbox.repo / ".git" / "hooks" / "pre-rebase"
    hook.write_text('#!/bin/sh\nbranch=$(git symbolic-ref --short HEAD)\n[ "$branch" != second ]\n')
    hook.chmod(0o755)
    upstream = sandbox.commit("upstream.txt", "upstream\n", path=sandbox.remote)
    second = sandbox.git("rev-parse", "second").stdout.strip()
    with pytest.raises(GitError, match="second"):
        sync_rebase(Repo.open(sandbox.repo), ("first", "second"))
    assert_preserved(sandbox, upstream, "first.txt", "first\n", ref="first")
    assert sandbox.git("rev-parse", "second").stdout.strip() == second
    assert sandbox.git("symbolic-ref", "HEAD").stdout.strip() == "refs/heads/main"
    assert not Repo.open(sandbox.repo).operation()


@pytest.mark.parametrize("operation", ["pull", "sync"])
def test_rebase_update_refs_configuration_cannot_modify_personal_branches(
    sandbox: Sandbox, operation: str
) -> None:
    original = sandbox.commit("local.txt", "local\n")
    sandbox.git("branch", "personal", original)
    sandbox.git("config", "rebase.updateRefs", "true")
    sandbox.commit("upstream.txt", "upstream\n", path=sandbox.remote)
    repo = Repo.open(sandbox.repo)
    if operation == "pull":
        pull_rebase(repo)
    else:
        sync_rebase(repo)
    assert sandbox.git("rev-parse", "personal").stdout.strip() == original
    assert sandbox.git("rev-parse", "main").stdout.strip() != original


@pytest.mark.parametrize("operation", ["pull", "sync"])
def test_multiple_configured_merge_upstreams_are_rejected_before_fetch(
    sandbox: Sandbox, operation: str
) -> None:
    sandbox.git("config", "--add", "branch.main.merge", "refs/heads/other")
    before = local_refs(sandbox)
    old_remote = sandbox.git("rev-parse", "origin/main").stdout.strip()
    sandbox.commit("upstream.txt", "upstream\n", path=sandbox.remote)
    with pytest.raises(GitError, match="upstream|merge"):
        if operation == "pull":
            pull_rebase(Repo.open(sandbox.repo))
        else:
            sync_rebase(Repo.open(sandbox.repo))
    assert local_refs(sandbox) == before
    assert sandbox.git("rev-parse", "origin/main").stdout.strip() == old_remote


def test_configured_local_upstream_can_be_rebased_without_remote_enrollment(
    sandbox: Sandbox,
) -> None:
    sandbox.git("switch", "--no-track", "-c", "topic")
    sandbox.git("config", "branch.topic.remote", ".")
    sandbox.git("config", "branch.topic.merge", "refs/heads/main")
    sandbox.commit("local.txt", "local\n")
    sandbox.git("switch", "main")
    upstream = sandbox.commit("upstream.txt", "local integration\n")
    sandbox.git("switch", "topic")
    pull_rebase(Repo.open(sandbox.repo))
    assert_preserved(sandbox, upstream, "local.txt", "local\n")
    assert sandbox.git("config", "--get", "branch.topic.remote").stdout.strip() == "."


@pytest.mark.parametrize("operation", ["pull", "sync"])
def test_replayed_add_then_delete_cannot_overwrite_ignored_files(
    sandbox: Sandbox, operation: str
) -> None:
    sandbox.commit("private.txt", "temporarily tracked\n")
    sandbox.git("rm", "private.txt")
    sandbox.git("commit", "-m", "delete private")
    (sandbox.repo / ".git" / "info" / "exclude").write_text("private.txt\n")
    (sandbox.repo / "private.txt").write_text("private ignored data\n")
    sandbox.commit("upstream.txt", "upstream\n", path=sandbox.remote)
    before = local_refs(sandbox)
    repo = Repo.open(sandbox.repo)
    if operation == "pull":
        with pytest.raises(GitError, match="ignored"):
            pull_rebase(repo)
    else:
        result = sync_rebase(repo)[0]
        assert result.action == "skipped" and "ignored" in result.detail
    assert local_refs(sandbox) == before
    assert (sandbox.repo / "private.txt").read_text() == "private ignored data\n"
