"""
Environment builder — orchestrates the full secure build pipeline.
"""
from __future__ import annotations

import json
import re
import shutil
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .. import __version__
from ..models import BuildPlan, StepUpdate
from ..security import (
    audit,
    redact_url_secrets,
    sanitize_env_name,
    sanitize_source_path_for_manifest,
    set_audit_env_root,
    validate_target_path,
)
from ..util.paths import EMBED_ROOT_DIRNAME, cache_dir
from .downloader import download_file
from .extractor import extract_zip
from .http import http_get
from .launcher import create_launchers
from .pth_patcher import patch_embedded_pth
from .project_inspector import detect_pyproject_dependencies, find_requirements_file
from .project_source import prepare_project_source
from .pymanager import augment_from_pymanager
from .python_release_page import get_embeddable_info
from .subprocess_runner import CommandCancelled, run_command_stream


GET_PIP_URL = "https://bootstrap.pypa.io/get-pip.py"
MANIFEST_FILENAME = "build_manifest.json"
CONFIG_MARKDOWN_FILENAME = "EMBEDDED_PYTHON_CONFIGURATION.md"

LogCb = Callable[[str], None]
StepCb = Callable[[StepUpdate], None]
ProgressCb = Callable[[int, int | None], None]


@dataclass(frozen=True)
class BuildResult:
    env_dir: Path
    py_root: Path
    manifest: dict


@dataclass(frozen=True)
class LaunchTarget:
    entry_point_rel: str

    @property
    def display(self) -> str:
        return self.entry_point_rel


class CancelledError(RuntimeError):
    pass


