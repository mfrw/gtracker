from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest


@dataclass(frozen=True)
class Sandbox:
    remote: Path
    repo: Path

    def git(
        self, *args: str, path: Path | None = None, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(path or self.repo), *args],
            text=True,
            capture_output=True,
            check=check,
        )

    def commit(self, filename: str, text: str, *, path: Path | None = None) -> str:
        root = path or self.repo
        (root / filename).write_text(text)
        self.git("add", "--", filename, path=root)
        self.git("commit", "-m", filename, path=root)
        return self.git("rev-parse", "HEAD", path=root).stdout.strip()

    def cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "gtracker", *args, "--repo", str(self.repo)],
            text=True,
            capture_output=True,
        )


@pytest.fixture(autouse=True)
def isolated_git(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for key in tuple(os.environ):
        if key.startswith("GIT_"):
            monkeypatch.delenv(key)
    home = tmp_path / "home"
    config = tmp_path / "xdg-config"
    home.mkdir()
    config.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test User")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test User")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.invalid")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")


@pytest.fixture
def sandbox(tmp_path: Path) -> Sandbox:
    remote, repo = tmp_path / "remote", tmp_path / "repo"
    subprocess.run(["git", "init", "-b", "main", str(remote)], check=True, capture_output=True)
    box = Sandbox(remote, repo)
    box.commit("base.txt", "base\n", path=remote)
    box.git("clone", str(remote), str(repo), path=tmp_path)
    return box
