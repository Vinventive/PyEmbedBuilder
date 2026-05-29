"""Windows shortcut creation for built portable environments."""
from __future__ import annotations

import os
import re
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..security import audit
from .subprocess_runner import run_command_stream


LogCb = Callable[[str], None]

_INVALID_SHORTCUT_CHARS_RE = re.compile(r'[<>:"/\\|?*]+')


def _ps_string(value: str) -> str:
    """Return *value* as a PowerShell single-quoted string literal."""
    return "'" + value.replace("'", "''") + "'"


def _build_shortcut_script(
    *,
    shortcut_path: Path,
    target_path: Path,
    arguments: str,
    working_directory: Path,
    description: str,
    icon_path: Path,
) -> str:
    icon_location = str(icon_path) if icon_path.exists() else ""
    return (
        "$ErrorActionPreference='Stop';"
        f"$ShortcutPath={_ps_string(str(shortcut_path))};"
        f"$TargetPath={_ps_string(str(target_path))};"
        f"$Arguments={_ps_string(arguments)};"
        f"$WorkingDirectory={_ps_string(str(working_directory))};"
        f"$Description={_ps_string(description)};"
        f"$IconLocation={_ps_string(icon_location)};"
        "$Parent=Split-Path -Parent $ShortcutPath;"
        "if ($Parent) { New-Item -ItemType Directory -Force -Path $Parent | Out-Null };"
        "$Shell=New-Object -ComObject WScript.Shell;"
        "$Shortcut=$Shell.CreateShortcut($ShortcutPath);"
        "$Shortcut.TargetPath=$TargetPath;"
        "$Shortcut.Arguments=$Arguments;"
        "$Shortcut.WorkingDirectory=$WorkingDirectory;"
        "$Shortcut.Description=$Description;"
        "if ($IconLocation) { $Shortcut.IconLocation=$IconLocation };"
        "$Shortcut.Save();"
    )


@dataclass(frozen=True)
class ShortcutResult:
    desktop_shortcut: Path | None = None
    start_menu_shortcut: Path | None = None
    skipped_reason: str = ""

    @property
    def created_count(self) -> int:
        return int(self.desktop_shortcut is not None) + int(
            self.start_menu_shortcut is not None
        )


def _shortcut_filename(app_name: str) -> str:
    name = _INVALID_SHORTCUT_CHARS_RE.sub("-", app_name).strip(" .")
    if not name:
        name = "PyEmbedBuilder App"
    return f"{name}.lnk"


def _user_shell_folder(value_name: str, fallback: Path) -> Path:
    if sys.platform != "win32":
        return fallback
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
        ) as key:
            raw, _kind = winreg.QueryValueEx(key, value_name)
        if isinstance(raw, str) and raw.strip():
            return Path(os.path.expandvars(raw))
    except Exception:
        pass
    return fallback


def _desktop_dir() -> Path:
    return _user_shell_folder("Desktop", Path.home() / "Desktop")


def _start_menu_programs_dir() -> Path:
    appdata = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    fallback = appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    return _user_shell_folder("Programs", fallback)


def _powershell_exe() -> str:
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    bundled = (
        system_root
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    if bundled.exists():
        return str(bundled)
    return "powershell.exe"


def _create_shortcut(
    *,
    shortcut_path: Path,
    target_path: Path,
    arguments: str,
    working_directory: Path,
    description: str,
    icon_path: Path,
    log_cb: LogCb,
    cancel_event: threading.Event | None,
) -> Path:
    run_command_stream(
        [
            _powershell_exe(),
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            _build_shortcut_script(
                shortcut_path=shortcut_path,
                target_path=target_path,
                arguments=arguments,
                working_directory=working_directory,
                description=description,
                icon_path=icon_path,
            ),
        ],
        log_cb=log_cb,
        timeout_s=60.0,
        cancel_event=cancel_event,
    )
    audit("shortcut_created", path=str(shortcut_path), target=str(target_path))
    log_cb(f"Created shortcut: {shortcut_path}")
    return shortcut_path


def create_windows_launch_shortcuts(
    *,
    env_name: str,
    env_dir: Path,
    py_root: Path,
    entry_point_rel: str,
    window_only: bool,
    desktop: bool,
    start_menu: bool,
    log_cb: LogCb,
    cancel_event: threading.Event | None = None,
) -> ShortcutResult:
    """Create current-user Windows shortcuts for the selected launch mode."""
    if not desktop and not start_menu:
        return ShortcutResult(skipped_reason="not requested")
    if sys.platform != "win32":
        reason = "Windows shortcuts are only available on Windows."
        audit("shortcut_skipped", level="WARNING", reason=reason)
        log_cb(reason)
        return ShortcutResult(skipped_reason=reason)

    target_exe = "pythonw.exe" if window_only else "python.exe"
    target_path = py_root / target_exe
    if not target_path.exists():
        raise FileNotFoundError(f"{target_exe} not found: {target_path}")

    entry_path = (env_dir / Path(entry_point_rel.replace("\\", "/"))).resolve()
    try:
        entry_path.relative_to(env_dir.resolve())
    except ValueError as exc:
        raise ValueError(
            f"Entry point escapes environment folder: {entry_point_rel}"
        ) from exc
    if not entry_path.exists() or not entry_path.is_file():
        raise FileNotFoundError(f"Entry point not found for shortcut: {entry_path}")

    shortcut_name = _shortcut_filename(env_name)
    description = f"Launch {env_name}"
    arguments = f'"{entry_path}"'
    icon_path = target_path

    desktop_path: Path | None = None
    start_menu_path: Path | None = None

    if desktop:
        desktop_path = _create_shortcut(
            shortcut_path=_desktop_dir() / shortcut_name,
            target_path=target_path,
            arguments=arguments,
            working_directory=env_dir,
            description=description,
            icon_path=icon_path,
            log_cb=log_cb,
            cancel_event=cancel_event,
        )

    if start_menu:
        start_menu_path = _create_shortcut(
            shortcut_path=_start_menu_programs_dir() / shortcut_name,
            target_path=target_path,
            arguments=arguments,
            working_directory=env_dir,
            description=description,
            icon_path=icon_path,
            log_cb=log_cb,
            cancel_event=cancel_event,
        )

    return ShortcutResult(
        desktop_shortcut=desktop_path,
        start_menu_shortcut=start_menu_path,
    )
