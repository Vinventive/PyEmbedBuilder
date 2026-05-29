"""
Hardened subprocess execution with timeout, path validation, and audit logging.
"""
from __future__ import annotations

import os
import queue
import shutil
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from ..security import audit


LogCb = Callable[[str], None]

# Maximum subprocess runtime (30 minutes)
DEFAULT_TIMEOUT_S = 1800.0


@dataclass(frozen=True)
class CommandResult:
    args: list[str]
    returncode: int


class CommandCancelled(RuntimeError):
    """Raised when a subprocess is cancelled via a caller-provided event."""


def _resolve_executable(exe_path: str, cwd: Path | None) -> Path:
    """Resolve executable to an absolute path before launching."""
    p = Path(exe_path)
    has_sep = ("\\" in exe_path) or ("/" in exe_path)

    # Explicit path (absolute or relative with directory component).
    if p.is_absolute() or p.parent != Path(".") or has_sep:
        base = cwd.resolve() if (cwd is not None and not p.is_absolute()) else None
        return (base / p).resolve() if base else p.resolve()

    # Bare command name: resolve from PATH now, not at CreateProcess time.
    found = shutil.which(exe_path, path=os.environ.get("PATH", ""))
    if not found:
        raise FileNotFoundError(f"Executable not found on PATH: {exe_path}")
    resolved = Path(found).resolve()

    # Defend against current-directory executable hijacking for bare names.
    cwd_res = cwd.resolve() if cwd is not None else Path.cwd().resolve()
    if resolved.parent == cwd_res:
        raise ValueError(
            f"Refusing to execute bare command from working directory: {resolved}"
        )
    return resolved


def _validate_executable(exe_path: str, allowed_dir: Path | None) -> None:
    """Ensure the executable exists and (optionally) lives inside an allowed dir."""
    p = Path(exe_path).resolve()
    if not p.exists():
        raise FileNotFoundError(f"Executable not found: {exe_path}")
    if not p.is_file():
        raise ValueError(f"Not a regular file: {exe_path}")
    if allowed_dir is not None:
        try:
            p.relative_to(allowed_dir.resolve())
        except ValueError:
            raise ValueError(
                f"Executable {exe_path} is outside the allowed directory "
                f"{allowed_dir}"
            ) from None


def _kill_process_tree(proc: subprocess.Popen[str]) -> None:
    """Best-effort process-tree termination."""
    if proc.poll() is not None:
        return

    if os.name == "nt":
        # taskkill /T terminates child processes as well.
        taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
        taskkill_exe = str(taskkill) if taskkill.exists() else shutil.which("taskkill")
        if not taskkill_exe:
            proc.kill()
            return
        subprocess.run(
            [taskkill_exe, "/PID", str(proc.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            shell=False,
        )
    else:
        proc.kill()

    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()
        proc.wait()


def run_command_stream(
    args: Iterable[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    log_cb: LogCb | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    allowed_exe_dir: Path | None = None,
    cancel_event: threading.Event | None = None,
) -> CommandResult:
    """Run a command, stream output to *log_cb*, and enforce a timeout.

    Security:
    - shell=False always (prevents shell injection)
    - Optional executable path validation (*allowed_exe_dir*)
    - Timeout enforcement (kills the process on expiry)
    - Cooperative cancellation via *cancel_event*
    - Full audit logging of start, success, failure, and timeout
    """
    args_list = [str(a) for a in args]
    if not args_list:
        raise ValueError("No command arguments provided.")

    exe_resolved = _resolve_executable(args_list[0], cwd=cwd)
    args_list[0] = str(exe_resolved)
    _validate_executable(args_list[0], allowed_exe_dir)

    cmd_display = " ".join(args_list)
    cmd_log = cmd_display.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    if len(cmd_log) > 512:
        cmd_log = cmd_log[:509] + "..."
    audit("subprocess_start", command=cmd_log, cwd=str(cwd or "."))
    if log_cb:
        log_cb(f"$ {cmd_display}")

    proc = subprocess.Popen(
        args_list,
        cwd=str(cwd) if cwd else None,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )

    q: queue.Queue[str | None] = queue.Queue()

    def _reader() -> None:
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                q.put(line.rstrip("\n"))
        finally:
            q.put(None)

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()

    start = time.monotonic()
    reader_done = False
    cancelled = False
    timed_out = False
    recent_output: deque[str] = deque(maxlen=30)

    while True:
        while True:
            try:
                item = q.get_nowait()
            except queue.Empty:
                break
            if item is None:
                reader_done = True
                continue
            recent_output.append(item)
            if log_cb:
                log_cb(item)

        if proc.poll() is not None and reader_done:
            break

        if cancel_event is not None and cancel_event.is_set():
            cancelled = True
            _kill_process_tree(proc)
            break

        if (time.monotonic() - start) > timeout_s:
            timed_out = True
            _kill_process_tree(proc)
            break

        time.sleep(0.05)

    reader.join(timeout=0.2)
    while True:
        try:
            item = q.get_nowait()
        except queue.Empty:
            break
        if item is not None and log_cb:
            log_cb(item)
        if item is not None:
            recent_output.append(item)

    if timed_out:
        audit("subprocess_timeout", level="ERROR", command=cmd_log, timeout_s=str(timeout_s))
        raise RuntimeError(
            f"Command timed out after {timeout_s:.0f}s:\n{cmd_display}"
        )

    if cancelled:
        audit("subprocess_cancelled", level="WARNING", command=cmd_log)
        raise CommandCancelled(f"Command cancelled by user:\n{cmd_display}")

    if proc.returncode != 0:
        output_tail = "\n".join(recent_output).strip()
        if not output_tail:
            output_tail = "No process output captured."
        audit(
            "subprocess_failed",
            level="ERROR",
            command=cmd_log,
            exit_code=str(proc.returncode),
        )
        raise RuntimeError(
            f"Command failed (exit code {proc.returncode}):\n{cmd_display}\n\n"
            f"Output tail:\n{output_tail}"
        )

    audit("subprocess_ok", command=cmd_log)
    return CommandResult(args=args_list, returncode=proc.returncode)
