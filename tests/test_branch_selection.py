from conftest import Sandbox

from gtracker.repo import Repo


def test_selected_status_ignores_unavailable_objects_on_unselected_branches(
    sandbox: Sandbox,
) -> None:
    original = sandbox.git("rev-parse", "main").stdout.strip()
    sandbox.git("switch", "-c", "unselected", path=sandbox.remote)
    unavailable = sandbox.commit("other.txt", "unselected history\n", path=sandbox.remote)
    sandbox.git("fetch", "origin")
    sandbox.git("branch", "--track", "unselected", "origin/unselected")
    object_path = sandbox.repo / ".git" / "objects" / unavailable[:2] / unavailable[2:]
    object_path.unlink()

    result = sandbox.cli("status", "--branch", "main")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "unselected" not in result.stdout
    assert sandbox.git("rev-parse", "main").stdout.strip() == original
    assert sandbox.git("rev-parse", "unselected").stdout.strip() == unavailable


def test_selection_deduplicates_exact_names_without_selecting_prefixes(sandbox: Sandbox) -> None:
    sandbox.git("branch", "topic")
    sandbox.git("branch", "topic-extra")

    selected = Repo.open(sandbox.repo).branches(("topic", "main", "topic"))

    assert tuple(branch.name for branch in selected) == ("topic", "main")
