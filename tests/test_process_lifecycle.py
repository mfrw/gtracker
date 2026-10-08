from __future__ import annotations

import errno
import os
import pty
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import Sandbox


def _run_terminal(
    sandbox: Sandbox,
    response: bytes,
    *,
    args: tuple[str, ...] = ("sync",),
    timeout: float = 20,
) -> tuple[int, bytes]:
    pid, terminal = pty.fork()
    if pid == 0:
        os.execv(
            sys.executable,
            [sys.executable, "-m", "gtracker", *args, "--repo", str(sandbox.repo)],
        )
    output = b""
    answered = False
    deadline = time.monotonic() + timeout
    try:
        while True:
            remaining = deadline - time.monotonic()
            assert remaining > 0 and select.select([terminal], [], [], remaining)[0]
            try:
                chunk = os.read(terminal, 65536)
            except OSError as exc:
                if exc.errno != errno.EIO:
                    raise
                break
            if not chunk:
                break
            output += chunk
            if b"TRANSPORT_RESPONSE?" in output and not answered:
                os.write(terminal, response)
                answered = True
        _, status = os.waitpid(pid, 0)
        pid = 0
        return os.waitstatus_to_exitcode(status), output
    finally:
        if pid:
            os.kill(pid, signal.SIGINT)
            os.waitpid(pid, 0)
        os.close(terminal)


@pytest.mark.parametrize("interrupt", [False, True])
def test_fetch_retains_controlling_terminal_and_handles_terminal_interrupt(
    sandbox: Sandbox, tmp_path: Path, interrupt: bool
) -> None:
    original = sandbox.git("rev-parse", "main").stdout.strip()
    target = sandbox.commit("new.txt", "upstream\n", path=sandbox.remote)
    helper = tmp_path / "interactive-upload-pack"
    helper.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "terminal = os.open('/dev/tty', os.O_RDWR)\n"
        "os.write(terminal, b'TRANSPORT_RESPONSE?\\n')\n"
        "assert os.read(terminal, 100).strip() == b'yes'\n"
        "os.close(terminal)\n"
        "os.execvp('git', ['git', 'upload-pack', *sys.argv[1:]])\n"
    )
    helper.chmod(0o755)
    sandbox.git("config", "remote.origin.uploadpack", str(helper))

    code, output = _run_terminal(sandbox, b"\x03" if interrupt else b"yes\n")

    assert b"TRANSPORT_RESPONSE?" in output, output.decode(errors="replace")
    assert code == (130 if interrupt else 0), output.decode(errors="replace")
    expected = original if interrupt else target
    assert sandbox.git("rev-parse", "main").stdout.strip() == expected
    assert (sandbox.repo / "new.txt").exists() is not interrupt


def test_terminal_interrupt_terminates_hook_that_keeps_captured_pipes_open(
    sandbox: Sandbox,
) -> None:
    original = sandbox.commit("local.txt", "local work\n")
    sandbox.commit("upstream.txt", "upstream\n", path=sandbox.remote)
    hook = sandbox.repo / ".git" / "hooks" / "pre-rebase"
    hook.write_text(
        f"#!{sys.executable}\n"
        "import os, select, signal\n"
        "from pathlib import Path\n"
        "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
        "terminal = os.open('/dev/tty', os.O_RDWR)\n"
        "os.write(terminal, b'TRANSPORT_RESPONSE?\\n')\n"
        "select.select([], [], [], 12)\n"
        "Path('late-hook-write').write_text('unexpected mutation')\n"
    )
    hook.chmod(0o755)

    code, output = _run_terminal(sandbox, b"\x03", args=("pull", "--rebase"), timeout=8)

    assert code == 130, output.decode(errors="replace")
    assert sandbox.git("rev-parse", "HEAD").stdout.strip() == original
    assert not (sandbox.repo / "late-hook-write").exists()
    assert sandbox.git("status", "--porcelain").stdout == ""


def test_redirected_sigint_ignoring_hook_cannot_write_after_cli_exit(
    sandbox: Sandbox, tmp_path: Path
) -> None:
    sandbox.git("switch", "--no-track", "-c", "topic")
    sandbox.git("branch", "--set-upstream-to", "origin/main", "topic")
    sandbox.commit("local.txt", "local\n")
    sandbox.git("switch", "main")
    sandbox.commit("upstream.txt", "upstream\n", path=sandbox.remote)
    ready, release = tmp_path / "ready", tmp_path / "release"
    os.mkfifo(ready)
    os.mkfifo(release)
    marker = sandbox.repo / "orphan-mutation"
    hook = sandbox.repo / ".git" / "hooks" / "pre-rebase"
    hook.write_text(
        f"#!{sys.executable}\n"
        "import os, select, signal\n"
        "from pathlib import Path\n"
        "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
        "null = os.open(os.devnull, os.O_RDWR)\n"
        "for fd in (0, 1, 2): os.dup2(null, fd)\n"
        f"gate = os.open({str(release)!r}, os.O_RDONLY | os.O_NONBLOCK)\n"
        f"Path({str(ready)!r}).write_text(str(os.getpid()))\n"
        "if select.select([gate], [], [], 10)[0] and os.read(gate, 100):\n"
        f"    Path({str(marker)!r}).write_text('late mutation')\n"
    )
    hook.chmod(0o755)
    ready_fd = os.open(ready, os.O_RDWR | os.O_NONBLOCK)
    release_fd = os.open(release, os.O_RDWR | os.O_NONBLOCK)
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "gtracker",
            "sync",
            "--strategy",
            "rebase",
            "--branch",
            "topic",
            "--repo",
            str(sandbox.repo),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    hook_pid = None
    try:
        assert select.select([ready_fd], [], [], 10)[0], "hook did not start"
        hook_pid = int(os.read(ready_fd, 100))
        process.send_signal(signal.SIGINT)
        stdout, stderr = process.communicate(timeout=15)
        assert process.returncode == 130, stdout + stderr
        os.write(release_fd, b"release")
        with pytest.raises(ProcessLookupError):
            os.kill(hook_pid, 0)
        assert not marker.exists()
        assert sandbox.git("symbolic-ref", "HEAD").stdout.strip() == "refs/heads/main"
        assert sandbox.git("status", "--porcelain").stdout == ""
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
            process.communicate(timeout=10)
        if hook_pid:
            try:
                os.kill(hook_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        os.close(ready_fd)
        os.close(release_fd)
