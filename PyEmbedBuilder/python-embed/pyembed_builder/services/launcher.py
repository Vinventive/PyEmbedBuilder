"""
Launcher (.bat) creation for embedded Python environments.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..security import audit


@dataclass(frozen=True)
class LauncherResult:
    deps_bat: Path
    list_bat: Path
    uninstall_bat: Path
    launch_bat: Path
    update_entry_point_bat: Path

def _build_file_launch_bat(*, py_rel_win: str, entry_rel: str, window_only: bool) -> str:
    """Return launch.bat body for direct .py entry-point launches."""
    if window_only:
        return (
            "@echo off\r\n"
            "setlocal EnableExtensions\r\n"
            'set "ROOT=%~dp0"\r\n'
            'pushd "%ROOT%"\r\n'
            f'if not exist ".\\{entry_rel}" (\r\n'
            f'    echo Entry point not found: {entry_rel}\r\n'
            '    echo Run update_entry_point.bat to select a valid .py file.\r\n'
            "    popd\r\n"
            "    endlocal\r\n"
            "    exit /b 1\r\n"
            ")\r\n"
            f'start "" ".\\{py_rel_win}\\pythonw.exe" ".\\{entry_rel}" %*\r\n'
            "popd\r\n"
            "endlocal\r\n"
            "exit /b\r\n"
        )
    return (
        "@echo off\r\n"
        "setlocal EnableExtensions\r\n"
        'set "ROOT=%~dp0"\r\n'
        'pushd "%ROOT%"\r\n'
        f'if not exist ".\\{entry_rel}" (\r\n'
        f'    echo Entry point not found: {entry_rel}\r\n'
        '    echo Run update_entry_point.bat to select a valid .py file.\r\n'
        "    popd\r\n"
        "    endlocal\r\n"
        "    exit /b 1\r\n"
        ")\r\n"
        f'".\\{py_rel_win}\\python.exe" ".\\{entry_rel}" %*\r\n'
        'set "EC=%errorlevel%"\r\n'
        "popd\r\n"
        "endlocal\r\n"
        "exit /b %EC%\r\n"
    )


def _build_update_helper_script(*, py_rel_win: str, window_only: bool) -> str:
    """Return Python helper source used by update_entry_point.bat."""
    mode_label = "window-only (pythonw.exe)" if window_only else "console (python.exe)"
    return (
        "\"\"\"Interactive launch entry-point updater for PyEmbedBuilder.\"\"\"\n"
        "from __future__ import annotations\n\n"
        "import json\n"
        "import os\n"
        "import sys\n"
        "from pathlib import Path\n\n"
        "ROOT = Path(__file__).resolve().parent\n"
        f"PY_REL = {py_rel_win!r}\n"
        f"WINDOW_ONLY = {window_only!r}\n"
        f"MODE_LABEL = {mode_label!r}\n\n"
        "def _read_current_target() -> str:\n"
        "    manifest = ROOT / 'build_manifest.json'\n"
        "    if not manifest.exists() or not manifest.is_file():\n"
        "        return 'main.py'\n"
        "    try:\n"
        "        data = json.loads(manifest.read_text(encoding='utf-8'))\n"
        "    except Exception:\n"
        "        return 'main.py'\n"
        "    if not isinstance(data, dict):\n"
        "        return 'main.py'\n"
        "    target = data.get('launch_target') or data.get('entry_point_rel') or 'main.py'\n"
        "    return str(target)\n\n"
        "def _prepare_tk_runtime() -> None:\n"
        "    py_root = (ROOT / PY_REL).resolve()\n"
        "    tcl_root = py_root / 'tcl'\n"
        "    if not os.environ.get('TCL_LIBRARY'):\n"
        "        for name in ('tcl8.7', 'tcl8.6', 'tcl8.5'):\n"
        "            cand = tcl_root / name\n"
        "            if (cand / 'init.tcl').exists():\n"
        "                os.environ['TCL_LIBRARY'] = str(cand)\n"
        "                break\n"
        "    if not os.environ.get('TK_LIBRARY'):\n"
        "        for name in ('tk8.7', 'tk8.6', 'tk8.5'):\n"
        "            cand = tcl_root / name\n"
        "            if (cand / 'tk.tcl').exists():\n"
        "                os.environ['TK_LIBRARY'] = str(cand)\n"
        "                break\n"
        "    add_dll = getattr(os, 'add_dll_directory', None)\n"
        "    if add_dll is not None:\n"
        "        for p in (py_root, py_root / 'DLLs'):\n"
        "            if p.exists():\n"
        "                try:\n"
        "                    add_dll(str(p))\n"
        "                except OSError:\n"
        "                    pass\n\n"
        "def _pick_with_tk():\n"
        "    err = ''\n"
        "    try:\n"
        "        _prepare_tk_runtime()\n"
        "        import tkinter as tk\n"
        "        from tkinter import filedialog\n"
        "    except Exception as exc:\n"
        "        err = f'{exc.__class__.__name__}: {exc}'\n"
        "        return False, None, err\n"
        "    try:\n"
        "        root = tk.Tk()\n"
        "    except Exception as exc:\n"
        "        err = f'{exc.__class__.__name__}: {exc}'\n"
        "        return False, None, err\n"
        "    root.withdraw()\n"
        "    try:\n"
        "        root.attributes('-topmost', True)\n"
        "    except Exception:\n"
        "        pass\n"
        "    initial = ROOT if ROOT.exists() else Path.cwd()\n"
        "    try:\n"
        "        chosen = filedialog.askopenfilename(\n"
        "            title='Select Python entry point',\n"
        "            initialdir=str(initial),\n"
        "            filetypes=[('Python files', '*.py'), ('All files', '*.*')],\n"
        "        )\n"
        "    finally:\n"
        "        root.destroy()\n"
        "    if not chosen:\n"
        "        return True, None, ''\n"
        "    return True, Path(chosen), ''\n\n"
        "def _prompt_path():\n"
        "    print('Enter a .py path inside this folder (example: main.py or src\\\\app.py).')\n"
        "    raw = input('Path (blank to cancel): ').strip().strip('\"')\n"
        "    if not raw:\n"
        "        return None\n"
        "    p = Path(raw)\n"
        "    if not p.is_absolute():\n"
        "        p = ROOT / p\n"
        "    return p\n\n"
        "def _arg_path():\n"
        "    if len(sys.argv) < 2:\n"
        "        return None\n"
        "    raw = ' '.join(sys.argv[1:]).strip().strip('\"')\n"
        "    if not raw:\n"
        "        return None\n"
        "    p = Path(raw)\n"
        "    if not p.is_absolute():\n"
        "        p = ROOT / p\n"
        "    return p\n\n"
        "def _validate_entry(candidate):\n"
        "    try:\n"
        "        abs_path = candidate.resolve()\n"
        "    except Exception as exc:\n"
        "        raise ValueError(f'Could not resolve path: {exc}')\n"
        "    try:\n"
        "        rel = abs_path.relative_to(ROOT)\n"
        "    except ValueError as exc:\n"
        "        raise ValueError('Entry point must stay inside this portable project folder.') from exc\n"
        "    if '..' in rel.parts:\n"
        "        raise ValueError('Entry point cannot contain .. segments.')\n"
        "    if rel.suffix.lower() != '.py':\n"
        "        raise ValueError('Entry point must be a .py file.')\n"
        "    if not abs_path.exists() or not abs_path.is_file():\n"
        "        raise ValueError(f'File not found: {abs_path}')\n"
        "    rel_win = str(rel).replace('/', '\\\\')\n"
        "    return rel_win\n\n"
        "def _launch_body(entry_rel):\n"
        "    if WINDOW_ONLY:\n"
        "        return (\n"
        "            '@echo off\\r\\n'\n"
        "            'setlocal EnableExtensions\\r\\n'\n"
        "            'set \"ROOT=%~dp0\"\\r\\n'\n"
        "            'pushd \"%ROOT%\"\\r\\n'\n"
        "            f'if not exist \".\\\\{entry_rel}\" (\\r\\n'\n"
        "            f'    echo Entry point not found: {entry_rel}\\r\\n'\n"
        "            '    echo Run update_entry_point.bat to select a valid .py file.\\r\\n'\n"
        "            '    popd\\r\\n'\n"
        "            '    endlocal\\r\\n'\n"
        "            '    exit /b 1\\r\\n'\n"
        "            ')\\r\\n'\n"
        "            f'start \"\" \".\\\\{PY_REL}\\\\pythonw.exe\" \".\\\\{entry_rel}\" %*\\r\\n'\n"
        "            'popd\\r\\n'\n"
        "            'endlocal\\r\\n'\n"
        "            'exit /b\\r\\n'\n"
        "        )\n"
        "    return (\n"
        "        '@echo off\\r\\n'\n"
        "        'setlocal EnableExtensions\\r\\n'\n"
        "        'set \"ROOT=%~dp0\"\\r\\n'\n"
        "        'pushd \"%ROOT%\"\\r\\n'\n"
        "        f'if not exist \".\\\\{entry_rel}\" (\\r\\n'\n"
        "        f'    echo Entry point not found: {entry_rel}\\r\\n'\n"
        "        '    echo Run update_entry_point.bat to select a valid .py file.\\r\\n'\n"
        "        '    popd\\r\\n'\n"
        "        '    endlocal\\r\\n'\n"
        "        '    exit /b 1\\r\\n'\n"
        "        ')\\r\\n'\n"
        "        f'\".\\\\{PY_REL}\\\\python.exe\" \".\\\\{entry_rel}\" %*\\r\\n'\n"
        "        'set \"EC=%errorlevel%\"\\r\\n'\n"
        "        'popd\\r\\n'\n"
        "        'endlocal\\r\\n'\n"
        "        'exit /b %EC%\\r\\n'\n"
        "    )\n\n"
        "def _update_manifest(entry_rel):\n"
        "    manifest = ROOT / 'build_manifest.json'\n"
        "    if not manifest.exists() or not manifest.is_file():\n"
        "        return\n"
        "    try:\n"
        "        data = json.loads(manifest.read_text(encoding='utf-8'))\n"
        "    except Exception:\n"
        "        return\n"
        "    if not isinstance(data, dict):\n"
        "        return\n"
        "    data['entry_point_mode'] = 'file'\n"
        "    data['entry_point_rel'] = entry_rel\n"
        "    data['launch_target'] = entry_rel\n"
        "    data['user_entry_point_rel'] = entry_rel\n"
        "    data.pop('console_script', None)\n"
        "    try:\n"
        "        manifest.write_text(json.dumps(data, indent=2, sort_keys=True), encoding='utf-8')\n"
        "    except Exception:\n"
        "        return\n\n"
        "def main() -> int:\n"
        "    print('==========================================')\n"
        "    print('Launch Entry Point Updater')\n"
        "    print('==========================================')\n"
        "    print(f'Current launch target: {_read_current_target()}')\n"
        "    print(f'Launch mode kept: {MODE_LABEL}')\n"
        "    print()\n"
        "    candidate = _arg_path()\n"
        "    if candidate is None:\n"
        "        has_gui, picked, tk_err = _pick_with_tk()\n"
        "        if has_gui and picked is not None:\n"
        "            candidate = picked\n"
        "        else:\n"
        "            if has_gui:\n"
        "                print('No file selected in picker.')\n"
        "            else:\n"
        "                print('Tk file picker unavailable. Using terminal input.')\n"
        "                if tk_err:\n"
        "                    print(f'Reason: {tk_err}')\n"
        "            candidate = _prompt_path()\n"
        "    if candidate is None:\n"
        "        print('No changes made.')\n"
        "        return 0\n"
        "    try:\n"
        "        entry_rel = _validate_entry(candidate)\n"
        "    except ValueError as exc:\n"
        "        print(f'Error: {exc}')\n"
        "        return 1\n"
        "    (ROOT / 'launch.bat').write_text(_launch_body(entry_rel), encoding='utf-8')\n"
        "    launch_helper = ROOT / '_pyembed_launch.py'\n"
        "    if launch_helper.exists() and launch_helper.is_file():\n"
        "        try:\n"
        "            launch_helper.unlink()\n"
        "        except OSError:\n"
        "            pass\n"
        "    _update_manifest(entry_rel)\n"
        "    print(f'Updated launch target to: {entry_rel}')\n"
        "    print('Done.')\n"
        "    return 0\n\n"
        "if __name__ == '__main__':\n"
        "    raise SystemExit(main())\n"
    )


def create_launchers(
    env_dir: Path,
    py_root: Path,
    *,
    entry_point_rel: str,
    window_only: bool,
    dependency_no_deps: bool,
) -> LauncherResult:
    """Create convenience launcher scripts in the environment directory.

    Creates:
    - install_dependencies.bat : Interactive pip installer helper
    - list_dependencies.bat : Show installed packages via pip list
    - uninstall_dependencies.bat : Interactive pip uninstall helper
    - update_entry_point.bat : Update launch target (.py path) without rebuilding
    - launch.bat : Launches the selected Python entry-point file
    """
    try:
        py_rel = py_root.relative_to(env_dir)
    except ValueError:
        py_rel = py_root
    py_rel_win = str(py_rel).replace("/", "\\")

    for stale_name in ("python_shell.bat", "run_app.bat", "diagnose_env.bat"):
        (env_dir / stale_name).unlink(missing_ok=True)

    entry_rel = entry_point_rel.replace("/", "\\").lstrip("\\/")
    if not entry_rel:
        raise ValueError("create_launchers requires entry_point_rel.")
    (env_dir / "_pyembed_launch.py").unlink(missing_ok=True)

    # Dependency install helper (interactive by default)
    deps_bat = env_dir / "install_dependencies.bat"
    deps_bat.write_text(
        "@echo off\r\n"
        "setlocal EnableExtensions\r\n"
        'cd /d "%~dp0"\r\n'
        'if not "%~1"=="" (\r\n'
        f'    ".\\{py_rel_win}\\python.exe" -m pip install %*\r\n'
        "    exit /b %errorlevel%\r\n"
        ")\r\n"
        "echo ==========================================\r\n"
        "echo   Dependency Installer (embedded Python)\r\n"
        "echo ==========================================\r\n"
        "echo.\r\n"
        ":prompt\r\n"
        'set "PKG="\r\n'
        'set /p PKG=Enter package(s) to install (blank to exit): \r\n'
        'if not defined PKG exit /b 0\r\n'
        "echo.\r\n"
        "echo Installing: %PKG%\r\n"
        f'".\\{py_rel_win}\\python.exe" -m pip install %PKG%\r\n'
        "if errorlevel 1 (\r\n"
        "    echo.\r\n"
        "    echo Installation failed. Review output above.\r\n"
        ") else (\r\n"
        "    echo.\r\n"
        "    echo Installation succeeded.\r\n"
        ")\r\n"
        "echo.\r\n"
        "goto prompt\r\n",
        encoding="utf-8",
    )

    # Dependency list helper
    list_bat = env_dir / "list_dependencies.bat"
    list_bat.write_text(
        "@echo off\r\n"
        "setlocal EnableExtensions\r\n"
        'cd /d "%~dp0"\r\n'
        'if not "%~1"=="" (\r\n'
        f'    ".\\{py_rel_win}\\python.exe" -m pip list %*\r\n'
        "    exit /b %errorlevel%\r\n"
        ")\r\n"
        "echo ==========================================\r\n"
        "echo   Installed Dependencies (embedded Python)\r\n"
        "echo ==========================================\r\n"
        f'".\\{py_rel_win}\\python.exe" -m pip list\r\n'
        "echo.\r\n"
        "pause\r\n",
        encoding="utf-8",
    )

    # Dependency uninstall helper (interactive by default)
    uninstall_bat = env_dir / "uninstall_dependencies.bat"
    uninstall_bat.write_text(
        "@echo off\r\n"
        "setlocal EnableExtensions\r\n"
        'cd /d "%~dp0"\r\n'
        'if not "%~1"=="" (\r\n'
        f'    ".\\{py_rel_win}\\python.exe" -m pip uninstall -y %*\r\n'
        "    exit /b %errorlevel%\r\n"
        ")\r\n"
        "echo ==========================================\r\n"
        "echo  Dependency Uninstaller (embedded Python)\r\n"
        "echo ==========================================\r\n"
        "echo.\r\n"
        ":prompt\r\n"
        'set "PKG="\r\n'
        'set /p PKG=Enter package(s) to uninstall (blank to exit): \r\n'
        'if not defined PKG exit /b 0\r\n'
        "echo.\r\n"
        "echo Uninstalling: %PKG%\r\n"
        f'".\\{py_rel_win}\\python.exe" -m pip uninstall -y %PKG%\r\n'
        "if errorlevel 1 (\r\n"
        "    echo.\r\n"
        "    echo Uninstall failed. Review output above.\r\n"
        ") else (\r\n"
        "    echo.\r\n"
        "    echo Uninstall succeeded.\r\n"
        ")\r\n"
        "echo.\r\n"
        "goto prompt\r\n",
        encoding="utf-8",
    )

    # App launcher based on selected entry-point file
    launch_bat = env_dir / "launch.bat"
    launch_body = _build_file_launch_bat(
        py_rel_win=py_rel_win,
        entry_rel=entry_rel,
        window_only=window_only,
    )
    launch_bat.write_text(launch_body, encoding="utf-8")

    # Entry-point updater helper
    update_helper_rel = "_pyembed_update_entry_point.py"
    update_helper_path = env_dir / update_helper_rel
    update_helper_path.write_text(
        _build_update_helper_script(
            py_rel_win=py_rel_win,
            window_only=window_only,
        ),
        encoding="utf-8",
    )

    update_entry_bat = env_dir / "update_entry_point.bat"
    update_entry_bat.write_text(
        "@echo off\r\n"
        "setlocal EnableExtensions\r\n"
        'cd /d "%~dp0"\r\n'
        f'set "PYROOT=%CD%\\{py_rel_win}"\r\n'
        'set "PATH=%PYROOT%;%PYROOT%\\DLLs;%PATH%"\r\n'
        'if not defined TCL_LIBRARY if exist "%PYROOT%\\tcl\\tcl8.7\\init.tcl" set "TCL_LIBRARY=%PYROOT%\\tcl\\tcl8.7"\r\n'
        'if not defined TCL_LIBRARY if exist "%PYROOT%\\tcl\\tcl8.6\\init.tcl" set "TCL_LIBRARY=%PYROOT%\\tcl\\tcl8.6"\r\n'
        'if not defined TCL_LIBRARY if exist "%PYROOT%\\tcl\\tcl8.5\\init.tcl" set "TCL_LIBRARY=%PYROOT%\\tcl\\tcl8.5"\r\n'
        'if not defined TK_LIBRARY if exist "%PYROOT%\\tcl\\tk8.7\\tk.tcl" set "TK_LIBRARY=%PYROOT%\\tcl\\tk8.7"\r\n'
        'if not defined TK_LIBRARY if exist "%PYROOT%\\tcl\\tk8.6\\tk.tcl" set "TK_LIBRARY=%PYROOT%\\tcl\\tk8.6"\r\n'
        'if not defined TK_LIBRARY if exist "%PYROOT%\\tcl\\tk8.5\\tk.tcl" set "TK_LIBRARY=%PYROOT%\\tcl\\tk8.5"\r\n'
        "echo ==========================================\r\n"
        "echo   Launch Entry Point Updater\r\n"
        "echo ==========================================\r\n"
        "echo.\r\n"
        f'".\\{py_rel_win}\\python.exe" ".\\{update_helper_rel}" %*\r\n'
        'set "EC=%errorlevel%"\r\n'
        "echo.\r\n"
        'if "%EC%"=="0" (\r\n'
        "    echo Entry point update completed.\r\n"
        ") else (\r\n"
        "    echo Entry point update failed.\r\n"
        ")\r\n"
        "echo.\r\n"
        "pause\r\n"
        "exit /b %EC%\r\n",
        encoding="utf-8",
    )

    audit(
        "launcher_created",
        deps=str(deps_bat),
        list=str(list_bat),
        uninstall=str(uninstall_bat),
        launch=str(launch_bat),
        update_entry_point=str(update_entry_bat),
        launch_target=entry_rel,
        launch_target_mode="file",
        mode="window_only" if window_only else "console",
        dependency_mode="no_deps" if dependency_no_deps else "resolved",
    )
    return LauncherResult(
        deps_bat=deps_bat,
        list_bat=list_bat,
        uninstall_bat=uninstall_bat,
        launch_bat=launch_bat,
        update_entry_point_bat=update_entry_bat,
    )
