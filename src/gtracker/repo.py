from __future__ import annotations

import errno
import fcntl
import os
import signal
import stat
import subprocess
import time
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
    upstream_error: str = ""


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


class UpdateInterrupted(KeyboardInterrupt):
    """An interrupted update with completed results and recovery information."""

    def __init__(
        self,
        message: str,
        results: tuple[Result, ...] = (),
        *,
        recovery_safe: bool = True,
    ) -> None:
        self.results = results
        self.recovery_safe = recovery_safe
        super().__init__(message)


@dataclass(frozen=True)
class FetchedRef:
    remote: str
    ref: str
    oid: str

    @classmethod
    def parse(cls, remote: str, row: str) -> FetchedRef | None:
        fields = row[2:].split()
        if len(fields) != 3 or row[0] not in " +-*t=":
            raise GitError(f"Cannot interpret fetched refs from remote {remote}: {row}")
        return cls(remote, fields[2], fields[1]) if row[0] != "-" else None


@dataclass(frozen=True)
class FetchReport:
    remotes: frozenset[str]
    refs: tuple[FetchedRef, ...]

    def oid(self, remote: str, ref: str) -> str | None:
        return next(
            (item.oid for item in self.refs if item.remote == remote and item.ref == ref), None
        )


_GIT_INTERRUPT_GRACE = 5.0
_GIT_GROUP_EXIT_GRACE = 1.0
_GIT_GROUP_POLL_INTERVAL = 0.01
_GIT_SIGNAL_POLL_INTERVAL = 0.1


def _signal_git(process: subprocess.Popen[bytes], signum: int) -> None:
    try:
        os.killpg(process.pid, signum)
    except ProcessLookupError:
        pass  # Git and its process group have already exited.


def _git_group_exists(process: subprocess.Popen[bytes]) -> bool:
    try:
        os.killpg(process.pid, 0)
        return True
    except ProcessLookupError:
        return False


def _stop_git(process: subprocess.Popen[bytes]) -> None:
    previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        _signal_git(process, signal.SIGINT)
        try:
            process.communicate(timeout=_GIT_INTERRUPT_GRACE)
        except subprocess.TimeoutExpired:
            pass  # Force termination below, including hooks that closed their pipes.
        _signal_git(process, signal.SIGKILL)
        process.communicate()
        deadline = time.monotonic() + _GIT_GROUP_EXIT_GRACE
        while _git_group_exists(process) and time.monotonic() < deadline:
            time.sleep(_GIT_GROUP_POLL_INTERVAL)
        if _git_group_exists(process):
            raise UpdateInterrupted(
                "Git process group has not exited; automatic checkout recovery was disabled. "
                "Inspect Git processes and worktree state before retrying.",
                recovery_safe=False,
            )
    finally:
        signal.signal(signal.SIGINT, previous)


def _set_foreground(terminal: int, group: int) -> None:
    blocked = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTTOU})
    try:
        os.tcsetpgrp(terminal, group)
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, blocked)


@contextmanager
def _git_foreground(process: subprocess.Popen[bytes]) -> Iterator[None]:
    try:
        terminal = os.open("/dev/tty", os.O_RDWR | os.O_CLOEXEC)
    except OSError as exc:
        if exc.errno not in {errno.ENXIO, errno.ENODEV, errno.ENOTTY, errno.ENOENT}:
            raise
        yield
        return
    original: int | None = None
    try:
        if os.tcgetpgrp(terminal) == os.getpgrp():
            original = os.getpgrp()
            _set_foreground(terminal, process.pid)
            # Git may have attempted a terminal read before the foreground handoff.
            _signal_git(process, signal.SIGCONT)
        yield
    finally:
        try:
            if original is not None:
                _set_foreground(terminal, original)
        finally:
            os.close(terminal)


