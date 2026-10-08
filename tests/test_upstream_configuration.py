import pytest
from conftest import Sandbox


@pytest.mark.parametrize(
    "misconfiguration",
    [
        "unknown-remote",
        "missing-remote",
        "missing-merge",
        "multiple-merges",
        "tab-merge",
        "newline-merge",
    ],
)
@pytest.mark.parametrize("command", [("status",), ("sync",), ("sync", "--strategy", "rebase")])
def test_invalid_upstream_is_not_silently_treated_as_local_only(
    sandbox: Sandbox, command: tuple[str, ...], misconfiguration: str
) -> None:
    original = sandbox.git("rev-parse", "HEAD").stdout.strip()
    if misconfiguration == "unknown-remote":
        sandbox.git("config", "branch.main.remote", "typo")
    elif misconfiguration == "missing-remote":
        sandbox.git("config", "--unset", "branch.main.remote")
    elif misconfiguration == "missing-merge":
        sandbox.git("config", "--unset", "branch.main.merge")
    elif misconfiguration in {"tab-merge", "newline-merge"}:
        separator = "\t" if misconfiguration == "tab-merge" else "\n"
        sandbox.git("config", "branch.main.merge", f"refs/heads/bad{separator}name")
    else:
        sandbox.git("config", "--add", "branch.main.merge", "refs/heads/other")
    sandbox.commit("remote.txt", "remote change\n", path=sandbox.remote)

    result = sandbox.cli(*command)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "Traceback" not in result.stderr
    assert "upstream" in (result.stdout + result.stderr).lower()
    assert sandbox.git("rev-parse", "refs/heads/main").stdout.strip() == original
    assert sandbox.git("rev-parse", "refs/remotes/origin/main").stdout.strip() == original
    assert not (sandbox.repo / "remote.txt").exists()


def test_branch_selection_does_not_inspect_an_unselected_broken_upstream(sandbox: Sandbox) -> None:
    sandbox.git("branch", "unselected")
    sandbox.git("config", "branch.unselected.remote", "missing")
    sandbox.git("config", "branch.unselected.merge", "refs/heads/main")
    target = sandbox.commit("new.txt", "selected update\n", path=sandbox.remote)

    result = sandbox.cli("sync", "--branch", "main", "--branch", "main")

    assert result.returncode == 0, result.stdout + result.stderr
    assert sandbox.git("rev-parse", "main").stdout.strip() == target
    assert sandbox.git("rev-parse", "unselected").stdout.strip() != target
