from conftest import Sandbox


def test_explicit_pull_rebases_local_work_without_enrolling_branch(sandbox: Sandbox) -> None:
    sandbox.git("switch", "-c", "personal")
    sandbox.commit("local.txt", "my work\n")
    upstream = sandbox.commit("upstream.txt", "new upstream\n", path=sandbox.remote)
    result = sandbox.cli("pull", "--rebase", "--upstream", "origin/main")
    assert result.returncode == 0, result.stderr
    sandbox.git("merge-base", "--is-ancestor", upstream, "HEAD")
    assert (sandbox.repo / "local.txt").read_text() == "my work\n"
    assert (sandbox.repo / "upstream.txt").read_text() == "new upstream\n"
    assert sandbox.git("config", "--get", "branch.personal.remote", check=False).returncode == 1