class EnvBuilder:
    """Builds a fully configured embedded Python environment."""

    def __init__(self, *, cancel_event: threading.Event | None = None) -> None:
        self._cancel = cancel_event or threading.Event()
        self._manifest: dict = {}

    # ── helpers ────────────────────────────────────────────────────────

    def _check(self) -> None:
        if self._cancel.is_set():
            raise CancelledError("Build cancelled by user.")

    def _step(
        self, cb: StepCb, sid: str, title: str, status: str, detail: str = ""
    ) -> None:
        cb(StepUpdate(step_id=sid, title=title, status=status, detail=detail))

    # ── main entry point ──────────────────────────────────────────────

    def build(
        self,
        plan: BuildPlan,
        *,
        step_cb: StepCb,
        log_cb: LogCb,
        progress_cb: ProgressCb | None = None,
    ) -> BuildResult:
        """Execute the full environment build pipeline."""
        sanitize_env_name(plan.env_name)
        validate_target_path(plan.target_dir)
        audit(
            "build_start",
            env=plan.env_name,
            version=str(plan.version),
            arch=plan.arch,
        )

        env_dir = plan.target_dir
        py_root = env_dir / EMBED_ROOT_DIRNAME
        self._manifest = {
            "manifest_version": 1,
            "builder_version": __version__,
            "env_name": plan.env_name,
            "project_mode": plan.project_mode,
            "source": {
                "path": sanitize_source_path_for_manifest(plan.source_path),
                "url": redact_url_secrets(plan.source_url),
                "ref": plan.source_ref,
            },
            "user_entry_point_rel": plan.entry_point_rel,
            "entry_point_rel": plan.entry_point_rel,
            "entry_point_mode": "file",
            "launch_target": plan.entry_point_rel,
            "launch_mode": "window_only" if plan.window_only else "console",
            "dependency_mode": "no_deps" if plan.dependency_no_deps else "resolved",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "python_version": str(plan.version),
            "arch": plan.arch,
            "artifacts": {},
        }

        s = lambda sid, title, status, detail="": self._step(
            step_cb, sid, title, status, detail
        )

        set_audit_env_root(env_dir)
        try:
            # 1 ─ Prepare project source
            self._check()
            s("source", "Prepare project source", "running", plan.project_mode)
            source_result = prepare_project_source(
                plan=plan,
                env_dir=env_dir,
                log_cb=log_cb,
                cancel_event=self._cancel,
            )
            launch_target = self._resolve_launch_target(plan, env_dir, log_cb)
            s("source", "Prepare project source", "ok", source_result.detail)
            self._manifest["source"].update({
                "detail": source_result.detail,
                "files_copied": source_result.files_copied,
            })
            self._manifest["entry_point_mode"] = "file"
            self._manifest["entry_point_rel"] = launch_target.entry_point_rel
            self._manifest["launch_target"] = launch_target.display

            # 2 ─ Resolve metadata
            self._check()
            s("meta", "Resolve download metadata", "running",
              f"{plan.version} ({plan.arch})")
            info = get_embeddable_info(plan.version, plan.arch)
            self._manifest["artifacts"]["python_zip"] = {
                "url": info.url,
                "filename": info.filename,
            }
            s("meta", "Resolve download metadata", "ok", info.filename)
            log_cb(f"Resolved: {info.filename}")
            log_cb(f"URL: {info.url}")

            # 3 ─ Download ZIP
            self._check()
            s("download", "Download Python embeddable ZIP", "running")
            zip_path = self._acquire_zip(info, log_cb, progress_cb)
            s("download", "Download Python embeddable ZIP", "ok",
              zip_path.name)

            # 4 ─ Extract
            self._check()
            s("extract", "Extract and verify archive", "running")
            extract_zip(zip_path, py_root)
            s("extract", "Extract and verify archive", "ok")
            log_cb(f"Extracted to: {py_root}")

            # 5 ─ MSI components (optional)
            self._check()
            if plan.use_pymanager_components:
                s("pymanager", "MSI components (stdlib + tools)", "running")
                pm = augment_from_pymanager(
                    version=plan.version,
                    arch=plan.arch,
                    py_root=py_root,
                    log_cb=log_cb,
                )
                pm_detail = (
                    ", ".join(pm.extracted_dirs) if pm.status == "ok" else "Skipped"
                )
                s("pymanager", "MSI components (stdlib + tools)", "ok", pm_detail)
                self._manifest["artifacts"]["pymanager"] = {
                    "status": pm.status,
                    "msix_version": pm.msix_version,
                    "msix_url": pm.msix_url,
                    "pythoncore_zip": pm.pythoncore_zip,
                    "extracted_dirs": pm.extracted_dirs,
                    "reason": pm.reason,
                }
            else:
                s("pymanager", "MSI components (stdlib + tools)", "ok", "Skipped")
                self._manifest["artifacts"]["pymanager"] = {
                    "status": "skipped", "reason": "disabled",
                }

            # 6 ─ Patch _pth
            self._check()
            s("pth", "Configure pythonXY._pth", "running")
            pth = patch_embedded_pth(py_root)
            s("pth", "Configure pythonXY._pth", "ok", pth.pth_path.name)
            log_cb(f"Patched: {pth.pth_path.name}  stdlib: {pth.zip_name}")
            self._manifest["artifacts"]["pth"] = {
                "filename": pth.pth_path.name,
                "stdlib_zip": pth.zip_name,
            }

            # 7 ─ Bootstrap pip
            self._check()
            s("pip", "Bootstrap pip", "running")
            self._bootstrap_pip(py_root, log_cb)
            s("pip", "Bootstrap pip", "ok")

            # 8 ─ Install dependencies
            self._check()
            s("reqs", "Install dependencies", "running")
            dep_info = self._install_dependencies(plan, env_dir, py_root, log_cb)
            if dep_info.get("skipped"):
                s("reqs", "Install dependencies", "ok", "Skipped")
            else:
                s(
                    "reqs",
                    "Install dependencies",
                    "ok",
                    str(dep_info.get("summary", "Done")),
                )

            # Ensure a placeholder requirements.txt exists in env root
            req_placeholder = env_dir / "requirements.txt"
            if not req_placeholder.exists():
                req_placeholder.write_text("", encoding="utf-8")
                log_cb("Created empty requirements.txt in environment root.")

            # 8b ─ Sanitize pip metadata (strip absolute paths)
            self._check()
            log_cb("Sanitizing pip metadata (remove absolute paths)...")
            red_files, red_paths = _sanitize_pip_metadata(py_root, log_cb)
            self._manifest["pip_metadata"] = {
                "files_redacted": red_files,
                "paths_redacted": red_paths,
            }
            audit(
                "pip_metadata_sanitized",
                files=str(red_files),
                paths=str(red_paths),
            )

            # 9 ─ Sanity check
            self._check()
            s("verify", "Verify environment", "running")
            self._sanity_check(py_root, log_cb)
            s("verify", "Verify environment", "ok")
            dep_snapshot = self._capture_installed_packages(py_root, log_cb)
            self._manifest.setdefault("dependencies", {}).update(dep_snapshot)

            # 10 ─ Entry-point smoke test
            self._check()
            s("smoke", "Smoke-test launch target", "running")
            self._entrypoint_smoke_test(env_dir, py_root, launch_target, log_cb)
            s("smoke", "Smoke-test launch target", "ok")

            # 11 ─ Create launchers
            self._check()
            s("launcher", "Create launcher scripts", "running")
            lr = create_launchers(
                env_dir,
                py_root,
                entry_point_rel=launch_target.entry_point_rel,
                window_only=plan.window_only,
                dependency_no_deps=plan.dependency_no_deps,
            )
            s("launcher", "Create launcher scripts", "ok",
              f"{lr.launch_bat.name}, {lr.deps_bat.name}, {lr.list_bat.name}, {lr.update_entry_point_bat.name}")
            self._manifest["artifacts"]["launchers"] = {
                "launch": lr.launch_bat.name,
                "install_dependencies": lr.deps_bat.name,
                "list_dependencies": lr.list_bat.name,
                "uninstall_dependencies": lr.uninstall_bat.name,
                "update_entry_point": lr.update_entry_point_bat.name,
            }

            # 12 ─ Write portable guides
            self._check()
            s("guide", "Write portable guides", "running")
            guide_path = self._write_quickstart(env_dir, plan, launch_target, log_cb)
            config_path = self._write_configuration_markdown(
                env_dir,
                plan,
                py_root,
                log_cb,
            )
            s("guide", "Write portable guides", "ok", f"{guide_path.name}, {config_path.name}")
            self._manifest["artifacts"]["quickstart"] = guide_path.name
            self._manifest["artifacts"]["configuration_markdown"] = config_path.name

            # 13 ─ Optional: clear cache after build
            if plan.clear_cache_on_success:
                self._check()
                log_cb("Clearing download cache...")
                try:
                    removed = _clear_cache_dir(cache_dir())
                    log_cb(f"Cache cleared ({removed} items).")
                    self._manifest["cache"] = {
                        "cleared": True,
                        "items_removed": removed,
                    }
                    audit("cache_cleared", items=str(removed))
                except Exception as exc:
                    log_cb(f"Cache clear failed: {exc}")
                    self._manifest["cache"] = {
                        "cleared": False,
                        "error": str(exc),
                    }
                    audit("cache_clear_failed", error=str(exc))
            else:
                self._manifest["cache"] = {
                    "cleared": False,
                    "reason": "disabled",
                }

            manifest_path = env_dir / MANIFEST_FILENAME
            self._manifest["manifest_file"] = manifest_path.name
            self._write_manifest(manifest_path, log_cb)
            audit("manifest_written", path=str(manifest_path))
            audit("build_complete", env=plan.env_name, path=str(env_dir))

            return BuildResult(
                env_dir=env_dir,
                py_root=py_root,
                manifest=self._manifest,
            )

        except CancelledError:
            audit("build_cancelled", env=plan.env_name)
            raise
        except CommandCancelled:
            audit("build_cancelled", env=plan.env_name)
            raise CancelledError("Build cancelled by user.")
        except Exception as exc:
            audit("build_failed", env=plan.env_name, error=str(exc))
            raise
        finally:
            set_audit_env_root(None)

    # ── pipeline helpers ──────────────────────────────────────────────

    def _acquire_zip(
        self, info, log_cb: LogCb, progress_cb: ProgressCb | None,
    ) -> Path:
        """Return path to the embeddable ZIP (cache hit or fresh download)."""
        cache_path = cache_dir() / info.filename

        cached_size = 0
        if cache_path.exists():
            cached_size = cache_path.stat().st_size
            if cached_size <= 0:
                cache_path.unlink(missing_ok=True)
                cached_size = 0
            else:
                log_cb(
                    f"Cached ZIP found ({cached_size:,} bytes) — refreshing from source."
                )

        # Download
        self._check()
        log_cb(f"Downloading: {info.url}")

        def _progress(done: int, total: int | None) -> None:
            if self._cancel.is_set():
                raise CancelledError("Cancelled during download.")
            if progress_cb:
                progress_cb(done, total)

        try:
            result = download_file(
                info.url,
                cache_path,
                progress_cb=_progress,
                source_policy="python_embed_zip",
            )
            log_cb(f"Size: {result.size_bytes:,} bytes")
            self._manifest["artifacts"]["python_zip"].update({
                "source": "downloaded",
                "size_bytes": result.size_bytes,
            })
            return result.path
        except CancelledError:
            raise
        except Exception as exc:
            if cached_size > 0 and cache_path.exists():
                log_cb(
                    f"Download failed ({exc}). Falling back to cached ZIP: {cache_path.name}"
                )
                audit(
                    "download_cache_fallback",
                    filename=cache_path.name,
                    error=str(exc),
                )
                self._manifest["artifacts"]["python_zip"].update({
                    "source": "cache_fallback",
                    "size_bytes": cached_size,
                })
                return cache_path
            raise

    def _bootstrap_pip(self, py_root: Path, log_cb: LogCb) -> None:
        py_exe = py_root / "python.exe"
        if not py_exe.exists():
            raise FileNotFoundError(f"python.exe not found: {py_exe}")

        cached_get_pip = cache_dir() / "get-pip.py"
        try:
            log_cb(f"Downloading get-pip.py from {GET_PIP_URL}")
            data = http_get(GET_PIP_URL, source_policy="get_pip_script")
            cached_get_pip.write_bytes(data)
            log_cb(f"get-pip.py downloaded ({len(data):,} bytes)")
        except Exception as exc:
            if cached_get_pip.exists() and cached_get_pip.stat().st_size > 0:
                log_cb(f"get-pip.py download failed ({exc}); using cached copy.")
                audit("get_pip_cache_fallback", error=str(exc))
            else:
                raise

        env_get_pip = py_root / "get-pip.py"
        shutil.copyfile(cached_get_pip, env_get_pip)
        try:
            run_command_stream(
                [str(py_exe), str(env_get_pip), "--no-warn-script-location"],
                cwd=py_root,
                log_cb=log_cb,
                allowed_exe_dir=py_root,
                cancel_event=self._cancel,
            )
            run_command_stream(
                [str(py_exe), "-m", "pip", "install", "--upgrade", "pip"],
                cwd=py_root,
                log_cb=log_cb,
                allowed_exe_dir=py_root,
                cancel_event=self._cancel,
            )
        finally:
            env_get_pip.unlink(missing_ok=True)
        self._manifest["artifacts"]["get_pip"] = {"source": GET_PIP_URL}

    def _resolve_launch_target(
        self, plan: BuildPlan, env_dir: Path, log_cb: LogCb
    ) -> LaunchTarget:
        entry_raw = plan.entry_point_rel.strip()
        if entry_raw:
            entry_rel = Path(entry_raw.replace("\\", "/"))
            if entry_rel.is_absolute() or ".." in entry_rel.parts:
                raise ValueError(
                    f"Entry point must stay inside output folder: {plan.entry_point_rel}"
                )
            entry_abs = (env_dir / entry_rel).resolve()
            try:
                entry_abs.relative_to(env_dir.resolve())
            except ValueError as exc:
                raise ValueError(
                    f"Entry point escapes output folder: {plan.entry_point_rel}"
                ) from exc
            if entry_abs.exists():
                if not entry_abs.is_file():
                    raise ValueError(f"Entry point exists but is not a file: {entry_abs}")
                return LaunchTarget(entry_point_rel=str(entry_rel).replace("/", "\\"))
            log_cb(
                f"Entry point not found ({entry_rel}); "
                "falling back to main.py."
            )

        main_rel = Path("main.py")
        main_abs = (env_dir / main_rel).resolve()
        try:
            main_abs.relative_to(env_dir.resolve())
        except ValueError as exc:
            raise ValueError("main.py resolves outside output folder.") from exc

        if main_abs.exists():
            if not main_abs.is_file():
                raise ValueError(f"main.py exists but is not a file: {main_abs}")
            log_cb("Using main.py as launch entry point.")
            return LaunchTarget(entry_point_rel="main.py")

        if plan.use_pymanager_components:
            starter_main = (
                "\"\"\"PyEmbedBuilder starter entry point.\"\"\"\n\n"
                "def main() -> int:\n"
                "    try:\n"
                "        import tkinter as tk\n"
                "        from tkinter import ttk\n"
                "    except Exception:\n"
                "        print(\"This is the default entry point.\")\n"
                "        print(\"Replace main.py or set a custom entry point for your app.\")\n"
                "        try:\n"
                "            input(\"Press Enter to close...\")\n"
                "        except EOFError:\n"
                "            pass\n"
                "        return 0\n\n"
                "    root = tk.Tk()\n"
                "    root.title(\"PyEmbedBuilder Starter\")\n"
                "    root.resizable(False, False)\n\n"
                "    frame = ttk.Frame(root, padding=16)\n"
                "    frame.grid(row=0, column=0, sticky=\"nsew\")\n\n"
                "    text = (\n"
                "        \"This is the default entry point.\\n\"\n"
                "        \"Replace main.py or set a custom entry point for your app.\"\n"
                "    )\n"
                "    lbl = ttk.Label(frame, text=text, justify=\"center\", anchor=\"center\")\n"
                "    lbl.grid(row=0, column=0, pady=(0, 12))\n\n"
                "    btn = ttk.Button(frame, text=\"Close\", command=root.destroy)\n"
                "    btn.grid(row=1, column=0)\n"
                "    btn.focus_set()\n\n"
                "    root.update_idletasks()\n"
                "    w = root.winfo_reqwidth()\n"
                "    h = root.winfo_reqheight()\n"
                "    sw = root.winfo_screenwidth()\n"
                "    sh = root.winfo_screenheight()\n"
                "    x = max(0, (sw - w) // 2)\n"
                "    y = max(0, (sh - h) // 2)\n"
                "    root.geometry(f\"{w}x{h}+{x}+{y}\")\n\n"
                "    root.mainloop()\n"
                "    return 0\n\n\n"
                "if __name__ == \"__main__\":\n"
                "    raise SystemExit(main())\n"
            )
        else:
            starter_main = (
                "\"\"\"PyEmbedBuilder starter entry point.\"\"\"\n\n"
                "import sys\n\n"
                "def main() -> int:\n"
                "    print(\"This is the default entry point.\")\n"
                "    print(\"Replace main.py or set a custom entry point for your app.\")\n"
                "    if sys.stdin is not None and sys.stdin.isatty():\n"
                "        try:\n"
                "            input(\"Press Enter to close...\")\n"
                "        except EOFError:\n"
                "            pass\n"
                "    return 0\n\n\n"
                "if __name__ == \"__main__\":\n"
                "    raise SystemExit(main())\n"
            )

        main_abs.parent.mkdir(parents=True, exist_ok=True)
        main_abs.write_text(starter_main, encoding="utf-8")
        log_cb("Created starter entry point: main.py")
        return LaunchTarget(entry_point_rel="main.py")

    @staticmethod
    def _detect_requirements_file(env_dir: Path) -> Path | None:
        return find_requirements_file(env_dir)

    @staticmethod
    def _detect_pyproject_dependencies(env_dir: Path) -> tuple[str, ...]:
        return detect_pyproject_dependencies(env_dir)

    @staticmethod
    def _has_installable_project(env_dir: Path) -> bool:
        pyproject = env_dir / "pyproject.toml"
        if pyproject.exists() and pyproject.is_file():
            return True
        for name in ("setup.py", "setup.cfg"):
            p = env_dir / name
            if p.exists() and p.is_file():
                return True
        return False

    def _install_dependencies(
        self, plan: BuildPlan, env_dir: Path, py_root: Path, log_cb: LogCb,
    ) -> dict:
        py_exe = py_root / "python.exe"
        dep_flags = ["--no-deps"] if plan.dependency_no_deps else []

        req_source = plan.requirements_txt or self._detect_requirements_file(env_dir)
        used_auto_req = (
            bool(req_source)
            and plan.requirements_txt is None
        )
        requirements_entries = _load_dependency_entries(req_source)

        actions: list[str] = []
        pyproject_deps: tuple[str, ...] = ()
        installed_project = False
        project_install_no_deps = False
        installable_project = self._has_installable_project(env_dir)
        project_first_attempted = False
        project_first_succeeded = False
        skipped_auto_pyproject_after_project = False

        auto_metadata_mode = (
            plan.requirements_txt is None
            and (
                req_source is not None
                or (env_dir / "pyproject.toml").exists()
            )
        )
        if plan.auto_install_project and installable_project and auto_metadata_mode:
            project_first_attempted = True
            first_flags: list[str] = list(dep_flags)
            if first_flags:
                project_install_no_deps = True
                log_cb(
                    "Auto mode detected requirements/pyproject metadata; trying pip install . first (--no-deps)."
                )
            else:
                log_cb(
                    "Auto mode detected requirements/pyproject metadata; trying pip install . first."
                )
            try:
                run_command_stream(
                    [str(py_exe), "-m", "pip", "install", *first_flags, "."],
                    cwd=env_dir,
                    log_cb=log_cb,
                    allowed_exe_dir=py_root,
                    cancel_event=self._cancel,
                )
                actions.append("pip install . --no-deps" if first_flags else "pip install .")
                installed_project = True
                project_first_succeeded = True
            except Exception as exc:
                log_cb(
                    f"Initial pip install . failed ({exc}); continuing with automatic dependency installation."
                )

        if req_source:
            if used_auto_req:
                try:
                    rel_req = req_source.relative_to(env_dir).as_posix()
                except Exception:
                    rel_req = req_source.name
                log_cb(f"No requirements file selected; using discovered {rel_req}.")
            run_command_stream(
                [str(py_exe), "-m", "pip", "install", *dep_flags, "-r", str(req_source)],
                cwd=py_root,
                log_cb=log_cb,
                allowed_exe_dir=py_root,
                cancel_event=self._cancel,
            )
            actions.append(f"-r {req_source.name}")
        else:
            pyproject_deps = self._detect_pyproject_dependencies(env_dir)
            if pyproject_deps:
                if project_first_succeeded and not plan.dependency_no_deps:
                    skipped_auto_pyproject_after_project = True
                    log_cb(
                        "Skipping automatic pyproject dependency install because pip install . already succeeded."
                    )
                else:
                    log_cb(
                        "No requirements file found; installing dependencies from pyproject.toml [project.dependencies]."
                    )
                    run_command_stream(
                        [str(py_exe), "-m", "pip", "install", *dep_flags, *pyproject_deps],
                        cwd=py_root,
                        log_cb=log_cb,
                        allowed_exe_dir=py_root,
                        cancel_event=self._cancel,
                    )
                    actions.append(f"{len(pyproject_deps)} pyproject dependency(ies)")

        if plan.manual_packages:
            run_command_stream(
                [str(py_exe), "-m", "pip", "install", *dep_flags, *plan.manual_packages],
                cwd=py_root,
                log_cb=log_cb,
                allowed_exe_dir=py_root,
                cancel_event=self._cancel,
            )
            actions.append(f"{len(plan.manual_packages)} manual package(s)")

        if plan.auto_install_project and installable_project and not installed_project:
            project_flags: list[str] = []
            if plan.dependency_no_deps or actions:
                project_flags = ["--no-deps"]
                project_install_no_deps = True

            if project_install_no_deps:
                log_cb(
                    "Installing project package via pip install . (--no-deps; dependencies were handled separately)."
                )
            else:
                log_cb("Installing project package via pip install .")
            run_command_stream(
                [str(py_exe), "-m", "pip", "install", *project_flags, "."],
                cwd=env_dir,
                log_cb=log_cb,
                allowed_exe_dir=py_root,
                cancel_event=self._cancel,
            )
            actions.append("pip install . --no-deps" if project_flags else "pip install .")
            installed_project = True
        elif plan.auto_install_project and not installable_project:
            log_cb(
                "Auto-install project enabled, but no pyproject.toml/setup.py/setup.cfg found; skipping pip install ."
            )

        summary = ", ".join(actions) if actions else "Skipped"
        info = {
            "requirements_file": _display_dependency_path(req_source, env_dir),
            "requirements_auto_detected": used_auto_req,
            "requirements_entries": requirements_entries,
            "manual_packages": [
                _sanitize_dependency_display(pkg) for pkg in plan.manual_packages
            ],
            "pyproject_dependencies": [
                _sanitize_dependency_display(dep) for dep in pyproject_deps
            ],
            "auto_install_project": plan.auto_install_project,
            "installable_project_detected": installable_project,
            "project_installed": installed_project,
            "project_install_no_deps": project_install_no_deps,
            "project_first_attempted": project_first_attempted,
            "project_first_succeeded": project_first_succeeded,
            "skipped_auto_pyproject_after_project": skipped_auto_pyproject_after_project,
            "no_deps": plan.dependency_no_deps,
            "summary": summary,
            "skipped": not actions,
        }
        self._manifest["dependencies"] = info
        return info

    def _sanity_check(self, py_root: Path, log_cb: LogCb) -> None:
        py_exe = py_root / "python.exe"
        run_command_stream(
            [str(py_exe), "-c",
             "import sys, site; "
             "print('Python:', sys.version); "
             "print('Executable:', sys.executable); "
             "print('Site-packages:', site.getsitepackages()); "
             "import pip; print('pip:', pip.__version__)"],
            cwd=py_root,
            log_cb=log_cb,
            allowed_exe_dir=py_root,
            cancel_event=self._cancel,
        )

    def _entrypoint_smoke_test(
        self,
        env_dir: Path,
        py_root: Path,
        launch_target: LaunchTarget,
        log_cb: LogCb,
    ) -> None:
        py_exe = py_root / "python.exe"
        entry_abs = (env_dir / Path(launch_target.entry_point_rel.replace("\\", "/"))).resolve()
        if not entry_abs.exists():
            raise FileNotFoundError(f"Entry point not found for smoke test: {entry_abs}")
        run_command_stream(
            [str(py_exe), "-m", "py_compile", str(entry_abs)],
            cwd=env_dir,
            log_cb=log_cb,
            allowed_exe_dir=py_root,
            cancel_event=self._cancel,
        )

    def _capture_installed_packages(self, py_root: Path, log_cb: LogCb) -> dict:
        py_exe = py_root / "python.exe"
        captured: list[str] = []

        def _capture(line: str) -> None:
            captured.append(line)

        try:
            run_command_stream(
                [
                    str(py_exe),
                    "-c",
                    (
                        "import json\n"
                        "from importlib import metadata\n"
                        "\n"
                        "packages = []\n"
                        "seen = set()\n"
                        "dists = sorted(\n"
                        "    metadata.distributions(),\n"
                        "    key=lambda d: (d.metadata.get('Name') or '').lower(),\n"
                        ")\n"
                        "for dist in dists:\n"
                        "    name = (dist.metadata.get('Name') or '').strip()\n"
                        "    version = str(dist.version).strip()\n"
                        "    key = name.lower()\n"
                        "    if not name or key in seen:\n"
                        "        continue\n"
                        "    seen.add(key)\n"
                        "    packages.append({'name': name, 'version': version})\n"
                        "print(json.dumps(packages))\n"
                    ),
                ],
                cwd=py_root,
                log_cb=_capture,
                allowed_exe_dir=py_root,
                cancel_event=self._cancel,
            )
        except Exception as exc:
            log_cb(f"Installed package snapshot unavailable: {exc}")
            return {
                "installed_packages": [],
                "installed_package_count": 0,
                "installed_packages_source": "importlib.metadata",
                "installed_packages_error": str(exc),
            }

        output = "\n".join(line for line in captured if not line.startswith("$ ")).strip()
        if not output:
            log_cb("Installed package snapshot was empty.")
            return {
                "installed_packages": [],
                "installed_package_count": 0,
                "installed_packages_source": "importlib.metadata",
                "installed_packages_error": "empty_output",
            }

        try:
            raw_packages = json.loads(output)
        except json.JSONDecodeError as exc:
            log_cb(f"Installed package snapshot parse failed: {exc}")
            return {
                "installed_packages": [],
                "installed_package_count": 0,
                "installed_packages_source": "importlib.metadata",
                "installed_packages_error": f"json_decode_error: {exc}",
            }

        packages: list[dict[str, str]] = []
        if isinstance(raw_packages, list):
            for item in raw_packages:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name", "")).strip()
                version = str(item.get("version", "")).strip()
                if not name:
                    continue
                packages.append({"name": name, "version": version})

        log_cb(f"Captured installed package snapshot ({len(packages)} packages).")
        return {
            "installed_packages": packages,
            "installed_package_count": len(packages),
            "installed_packages_source": "importlib.metadata",
            "installed_packages_error": "",
        }

    def _write_quickstart(
        self,
        env_dir: Path,
        plan: BuildPlan,
        launch_target: LaunchTarget,
        log_cb: LogCb,
    ) -> Path:
        guide = env_dir / "README_portable.txt"
        launch_target_label = launch_target.display or "(auto)"
        launch_hint = (
            "- If launch.bat reports missing entry point, run update_entry_point.bat\n"
            "  to select a valid .py file (or rebuild with a different source).\n"
        )
        guide.write_text(
            (
                "PyEmbedBuilder Portable Project Guide\n"
                "===================================\n\n"
                "1) Run launch.bat to start the app.\n"
                "2) Run install_dependencies.bat to add packages.\n"
                "3) Run list_dependencies.bat to inspect installed packages.\n"
                "4) Run uninstall_dependencies.bat to remove packages.\n\n"
                f"5) Open {CONFIG_MARKDOWN_FILENAME} for the full embedded Python configuration summary.\n"
                "6) Run update_entry_point.bat to change startup file without rebuilding.\n\n"
                f"Launch target: {launch_target_label}\n"
                f"Python mode: {'pythonw.exe (window only)' if plan.window_only else 'python.exe (console)'}\n"
                f"Dependency mode: {'--no-deps' if plan.dependency_no_deps else 'resolved'}\n\n"
                "Troubleshooting\n"
                "---------------\n"
                f"{launch_hint}"
                "- If imports fail, install missing packages via install_dependencies.bat.\n"
                f"- Open {CONFIG_MARKDOWN_FILENAME} to review Python version, optional components, and dependencies.\n"
                "- Review .pyembed_builder/logs/security_audit.log for audit details.\n"
            ),
            encoding="utf-8",
        )
        log_cb(f"Wrote quick-start guide: {guide.name}")
        return guide

    def _write_configuration_markdown(
        self,
        env_dir: Path,
        plan: BuildPlan,
        py_root: Path,
        log_cb: LogCb,
    ) -> Path:
        config_path = env_dir / CONFIG_MARKDOWN_FILENAME
        artifacts = self._manifest.get("artifacts", {})
        dep_info = self._manifest.get("dependencies", {})
        py_zip = artifacts.get("python_zip", {})
        pth_info = artifacts.get("pth", {})
        pm_info = artifacts.get("pymanager", {})

        try:
            py_root_rel = py_root.relative_to(env_dir).as_posix()
        except ValueError:
            py_root_rel = py_root.name

        pm_dirs = pm_info.get("extracted_dirs") or []
        pm_reason = pm_info.get("reason") or ""
        pm_status = pm_info.get("status", "skipped")
        pm_requested = "yes" if plan.use_pymanager_components else "no"
        launch_mode = (
            "pythonw.exe (window only)"
            if plan.window_only
            else "python.exe (console)"
        )
        dep_mode = (
            "direct only (--no-deps)"
            if plan.dependency_no_deps
            else "resolved (default pip behavior)"
        )

        lines = [
            "# Embedded Python Environment Configuration",
            "",
            "This file is a human- and agent-friendly summary of the portable environment.",
            f"`{MANIFEST_FILENAME}` is the machine-readable source of truth.",
            "",
            "## Quick Scan",
            f"- Environment name: `{self._manifest.get('env_name', '?')}`",
            f"- Project mode: `{self._manifest.get('project_mode', plan.project_mode)}`",
            f"- Python version: `{self._manifest.get('python_version', str(plan.version))}`",
            f"- Architecture: `{self._manifest.get('arch', plan.arch)}`",
            f"- Python runtime root: `{py_root_rel}`",
            f"- Launch mode: `{launch_mode}`",
            f"- Dependency mode: `{dep_mode}`",
            f"- Builder version: `{self._manifest.get('builder_version', __version__)}`",
            f"- Created at: `{self._manifest.get('created_at', '')}`",
            "",
            "## Embedded Python Runtime",
            f"- Embeddable archive: `{py_zip.get('filename', '') or '(unknown)'}`",
            f"- Archive source: `{py_zip.get('source', '') or '(unknown)'}`",
            f"- Archive size: `{_format_size_bytes(py_zip.get('size_bytes'))}`",
            f"- Standard library zip: `{pth_info.get('stdlib_zip', '') or '(unknown)'}`",
            f"- `site-packages` enabled via: `{pth_info.get('filename', '') or '(unknown)'}`",
            f"- `pip` bootstrapped from: `{artifacts.get('get_pip', {}).get('source', GET_PIP_URL)}`",
            "",
            "## Optional Components",
            f"- MSI component augmentation requested: `{pm_requested}`",
            f"- MSI augmentation status: `{pm_status}`",
        ]
        if pm_dirs:
            lines.append(f"- Added directories: `{', '.join(pm_dirs)}`")
        else:
            lines.append("- Added directories: `(none)`")
        if pm_info.get("msix_url"):
            lines.append(f"- MSI source: `{pm_info.get('msix_url')}`")
        if pm_info.get("pythoncore_zip"):
            lines.append(f"- MSI packages used: `{pm_info.get('pythoncore_zip')}`")
        if pm_reason:
            lines.append(f"- Notes: `{pm_reason}`")

        lines.extend([
            "",
            "## Dependency Inputs",
            f"- Resolution summary: `{dep_info.get('summary', 'Skipped')}`",
            f"- Requirements file: `{dep_info.get('requirements_file', '') or '(none)'}`",
            f"- Requirements auto-detected: `{_yes_no(dep_info.get('requirements_auto_detected', False))}`",
            f"- Auto-install project package enabled: `{_yes_no(dep_info.get('auto_install_project', False))}`",
            f"- Installable project metadata detected: `{_yes_no(dep_info.get('installable_project_detected', False))}`",
            f"- Project package installed: `{_yes_no(dep_info.get('project_installed', False))}`",
            f"- Project install used `--no-deps`: `{_yes_no(dep_info.get('project_install_no_deps', False))}`",
            f"- Installed package snapshot source: `{dep_info.get('installed_packages_source', 'importlib.metadata')}`",
        ])
        if dep_info.get("installed_packages_error"):
            lines.append(
                f"- Installed package snapshot error: `{dep_info.get('installed_packages_error')}`"
            )

        _append_markdown_section(
            lines,
            "Requirements Entries",
            dep_info.get("requirements_entries") or [],
        )
        _append_markdown_section(
            lines,
            "Manual Packages",
            dep_info.get("manual_packages") or [],
        )
        _append_markdown_section(
            lines,
            "Pyproject Dependencies",
            dep_info.get("pyproject_dependencies") or [],
        )

        installed_packages = dep_info.get("installed_packages") or []
        lines.extend([
            "",
            "## Installed Packages",
            f"- Installed package count: `{dep_info.get('installed_package_count', len(installed_packages))}`",
        ])
        if installed_packages:
            for pkg in installed_packages:
                if not isinstance(pkg, dict):
                    continue
                name = str(pkg.get("name", "")).strip()
                version = str(pkg.get("version", "")).strip()
                if not name:
                    continue
                lines.append(f"- `{name}=={version or '?'}`")
        else:
            lines.append("- `(none captured)`")

        lines.extend([
            "",
            "## Portable Workflow Files",
            "- `launch.bat`",
            "- `install_dependencies.bat`",
            "- `list_dependencies.bat`",
            "- `uninstall_dependencies.bat`",
            "- `update_entry_point.bat`",
            f"- `{MANIFEST_FILENAME}`",
            "- `README_portable.txt`",
        ])

        config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        log_cb(f"Wrote environment configuration markdown: {config_path.name}")
        return config_path

    def _write_manifest(self, manifest_path: Path, log_cb: LogCb) -> None:
        """Persist a JSON build manifest inside the environment root."""
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps(self._manifest, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        log_cb(f"Wrote manifest: {manifest_path.name}")


def _clear_cache_dir(path: Path) -> int:
    """Remove all files and folders inside the cache directory."""
    if not path.exists():
        return 0
    removed = 0
    for child in path.iterdir():
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)
        removed += 1
    return removed