def _communicate_git(
    process: subprocess.Popen[bytes], input: bytes | None
) -> tuple[bytes, bytes | None]:
    try:
        with _git_foreground(process):
            output: tuple[bytes, bytes | None] | None = None
            while output is None:
                try:
                    output = process.communicate(input, timeout=_GIT_SIGNAL_POLL_INTERVAL)
                except subprocess.TimeoutExpired:
                    input = None
                # A surviving hook can hold pipes open after terminal Ctrl-C kills Git.
                if process.poll() in (-signal.SIGINT, 128 + signal.SIGINT):
                    raise KeyboardInterrupt
            return output
    except (KeyboardInterrupt, OSError):
        _stop_git(process)
        raise


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
        self,
        *args: str,
        check: bool = True,
        input: str | None = None,
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
            with subprocess.Popen(
                ["git", "--no-pager", "-c", "submodule.recurse=false", "-C", str(self.path), *args],
                stdin=subprocess.PIPE if input is not None else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                process_group=0,
            ) as process:
                stdout, stderr = _communicate_git(
                    process, os.fsencode(input) if input is not None else None
                )
                result = subprocess.CompletedProcess(
                    process.args,
                    process.returncode,
                    os.fsdecode(stdout),
                    os.fsdecode(stderr or b""),
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

    def config_values(self, pattern: str, *, boolean: bool = False) -> tuple[tuple[str, str], ...]:
        options = ("--type=bool",) if boolean else ()
        result = self.git("config", "--null", *options, "--get-regexp", pattern, check=False)
        if result.returncode not in (0, 1):
            raise GitError(result.stderr.strip() or "Cannot read Git configuration")
        return tuple(
            (key, value)
            for row in result.stdout.split("\0")
            if row
            for key, _, value in (row.partition("\n"),)
        )

    def branches(self) -> tuple[Branch, ...]:
        fmt = (
            "%(refname)%09%(objectname)%09%(upstream)%09"
            "%(upstream:remotename)%09%(upstream:remoteref)"
        )
        output = self.git("for-each-ref", f"--format={fmt}", "refs/heads/").stdout
        config = self.config_values(r"^branch\..*\.(remote|merge)$")
        remotes = frozenset(self.git("remote").stdout.splitlines())
        return tuple(self._branch(row, config, remotes) for row in output.split("\n") if row)

    def _upstream_error(
        self,
        name: str,
        upstream: str,
        remote: str,
        remote_ref: str,
        config: tuple[tuple[str, str], ...],
        remotes: frozenset[str],
    ) -> str:
        configured = tuple(value for key, value in config if key == f"branch.{name}.remote")
        merges = tuple(value for key, value in config if key == f"branch.{name}.merge")
        if not configured and not merges:
            return ""
        if len(configured) != 1 or not configured[0]:
            return "upstream requires exactly one configured branch remote"
        if len(merges) != 1 or not merges[0]:
            return "upstream requires exactly one configured merge branch"
        if configured[0] != "." and configured[0] not in remotes:
            return f"unknown configured upstream remote: {configured[0]}"
        if (
            not merges[0].startswith("refs/heads/")
            or self.git("check-ref-format", merges[0], check=False).returncode
        ):
            return f"invalid configured upstream branch: {merges[0]}"
        if not upstream or remote != configured[0] or remote_ref != merges[0]:
            return "cannot resolve configured upstream; check the remote fetch mapping"
        return ""

    def _branch(
        self,
        row: str,
        config: tuple[tuple[str, str], ...],
        remotes: frozenset[str],
    ) -> Branch:
        ref, oid, upstream, remote, remote_ref = row.split("\t")
        name = ref.removeprefix("refs/heads/")
        error = self._upstream_error(name, upstream, remote, remote_ref, config, remotes)
        target = self.oid(upstream) if upstream and not error else None
        counts = (
            self.git("rev-list", "--left-right", "--count", f"{oid}...{target}").stdout.split()
            if target
            else ("0", "0")
        )
        return Branch(
            name,
            ref,
            oid,
            upstream,
            remote,
            remote_ref,
            target,
            int(counts[0]),
            int(counts[1]),
            error,
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

    def _fetch_remote(self, remote: str) -> tuple[FetchedRef, ...]:
        result = self.git("fetch", "--no-all", "--prune", "--porcelain", "--verbose", "--", remote)
        return tuple(
            ref
            for row in result.stdout.splitlines()
            if (ref := FetchedRef.parse(remote, row)) is not None
        )

    def fetch_all(self) -> FetchReport:
        try:
            self.ensure_safe_fetch()
            remotes = tuple(self.git("remote").stdout.splitlines())
            skipped = {
                key.rsplit(".", 1)[0].removeprefix("remote."): value == "true"
                for key, value in self.config_values(
                    r"^remote\..*\.(skipfetchall|skipdefaultupdate)$", boolean=True
                )
            }
            selected = tuple(remote for remote in remotes if not skipped.get(remote, False))
            fetched = tuple(self._fetch_remote(remote) for remote in selected)
            return FetchReport(frozenset(selected), tuple(ref for refs in fetched for ref in refs))
        except KeyboardInterrupt as exc:
            detail = f"\n{exc}" if str(exc) else ""
            safe = exc.recovery_safe if isinstance(exc, UpdateInterrupted) else True
            raise UpdateInterrupted(
                "Interrupted during fetch; no local branches were updated. "
                f"Remote-tracking refs may have changed.{detail}",
                recovery_safe=safe,
            ) from exc


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
