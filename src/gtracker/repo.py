from __future__ import annotations

import fcntl
import os
import stat
import subprocess
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


class GitError(RuntimeError):
    """An unsafe operation or failed Git command."""


@dataclass(frozen=True)
class Branch:
    name: str
    ref: str
    oid: str
    upstream: str
    remote: str
    remote_ref: str
    upstream_oid: str | None
    ahead: int = 0
    behind: int = 0


@dataclass(frozen=True)
class Worktree:
    path: Path
    branch: str | None
    locked: bool = False


@dataclass(frozen=True)
class Result:
    branch: str
    action: str
    detail: str = ""
    backup: str | None = None


class UpdateError(GitError):
    """A stopped batch with completed updates available for recovery reporting."""

    def __init__(self, message: str, results: tuple[Result, ...] = ()) -> None:
        self.results = results
        super().__init__(message)


@dataclass(frozen=True)
class Repo:
    path: Path
    offline: bool = False

    @classmethod
    def open(cls, path: str | Path, *, offline: bool = False) -> Repo:
        candidate = cls(Path(path).expanduser().resolve(), offline)
        if candidate.git("rev-parse", "--is-bare-repository").stdout.strip() == "true":
            raise GitError("gtracker requires a non-bare repository")
        root = candidate.git("rev-parse", "--show-toplevel").stdout.removesuffix("\n")
        return cls(Path(root), offline)

    def git(
        self, *args: str, check: bool = True, input: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        env = {
            key: value
            for key, value in os.environ.items()
            if key
            not in {
                "GIT_DIR",
                "GIT_WORK_TREE",
                "GIT_COMMON_DIR",
                "GIT_INDEX_FILE",
                "GIT_PREFIX",
            }
        }
        env = {**env, "LC_ALL": "C", "GIT_OPTIONAL_LOCKS": "0"}
        if self.offline:
            env = {**env, "GIT_NO_LAZY_FETCH": "1"}
        try:
            raw = subprocess.run(
                ["git", "--no-pager", "-c", "submodule.recurse=false", "-C", str(self.path), *args],
                input=os.fsencode(input) if input is not None else None,
                capture_output=True,
                env=env,
            )
            result = subprocess.CompletedProcess(
                raw.args, raw.returncode, os.fsdecode(raw.stdout), os.fsdecode(raw.stderr)
            )
        except OSError as exc:
            raise GitError(f"Cannot run Git: {exc}") from exc
        if check and result.returncode:
            raise GitError(result.stderr.strip() or result.stdout.strip() or "Git command failed")
        return result

    def common_dir(self) -> Path:
        return Path(
            self.git("rev-parse", "--path-format=absolute", "--git-common-dir").stdout.removesuffix(
                "\n"
            )
        )

    def git_path(self, name: str) -> Path:
        return Path(
            self.git("rev-parse", "--path-format=absolute", "--git-path", name).stdout.removesuffix(
                "\n"
            )
        )

    def current_branch(self) -> str | None:
        result = self.git("symbolic-ref", "-q", "HEAD", check=False)
        return result.stdout.removesuffix("\n") if result.returncode == 0 else None

    def oid(self, ref: str) -> str | None:
        result = self.git(
            "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}", check=False
        )
        if self.offline and result.returncode:
            raw = self.git("rev-parse", "--verify", "--end-of-options", ref, check=False)
            if raw.returncode == 0:
                raise GitError(
                    f"Commit object unavailable locally: {ref}; fetch explicitly to hydrate it"
                )
        return result.stdout.strip() if result.returncode == 0 else None

    def worktrees(self) -> tuple[Worktree, ...]:
        fields = self.git("worktree", "list", "--porcelain", "-z").stdout.split("\0\0")
        return tuple(self._worktree(block) for block in fields if block.strip("\0"))

    @staticmethod
    def _worktree(block: str) -> Worktree:
        rows = block.strip("\0").split("\0")
        path = next(row.removeprefix("worktree ") for row in rows if row.startswith("worktree "))
        branch = next(
            (row.removeprefix("branch ") for row in rows if row.startswith("branch ")), None
        )
        return Worktree(Path(path), branch, any(row.startswith("locked") for row in rows))

    def branches(self) -> tuple[Branch, ...]:
        fmt = (
            "%(refname)%09%(objectname)%09%(upstream)%09"
            "%(upstream:remotename)%09%(upstream:remoteref)"
        )
        output = self.git("for-each-ref", f"--format={fmt}", "refs/heads/").stdout
        return tuple(self._branch(row) for row in output.split("\n") if row)

    def _branch(self, row: str) -> Branch:
        ref, oid, upstream, remote, remote_ref = row.split("\t")
        target = self.oid(upstream) if upstream else None
        counts = (
            self.git("rev-list", "--left-right", "--count", f"{oid}...{target}").stdout.split()
            if target
            else ("0", "0")
        )
        return Branch(
            ref.removeprefix("refs/heads/"),
            ref,
            oid,
            upstream,
            remote,
            remote_ref,
            target,
            int(counts[0]),
            int(counts[1]),
        )

    def branch(self, name: str) -> Branch:
        found = next((branch for branch in self.branches() if branch.name == name), None)
        if found is None:
            raise GitError(f"Unknown local branch: {name}")
        return found

    def operation(self) -> str | None:
        names = (
            "rebase-merge",
            "rebase-apply",
            "MERGE_HEAD",
            "CHERRY_PICK_HEAD",
            "REVERT_HEAD",
            "sequencer",
            "BISECT_LOG",
        )
        return next((name for name in names if self.git_path(name).exists()), None)

    def ensure_no_operations(self) -> None:
        for tree in self.worktrees():
            if not tree.path.is_dir():
                raise GitError(f"Worktree is unavailable: {tree.path}; repair/prune it manually")
            operation = Repo(tree.path).operation()
            if operation:
                raise GitError(f"Git operation in progress at {tree.path}: {operation}")

    def ensure_clean(self, target: str | None = None) -> None:
        operation = self.operation()
        if operation:
            raise GitError(f"Git operation in progress at {self.path}: {operation}")
        for ref in ("ORIG_HEAD", "REBASE_HEAD", "AUTO_MERGE"):
            if self.git("symbolic-ref", "-q", ref, check=False).returncode == 0:
                raise GitError(f"Refusing symbolic {ref} in worktree: {self.path}")
        entries = self.git("ls-files", "-v", "-z").stdout.split("\0")
        if any(entry and (entry[0].islower() or entry[0] == "S") for entry in entries):
            raise GitError(f"Worktree uses assume-unchanged/skip-worktree index flags: {self.path}")
        if self.git(
            "status", "--porcelain", "-z", "--untracked-files=normal", "--ignore-submodules=none"
        ).stdout:
            raise GitError(f"Worktree has uncommitted or untracked changes: {self.path}")
        if target:
            self._protect_ignored(target)

    def _protect_ignored(self, target: str) -> None:
        paths = self.git(
            "diff", "--no-renames", "--name-only", "--diff-filter=A", "-z", "HEAD", target, "--"
        ).stdout.split("\0")
        self.protect_paths(filter(None, paths))

    def protect_paths(self, paths: Iterable[str]) -> None:
        added = frozenset(paths)
        if not added:
            return
        output = self.git("ls-files", "--others", "--ignored", "--exclude-standard", "-z").stdout
        ignored = frozenset(filter(None, output.split("\0")))
        for path in ignored:
            parts = path.split("/")
            if any("/".join(parts[:i]) in added for i in range(1, len(parts) + 1)):
                raise GitError(f"Update would overwrite ignored file: {self.path / path}")
        for path in added:
            parts = path.split("/")
            if any("/".join(parts[:i]) in ignored for i in range(1, len(parts))):
                raise GitError(f"Update would replace an ignored path: {self.path / path}")

    def ensure_safe_fetch(self) -> None:
        rows = self.git("config", "--null", "--get-regexp", r"^remote\..*\.fetch$", check=False)
        if rows.returncode not in (0, 1):
            raise GitError(rows.stderr.strip() or "Cannot read remote fetch mappings")
        for row in filter(None, rows.stdout.split("\0")):
            key, _, spec = row.partition("\n")
            spec = spec.removeprefix("+")
            if spec.startswith("^") or ":" not in spec:
                continue
            destination = spec.split(":", 1)[1]
            if destination and not destination.startswith(("refs/remotes/", "refs/tags/")):
                raise GitError(f"Unsafe fetch destination in {key}: {destination}")
            if destination and "*" not in destination:
                self._ensure_safe_destination(destination)
        symrefs = self.git(
            "for-each-ref", "--format=%(refname)%09%(symref)", "refs/remotes/", "refs/tags/"
        ).stdout
        for row in symrefs.splitlines():
            ref, target = row.split("\t")
            if target:
                self._ensure_safe_destination(ref)

    def _ensure_safe_destination(self, ref: str) -> None:
        result = self.git("symbolic-ref", "-q", ref, check=False)
        if result.returncode == 0 and not result.stdout.strip().startswith(
            ("refs/remotes/", "refs/tags/")
        ):
            raise GitError(f"Fetch destination dereferences outside safe namespaces: {ref}")

    def fetch_all(self) -> None:
        self.ensure_safe_fetch()
        self.git("fetch", "--all", "--prune")


@contextmanager
def locked(repo: Repo) -> Iterator[None]:
    path = repo.common_dir() / "gtracker.lock"
    flags = os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "r+") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise GitError(f"Lock path is not a regular file: {path}")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise GitError("Another gtracker mutation is running for this repository") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