def _display_dependency_path(path: Path | None, env_dir: Path) -> str:
    if path is None:
        return ""
    try:
        return path.resolve().relative_to(env_dir.resolve()).as_posix()
    except ValueError:
        return path.name
    except OSError:
        return path.name


def _load_dependency_entries(path: Path | None) -> list[str]:
    if path is None or not path.exists() or not path.is_file():
        return []

    entries: list[str] = []
    try:
        for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            entries.append(_sanitize_dependency_display(line))
    except OSError:
        return []
    return entries


def _sanitize_dependency_display(value: str) -> str:
    cleaned, _ = _redact_abs_paths(redact_url_secrets(value.strip()))
    return cleaned


def _yes_no(value: object) -> str:
    return "yes" if bool(value) else "no"


def _format_size_bytes(value: object) -> str:
    if isinstance(value, int) and value >= 0:
        return f"{value:,} bytes"
    return "(unknown)"


def _append_markdown_section(lines: list[str], heading: str, items: list[object]) -> None:
    lines.extend(["", f"### {heading}"])
    if not items:
        lines.append("- `(none)`")
        return
    for item in items:
        lines.append(f"- `{str(item)}`")


# ── pip metadata sanitization ─────────────────────────────────────────────

_ABS_FILE_URI_RE = re.compile(r"(?i)file:///([A-Z]:[\\/][^\\s\"'<>]+)")
_ABS_WIN_RE = re.compile(r"(?i)([A-Z]:[\\/][^\\s\"'<>]+)")
_ABS_UNC_RE = re.compile(r"(\\\\[^\\s\"'<>]+)")

