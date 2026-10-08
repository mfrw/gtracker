from pathlib import Path

from conftest import Sandbox

from gtracker.mirror import sync_mirror
from gtracker.repo import Repo


def test_git_recursion_configuration_cannot_overwrite_ignored_submodule_files(
    sandbox: Sandbox,
    tmp_path: Path,
) -> None:
    module_remote = tmp_path / "module-remote"
    sandbox.git("init", "-b", "main", str(module_remote))
    sandbox.commit("base.txt", "module base", path=module_remote)
    sandbox.commit(".gitignore", "artifact\n", path=module_remote)
    sandbox.git("submodule", "add", str(module_remote), "module", path=sandbox.remote)
    sandbox.git("commit", "-m", "add module", path=sandbox.remote)
    sandbox.git("pull", "--ff-only")
    sandbox.git("submodule", "update", "--init")
    initialized = sandbox.repo / "module"
    old_module = sandbox.git("rev-parse", "HEAD", path=initialized).stdout.strip()
    (initialized / "artifact").write_text("private submodule build output")
    (module_remote / "artifact").write_text("new tracked file")
    sandbox.git("add", "-f", "artifact", path=module_remote)
    sandbox.git("commit", "-m", "track artifact", path=module_remote)
    new_module = sandbox.git("rev-parse", "HEAD", path=module_remote).stdout.strip()
    remote_module = sandbox.remote / "module"
    sandbox.git("fetch", "origin", path=remote_module)
    sandbox.git("reset", "--hard", new_module, path=remote_module)
    sandbox.git("add", "module", path=sandbox.remote)
    sandbox.git("commit", "-m", "advance module", path=sandbox.remote)
    target = sandbox.git("rev-parse", "HEAD", path=sandbox.remote).stdout.strip()
    sandbox.git("config", "submodule.recurse", "true")
    sync_mirror(Repo.open(sandbox.repo))
    assert sandbox.git("rev-parse", "HEAD").stdout.strip() == target
    assert sandbox.git("rev-parse", "HEAD", path=initialized).stdout.strip() == old_module
    assert (initialized / "artifact").read_text() == "private submodule build output"