_TEXT_EXTS = {
    ".txt", ".json", ".ini", ".cfg", ".toml", ".yaml", ".yml", ".rst",
}
_TEXT_NAMES = {
    "METADATA", "PKG-INFO", "RECORD", "WHEEL", "SOURCES.txt",
    "top_level.txt", "entry_points.txt", "direct_url.json",
    "dependency_links.txt", "installed-files.txt", "requires.txt",
    "REQUESTED", "INSTALLER",
}
_MAX_METADATA_SIZE = 2 * 1024 * 1024  # 2 MB


def _redact_abs_paths(text: str) -> tuple[str, int]:
    """Redact absolute paths inside metadata text."""
    redacted = 0

    def _repl(_m):
        nonlocal redacted
        redacted += 1
        return "<redacted>"

    # Preserve file:// prefix when present
    def _repl_file(_m):
        nonlocal redacted
        redacted += 1
        return "file:///<redacted>"

    text = _ABS_FILE_URI_RE.sub(_repl_file, text)
    text = _ABS_WIN_RE.sub(_repl, text)
    text = _ABS_UNC_RE.sub(_repl, text)
    return text, redacted


def _sanitize_pip_metadata(py_root: Path, log_cb: LogCb) -> tuple[int, int]:
    """Remove absolute paths from pip metadata files only.

    Returns (files_redacted, paths_redacted).
    """
    site = py_root / "Lib" / "site-packages"
    if not site.exists():
        return 0, 0

    files_redacted = 0
    paths_redacted = 0

    meta_dirs = list(site.glob("*.dist-info")) + list(site.glob("*.egg-info"))
    for meta in meta_dirs:
        if not meta.is_dir():
            continue
        for path in meta.rglob("*"):
            if not path.is_file():
                continue
            name = path.name
            if name not in _TEXT_NAMES and path.suffix.lower() not in _TEXT_EXTS:
                continue
            try:
                if path.stat().st_size > _MAX_METADATA_SIZE:
                    continue
                raw = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            cleaned, count = _redact_abs_paths(raw)
            if count:
                try:
                    path.write_text(cleaned, encoding="utf-8")
                    files_redacted += 1
                    paths_redacted += count
                except OSError:
                    continue

    if files_redacted:
        log_cb(f"  Redacted {paths_redacted} absolute paths in {files_redacted} metadata files.")
    else:
        log_cb("  No absolute paths found in pip metadata.")
    return files_redacted, paths_redacted
