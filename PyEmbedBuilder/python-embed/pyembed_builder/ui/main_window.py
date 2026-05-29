"""
Main window and wizard pages for PyEmbedBuilder.

Wizard flow:  Setup  ->  Review  ->  Build  ->  Complete
"""
from __future__ import annotations

import os
import queue
import re
import shlex
import subprocess
import threading
import time
import traceback
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, ttk
from urllib.parse import urlparse

from ..models import BuildPlan, StepUpdate
from ..security import (
    normalize_project_git_source,
    sanitize_env_name,
    validate_project_source_url,
)
from ..services.env_builder import BuildResult, CancelledError, EnvBuilder
from ..services.exporter import export_portable_zip
from ..services.project_inspector import ProjectSourceAnalysis, analyze_project_source
from ..services.python_catalog import (
    has_embeddable_archive,
    load_cached_embeddable_versions,
    refresh_embeddable_versions_cache,
    resolve_embeddable_at_or_above,
)
from ..ui.theme import (
    BUILD_LOG_MONO_SIZE,
    CONTENT_WRAP_WIDTH,
    MONO_FONT,
    ScrollableFrame,
    SUCCESS_BANNER_SIZE,
    StepRow,
    ThemedDialog,
    ThemeManager,
    Tooltip,
    UI_FONT,
    VERSION_PICKER_MIN_SIZE,
    WINDOW_DEFAULT_SIZE,
    WINDOW_MIN_SIZE,
    WizardStepBar,
    themed_askyesno,
    themed_showinfo,
    themed_showerror,
    themed_showwarning,
)
from ..util.paths import (
    APP_DATA_DIRNAME,
    OUTPUT_BASE_DIRNAME,
    logs_dir,
    output_base_dir,
    project_root,
)
from ..util.preferences import (
    load_preferences,
    save_preferences,
    save_setup_config,
    load_setup_config,
    delete_setup_config,
    list_setup_configs,
)
from ..util.versioning import Version


DEFAULT_VERSION = Version.parse("3.11.9")
WIZARD_STEPS = ["Setup", "Review", "Build", "Complete"]
THEME_LABEL_TO_MODE: dict[str, str] = {
    "Light": "light",
    "Dark": "dark",
    "High-Contrast": "high_contrast",
}
THEME_MODE_TO_LABEL: dict[str, str] = {v: k for k, v in THEME_LABEL_TO_MODE.items()}
DEFAULT_ENV_NAME_RE = re.compile(r"^Project-\d{8}-\d{6}$")


def _rel_path(path: Path) -> str:
    """Return a path relative to the project root when possible."""
    try:
        return str(path.relative_to(project_root()))
    except Exception:
        try:
            return os.path.relpath(str(path), start=str(project_root()))
        except Exception:
            return str(path)


def _to_relative(path_str: str) -> str:
    """Convert an absolute path to project-relative when it is under the builder."""
    p = Path(path_str)
    if not p.is_absolute():
        return path_str
    try:
        root = project_root().resolve()
        rel = p.resolve().relative_to(root)
        return str(rel)
    except Exception:
        return path_str


def _rel_to_base(path: Path, base: Path) -> str:
    """Return a path relative to *base* when possible (fallback to builder-relative)."""
    try:
        rel = path.relative_to(base)
        return str(rel) if str(rel) else "."
    except Exception:
        return _rel_path(path)


def _display_output_path(path: Path) -> str:
    """Display output paths relative to the output base when possible."""
    try:
        rel = path.resolve().relative_to(output_base_dir().resolve())
        return str(Path(OUTPUT_BASE_DIRNAME) / rel)
    except Exception:
        return _rel_path(path)


def _tooling_summary(tooling: dict) -> str:
    labels: list[str] = []
    if not isinstance(tooling, dict):
        return "None"
    ffmpeg = tooling.get("ffmpeg", {})
    if isinstance(ffmpeg, dict) and ffmpeg.get("status") == "ok":
        labels.append("FFmpeg")
    return ", ".join(labels) if labels else "None"


def _parse_manual_packages(raw: str) -> tuple[str, ...]:
    """Parse manual package input into pip-installable tokens."""
    if not raw.strip():
        return ()
    normalized = raw.replace(",", " ")
    try:
        parsed = shlex.split(normalized, posix=False)
    except ValueError as exc:
        raise ValueError(f"Invalid manual package list: {exc}") from exc
    return tuple(p.strip() for p in parsed if p.strip())


def _safe_env_name_hint(raw: str) -> str:
    """Create a safe environment-name hint from arbitrary folder names."""
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", raw.strip())
    s = s.strip("._-")
    if not s:
        s = "ImportedProject"
    if not s[0].isalnum():
        s = f"P{s}"
    return s[:128]


def _disable_button_focus_recursive(widget: tk.Widget) -> None:
    """Disable keyboard focus for all ttk.Button descendants."""
    try:
        children = widget.winfo_children()
    except Exception:
        return
    for child in children:
        if isinstance(child, ttk.Button):
            try:
                child.configure(takefocus=False)
            except Exception:
                pass
        _disable_button_focus_recursive(child)


def _ask_setup_name(
    parent: tk.Tk,
    *,
    existing_names: list[str] | None = None,
    initial: str = "",
) -> str | None:
    """Show a themed dialog prompting the user for a setup configuration name."""
    dlg = ThemedDialog(
        parent,
        title="Save Setup Configuration",
        min_width=440,
        min_height=180,
        resizable=False,
    )
    result: list[str | None] = [None]

    body = ttk.Frame(dlg)
    body.pack(fill="both", expand=True, padx=16, pady=(16, 0))
    body.columnconfigure(0, weight=1)

    ttk.Label(body, text="Configuration name:", style="Heading.TLabel").grid(
        row=0, column=0, sticky="w", pady=(0, 6),
    )
    var = tk.StringVar(value=initial)
    entry = ttk.Entry(body, textvariable=var, width=40)
    entry.grid(row=1, column=0, sticky="ew")
    entry.focus_set()
    entry.select_range(0, "end")

    if existing_names:
        visible = [n for n in existing_names if not n.startswith("_")]
        if visible:
            ttk.Label(
                body, text="Existing configurations:", style="Muted.TLabel",
            ).grid(row=2, column=0, sticky="w", pady=(12, 4))

            lb = tk.Listbox(body, height=min(6, len(visible)), width=40)
            for n in visible:
                lb.insert("end", f"  {n}")
            lb.grid(row=3, column=0, sticky="ew")
            dlg.theme_listbox(lb)

            def _on_lb_select(_e=None) -> None:
                sel = lb.curselection()
                if sel:
                    var.set(visible[int(sel[0])])

            lb.bind("<<ListboxSelect>>", _on_lb_select)

    def _ok() -> None:
        name = var.get().strip()
        if name:
            result[0] = name
        dlg.destroy()

    btn_frame = ttk.Frame(dlg)
    btn_frame.pack(fill="x", padx=16, pady=(12, 16))
    btn_frame.columnconfigure(0, weight=1)

    ttk.Label(btn_frame, text="", style="Muted.TLabel").grid(
        row=0, column=0, sticky="w",
    )
    ttk.Button(
        btn_frame, text="Cancel", command=dlg.destroy,
    ).grid(row=0, column=1, sticky="e", padx=(0, 8))
    ttk.Button(
        btn_frame, text="Save", style="Accent.TButton", command=_ok,
    ).grid(row=0, column=2, sticky="e")

    entry.bind("<Return>", lambda _: _ok())
    dlg.center_over_parent()

    parent.wait_window(dlg)
    return result[0]


# ══════════════════════════════════════════════════════════════════════════
#  Main Application Window
# ══════════════════════════════════════════════════════════════════════════

class PyEmbedBuilderApp(tk.Tk):
    """Root window with wizard navigation."""

    def __init__(self) -> None:
        super().__init__()
        self._prefs = load_preferences()
        self.title("PyEmbedBuilder - Secure Embedded Python Environment Manager")
        self.minsize(*WINDOW_MIN_SIZE)

        # Centre on screen
        self.update_idletasks()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w, h = WINDOW_DEFAULT_SIZE
        self.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 2)}")
        saved_geo = self._prefs.get("window_geometry", "")
        if saved_geo:
            try:
                self.geometry(saved_geo)
            except Exception:
                pass

        # ── Theme ─────────────────────────────────────────────────────
        self._theme = ThemeManager(self)
        self.var_theme_mode = tk.StringVar(
            value=self._prefs.get("theme_mode", "Dark"),
        )
        _sizes = {"Small": 0.9, "Medium": 1.2, "Large": 1.6}
        _saved_size = self._prefs.get("text_size", "Large")
        self.var_text_scale = tk.DoubleVar(value=_sizes.get(_saved_size, 1.6))
        self.var_text_size = tk.StringVar(value=_saved_size)

        # ── Build-state variables ─────────────────────────────────────
        self.var_project_mode = tk.StringVar(value="create")  # "create" | "import" | "git" | "zip"
        self.var_env_name = tk.StringVar(value=self._default_env_name())
        self.var_target_dir = tk.StringVar(value=str(Path(self.var_env_name.get())))
        self.var_source_dir = tk.StringVar(value="")
        self.var_source_url = tk.StringVar(value="")
        self.var_source_ref = tk.StringVar(value="")
        self.var_entry_point = tk.StringVar(value="")
        self.var_auto_analyze_source = tk.BooleanVar(
            value=self._prefs.get("auto_analyze_source", True),
        )
        self.var_source_analysis_status = tk.StringVar(value="Source analysis not run yet.")
        self.var_window_only = tk.BooleanVar(value=False)
        self.var_create_desktop_shortcut = tk.BooleanVar(value=False)
        self.var_create_start_menu_shortcut = tk.BooleanVar(value=False)
        self.var_install_ffmpeg = tk.BooleanVar(value=False)
        self.var_ffmpeg_arch = tk.StringVar(
            value=self._prefs.get("ffmpeg_arch", "auto"),
        )
        self.var_python_mode = tk.StringVar(value="recommended")
        self.var_python_version = tk.StringVar(value=str(DEFAULT_VERSION))
        self.var_arch = tk.StringVar(
            value=self._prefs.get("arch", "amd64"),
        )
        self.var_use_requirements = tk.BooleanVar(value=False)
        self.var_requirements_path = tk.StringVar(value="")
        self.var_manual_packages = tk.StringVar(value="")
        self.var_dependency_no_deps = tk.BooleanVar(value=False)
        self.var_auto_install_project = tk.BooleanVar(
            value=self._prefs.get("auto_install_project", True),
        )
        self.var_use_pymanager_components = tk.BooleanVar(
            value=self._prefs.get("use_pymanager_components", True),
        )
        self.var_clear_cache = tk.BooleanVar(
            value=self._prefs.get("clear_cache", True),
        )
        self._target_custom = False
        self._suppress_target_trace = False
        self._last_text_size: str | None = None
        self._setting_auto_env_name = False
        self._env_name_user_modified = False
        self._last_git_auto_env_name = ""

        self._build_cancel = threading.Event()
        self._build_thread: threading.Thread | None = None
        self._ui_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self._build_result: BuildResult | None = None
        self._current_plan: BuildPlan | None = None
        self._source_analysis: ProjectSourceAnalysis | None = None
        self._source_analysis_key = ""
        self._current_page = 0
        self._build_state = "idle"   # "idle" | "running" | "cancelled" | "failed" | "succeeded"

        # ── Build UI ──────────────────────────────────────────────────
        self._build_ui()
        _disable_button_focus_recursive(self)
        self._install_text_context_menu_bindings()
        self._install_text_selection_cleanup_bindings()
        self._install_combobox_selection_bindings()
        self._apply_theme()

        # Sync target-dir whenever env name changes
        self.var_env_name.trace_add("write", self._on_env_name_change)
        self.var_target_dir.trace_add("write", self._on_target_dir_change)
        self.var_source_url.trace_add("write", self._on_source_url_change)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── helpers ────────────────────────────────────────────────────────

    def _install_text_context_menu_bindings(self) -> None:
        self._text_menu_target: tk.Widget | None = None
        self._text_menu = tk.Menu(self, tearoff=False)
        self._text_menu.add_command(label="Cut", command=self._text_menu_cut)
        self._text_menu.add_command(label="Copy", command=self._text_menu_copy)
        self._text_menu.add_command(label="Paste", command=self._text_menu_paste)
        self._text_menu.add_separator()
        self._text_menu.add_command(label="Select All", command=self._text_menu_select_all)
        for class_name in ("Entry", "TEntry", "Text", "TCombobox"):
            self.bind_class(class_name, "<Button-3>", self._show_text_context_menu, add="+")

    def _install_text_selection_cleanup_bindings(self) -> None:
        self._last_text_focus_widget: tk.Widget | None = None
        self.bind_all("<FocusIn>", self._on_global_focus_in, add="+")

    def _install_combobox_selection_bindings(self) -> None:
        self.bind_class("TCombobox", "<<ComboboxSelected>>", self._on_any_combobox_selected, add="+")
        self.bind_class("TCombobox", "<FocusIn>", self._on_any_combobox_focus_in, add="+")

    @staticmethod
    def _menu_widget_text(widget: tk.Widget) -> str:
        try:
            if isinstance(widget, tk.Text):
                return str(widget.get("1.0", "end-1c"))
            return str(widget.get())
        except Exception:
            return ""

    @staticmethod
    def _menu_widget_selection(widget: tk.Widget) -> str:
        try:
            if isinstance(widget, tk.Text):
                if widget.tag_ranges("sel"):
                    return str(widget.get("sel.first", "sel.last"))
                return ""
            if isinstance(widget, (tk.Entry, ttk.Entry, ttk.Combobox)):
                if bool(widget.selection_present()):
                    return str(widget.selection_get())
                return ""
            return ""
        except Exception:
            return ""

    @staticmethod
    def _menu_widget_is_editable(widget: tk.Widget) -> bool:
        try:
            return str(widget.cget("state")) not in {"disabled", "readonly"}
        except Exception:
            return True

    def _show_text_context_menu(self, event: tk.Event) -> str | None:
        widget = event.widget
        if not isinstance(widget, (tk.Entry, ttk.Entry, ttk.Combobox, tk.Text)):
            return None
        self._text_menu_target = widget
        has_text = bool(self._menu_widget_text(widget))
        has_selection = bool(self._menu_widget_selection(widget))
        editable = self._menu_widget_is_editable(widget)
        can_paste = editable and self._clipboard_has_text()

        self._text_menu.entryconfigure("Cut", state="normal" if editable and has_selection else "disabled")
        self._text_menu.entryconfigure("Copy", state="normal" if has_text else "disabled")
        self._text_menu.entryconfigure("Paste", state="normal" if can_paste else "disabled")
        self._text_menu.entryconfigure("Select All", state="normal" if has_text else "disabled")

        try:
            widget.focus_set()
        except Exception:
            pass
        try:
            self._text_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._text_menu.grab_release()
        return "break"

    @staticmethod
    def _is_text_like_widget(widget: tk.Widget) -> bool:
        return isinstance(widget, (tk.Entry, ttk.Entry, ttk.Combobox, tk.Text))

    def _clear_widget_selection(self, widget: tk.Widget | None) -> None:
        if widget is None:
            return
        try:
            if isinstance(widget, tk.Text):
                widget.tag_remove("sel", "1.0", "end")
            elif isinstance(widget, (tk.Entry, ttk.Entry, ttk.Combobox)):
                widget.selection_clear()
        except Exception:
            pass

    def _on_global_focus_in(self, event: tk.Event) -> None:
        widget = event.widget
        prev = self._last_text_focus_widget
        if prev is not None and prev is not widget:
            self._clear_widget_selection(prev)
        self._last_text_focus_widget = widget if self._is_text_like_widget(widget) else None

    def _clipboard_has_text(self) -> bool:
        try:
            return bool(self.clipboard_get())
        except Exception:
            return False

    def _text_menu_copy(self) -> None:
        widget = self._text_menu_target
        if widget is None:
            return
        text = self._menu_widget_selection(widget) or self._menu_widget_text(widget)
        if not text:
            return
        self.clipboard_clear()
        self.clipboard_append(text)

    def _text_menu_cut(self) -> None:
        widget = self._text_menu_target
        if widget is None or not self._menu_widget_is_editable(widget):
            return
        try:
            widget.event_generate("<<Cut>>")
        except Exception:
            pass

    def _text_menu_paste(self) -> None:
        widget = self._text_menu_target
        if widget is None or not self._menu_widget_is_editable(widget):
            return
        try:
            widget.event_generate("<<Paste>>")
        except Exception:
            pass

    def _text_menu_select_all(self) -> None:
        widget = self._text_menu_target
        if widget is None:
            return
        try:
            if isinstance(widget, tk.Text):
                widget.tag_add("sel", "1.0", "end-1c")
                widget.mark_set("insert", "end-1c")
                widget.see("insert")
            else:
                widget.selection_range(0, "end")
                widget.icursor("end")
        except Exception:
            pass

    @staticmethod
    def _default_env_name() -> str:
        return time.strftime("Project-%Y%m%d-%H%M%S")

    def _set_env_name_auto(self, raw_name: str, *, source: str = "") -> None:
        name = _safe_env_name_hint(raw_name)
        self._setting_auto_env_name = True
        try:
            self.var_env_name.set(name)
        finally:
            self._setting_auto_env_name = False
        self._env_name_user_modified = False
        if source == "git":
            self._last_git_auto_env_name = name

    @staticmethod
    def _extract_repo_name_from_git_source(raw: str) -> str:
        s = raw.strip()
        if not s:
            return ""
        try:
            s = normalize_project_git_source(s)
        except ValueError:
            pass

        repo = ""
        parsed = urlparse(s)
        if parsed.scheme.lower() in {"https", "ssh"} and parsed.path:
            repo = Path(parsed.path).name
        elif ":" in s and "@" in s.split(":", 1)[0]:
            repo = Path(s.split(":", 1)[1]).name
        else:
            repo = Path(s).name

        repo = repo.strip()
        if repo.lower().endswith(".git"):
            repo = repo[:-4]
        if not repo:
            return ""
        return _safe_env_name_hint(repo)

    def _apply_theme(self) -> None:
        mode = THEME_LABEL_TO_MODE.get(self.var_theme_mode.get(), "dark")
        self._theme.apply(
            mode=mode,
            scale=float(self.var_text_scale.get()),
        )
        c = self._theme.colors
        self._refresh_header_dropdown_theme()
        self.step_bar._redraw()
        for page in self._pages:
            if hasattr(page, "refresh_theme"):
                page.refresh_theme(c)

    def _apply_text_size(self) -> None:
        sizes = {"Small": 0.9, "Medium": 1.2, "Large": 1.6}
        size_label = self.var_text_size.get()
        if size_label == self._last_text_size:
            return
        self._last_text_size = size_label
        self.var_text_scale.set(sizes.get(size_label, 1.6))
        self._apply_theme()

    def _clear_combobox_selection(self, widget: tk.Widget | None) -> None:
        if not isinstance(widget, ttk.Combobox):
            return
        try:
            if str(widget.cget("state")) != "readonly":
                return
        except Exception:
            return
        self._clear_widget_selection(widget)
        try:
            widget.icursor("end")
        except Exception:
            pass

    def _on_any_combobox_selected(self, event: tk.Event) -> None:
        self.after_idle(lambda w=event.widget: self._clear_combobox_selection(w))

    def _on_any_combobox_focus_in(self, event: tk.Event) -> None:
        self.after_idle(lambda w=event.widget: self._clear_combobox_selection(w))

    def _on_theme_selected(self, event: tk.Event) -> None:
        self._apply_theme()

    def _on_text_size_selected(self, event: tk.Event) -> None:
        self._apply_text_size()

    def _on_env_name_change(self, *_a) -> None:
        if not self._setting_auto_env_name:
            self._env_name_user_modified = True
        name = self.var_env_name.get().strip()
        if name and not self._target_custom:
            self._suppress_target_trace = True
            try:
                self.var_target_dir.set(str(Path(name)))
            finally:
                self._suppress_target_trace = False

    def _on_target_dir_change(self, *_a) -> None:
        if not self._suppress_target_trace:
            self._target_custom = True

    def _on_source_url_change(self, *_a) -> None:
        if self.var_project_mode.get() != "git":
            return
        raw = self.var_source_url.get().strip()
        if not raw:
            return
        source_url, _inline_ref = self._split_git_url_and_inline_ref(raw)
        repo_name = self._extract_repo_name_from_git_source(source_url)
        if not repo_name:
            return

        current = self.var_env_name.get().strip()
        should_auto = (
            not current
            or not self._env_name_user_modified
            or current == self._last_git_auto_env_name
            or bool(DEFAULT_ENV_NAME_RE.fullmatch(current))
        )
        if should_auto and current != repo_name:
            self._set_env_name_auto(repo_name, source="git")

    def _current_version_or_default(self) -> Version:
        try:
            return Version.parse(self.var_python_version.get().strip())
        except ValueError:
            return DEFAULT_VERSION

    @staticmethod
    def _source_analysis_cache_key(
        *,
        mode: str,
        source_path: Path | None,
        source_url: str,
        source_ref: str,
        arch: str,
    ) -> str:
        path_part = ""
        if source_path is not None:
            try:
                path_part = str(source_path.resolve())
            except Exception:
                path_part = str(source_path)
        return "|".join([mode, path_part, source_url.strip(), source_ref.strip(), arch.strip()])

    @staticmethod
    def _split_git_url_and_inline_ref(raw: str) -> tuple[str, str]:
        s = raw.strip()
        if not s:
            return "", ""
        match = re.match(r"^(?P<url>.+?)\s+@\s+(?P<ref>\S.+)$", s)
        if not match:
            return s, ""
        url_part = (match.group("url") or "").strip()
        ref_part = (match.group("ref") or "").strip()
        if not url_part or not ref_part:
            return s, ""
        return url_part, ref_part

    def _clear_source_analysis(self) -> None:
        self._source_analysis = None
        self._source_analysis_key = ""
        self.var_source_analysis_status.set("Source analysis not run yet.")

    @staticmethod
    def _analysis_summary(analysis: ProjectSourceAnalysis) -> str:
        parts: list[str] = []
        if analysis.requirements_file:
            parts.append(f"requirements: {analysis.requirements_rel}")
        if analysis.pyproject_file:
            parts.append(f"pyproject: {analysis.pyproject_rel}")
        if analysis.requires_python:
            parts.append(f"requires-python: {analysis.requires_python}")
        if analysis.suggested_python:
            parts.append(f"suggested Python: {analysis.suggested_python}")
        if analysis.dependency_source == "requirements":
            parts.append("dependency source: requirements file")
        elif analysis.dependency_source == "pyproject":
            parts.append("dependency source: pyproject dependencies")
        else:
            parts.append("dependency source: none detected")
        parts.append(analysis.python_note)
        return " | ".join(parts)

    def _analyze_source_metadata(
        self,
        *,
        mode: str,
        source_path: Path | None,
        source_url: str,
        source_ref: str,
        arch: str,
    ) -> ProjectSourceAnalysis:
        key = self._source_analysis_cache_key(
            mode=mode,
            source_path=source_path,
            source_url=source_url,
            source_ref=source_ref,
            arch=arch,
        )
        if self._source_analysis is not None and self._source_analysis_key == key:
            return self._source_analysis

        prev_cursor = self.cget("cursor")
        self.configure(cursor="watch")
        self.update_idletasks()
        try:
            analysis = analyze_project_source(
                mode=mode,
                source_path=source_path,
                source_url=source_url,
                source_ref=source_ref,
                arch=arch,
                preferred_version=self._current_version_or_default(),
            )
        finally:
            self.configure(cursor=prev_cursor)
            self.update_idletasks()

        self._source_analysis = analysis
        self._source_analysis_key = key
        self.var_source_analysis_status.set(self._analysis_summary(analysis))
        return analysis

    def _resolve_embeddable_version(self, version: Version, arch: str) -> Version:
        try:
            if has_embeddable_archive(version, arch):
                return version
            fallback = resolve_embeddable_at_or_above(version, arch)
        except Exception as exc:
            themed_showwarning(self,
                "Embeddable Version Check",
                "Could not verify embeddable availability from python.org.\n"
                f"Continuing with Python {version}.\n\nDetails: {exc}",
            )
            return version

        if fallback is None:
            raise ValueError(
                f"No embeddable Python release is available for {arch} at or above {version}."
            )
        if fallback != version:
            self.var_python_mode.set("custom")
            self.var_python_version.set(str(fallback))
            self.pg_setup.sync_python_mode()
            themed_showinfo(self,
                "Python Version Updated",
                f"Python {version} ({arch}) has no embeddable ZIP.\n"
                f"Using the next higher embeddable version: {fallback}.",
            )
        return fallback

    def _on_close(self) -> None:
        """Handle window close - cancel running build first if needed."""
        if self._build_state == "running":
            if not themed_askyesno(self,
                "Build Running",
                "A build is currently running.\n\n"
                "Cancel the build and close the application?",
            ):
                return
            self._build_cancel.set()
            if self._build_thread and self._build_thread.is_alive():
                self._build_thread.join(timeout=3.0)
        self._save_prefs()
        self.destroy()

    def _save_prefs(self) -> None:
        """Persist user preferences to disk."""
        try:
            geo = self.geometry()
        except Exception:
            geo = ""
        save_preferences({
            "theme_mode": self.var_theme_mode.get(),
            "text_size": self.var_text_size.get(),
            "arch": self.var_arch.get(),
            "project_mode": self.var_project_mode.get(),
            "window_geometry": geo,
            "ffmpeg_arch": self.var_ffmpeg_arch.get(),
            "use_pymanager_components": self.var_use_pymanager_components.get(),
            "clear_cache": self.var_clear_cache.get(),
            "auto_analyze_source": self.var_auto_analyze_source.get(),
            "auto_install_project": self.var_auto_install_project.get(),
        })

    def _show_about(self) -> None:
        """Show application version and info in a themed dialog."""
        from .. import __version__
        dlg = ThemedDialog(
            self,
            title="About PyEmbedBuilder",
            min_width=420,
            min_height=240,
            resizable=False,
        )
        c = dlg.colors
        sc = dlg.scale

        body = ttk.Frame(dlg)
        body.pack(fill="both", expand=True, padx=24, pady=(20, 8))
        body.columnconfigure(0, weight=1)

        ttk.Label(
            body,
            text=f"PyEmbedBuilder v{__version__}",
            style="Heading.TLabel",
        ).grid(row=0, column=0, sticky="w", pady=(0, 4))

        ttk.Label(
            body,
            text="Secure Embedded Python Environment Manager",
            style="Secondary.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(0, 12))

        info_text = (
            "Create fully portable, self-contained Python\n"
            "environments for Windows - no system Python required.\n\n"
            "All downloads are HTTPS-only with strict source policies.\n"
            "Every download and build step is transparently\n"
            "recorded in security_audit.log for your review."
        )
        ttk.Label(
            body,
            text=info_text,
            wraplength=380,
            justify="left",
        ).grid(row=2, column=0, sticky="w")

        btn_frame = ttk.Frame(dlg)
        btn_frame.pack(fill="x", padx=24, pady=(8, 20))
        ttk.Button(
            btn_frame, text="Close", style="Accent.TButton",
            command=dlg.destroy,
        ).pack(side="right")

        dlg.center_over_parent()

    # ── Setup configuration save / load ───────────────────────────────

    def _capture_setup_fields(self) -> dict:
        """Snapshot all setup form fields into a dict."""
        return {
            "project_mode": self.var_project_mode.get(),
            "env_name": self.var_env_name.get(),
            "target_dir": self.var_target_dir.get(),
            "source_dir": self.var_source_dir.get(),
            "source_url": self.var_source_url.get(),
            "source_ref": self.var_source_ref.get(),
            "entry_point": self.var_entry_point.get(),
            "window_only": self.var_window_only.get(),
            "create_desktop_shortcut": self.var_create_desktop_shortcut.get(),
            "create_start_menu_shortcut": self.var_create_start_menu_shortcut.get(),
            "install_ffmpeg": self.var_install_ffmpeg.get(),
            "ffmpeg_arch": self.var_ffmpeg_arch.get(),
            "python_mode": self.var_python_mode.get(),
            "python_version": self.var_python_version.get(),
            "arch": self.var_arch.get(),
            "use_requirements": self.var_use_requirements.get(),
            "requirements_path": self.var_requirements_path.get(),
            "manual_packages": self.var_manual_packages.get(),
            "dependency_no_deps": self.var_dependency_no_deps.get(),
            "auto_install_project": self.var_auto_install_project.get(),
            "auto_analyze_source": self.var_auto_analyze_source.get(),
            "use_pymanager_components": self.var_use_pymanager_components.get(),
            "clear_cache": self.var_clear_cache.get(),
        }

    def _restore_setup_fields(self, fields: dict) -> None:
        """Restore all setup form fields from a saved configuration dict."""
        def _s(var: tk.StringVar, key: str) -> None:
            if key in fields:
                var.set(str(fields[key]))

        def _b(var: tk.BooleanVar, key: str) -> None:
            if key in fields:
                var.set(bool(fields[key]))

        self._suppress_target_trace = True
        try:
            _s(self.var_project_mode, "project_mode")
            _s(self.var_env_name, "env_name")
            _s(self.var_target_dir, "target_dir")
            _s(self.var_source_dir, "source_dir")
            _s(self.var_source_url, "source_url")
            _s(self.var_source_ref, "source_ref")
            _s(self.var_entry_point, "entry_point")
            _b(self.var_window_only, "window_only")
            _b(self.var_create_desktop_shortcut, "create_desktop_shortcut")
            _b(self.var_create_start_menu_shortcut, "create_start_menu_shortcut")
            _b(self.var_install_ffmpeg, "install_ffmpeg")
            _s(self.var_ffmpeg_arch, "ffmpeg_arch")
            _s(self.var_python_mode, "python_mode")
            _s(self.var_python_version, "python_version")
            _s(self.var_arch, "arch")
            _b(self.var_use_requirements, "use_requirements")
            _s(self.var_requirements_path, "requirements_path")
            _s(self.var_manual_packages, "manual_packages")
            _b(self.var_dependency_no_deps, "dependency_no_deps")
            _b(self.var_auto_install_project, "auto_install_project")
            _b(self.var_auto_analyze_source, "auto_analyze_source")
            _b(self.var_use_pymanager_components, "use_pymanager_components")
            _b(self.var_clear_cache, "clear_cache")
        finally:
            self._suppress_target_trace = False

        self._target_custom = bool(fields.get("target_dir", ""))
        self._env_name_user_modified = True
        self._clear_source_analysis()
        self.pg_setup.sync_python_mode()
        self.pg_setup.sync_dependency_ui()
        self.pg_setup.sync_shortcut_ui()
        self.pg_setup.sync_tooling_ui()
        self.pg_setup._sync_project_mode()

    def save_setup_as(self) -> None:
        """Prompt the user for a name and save the current setup configuration."""
        existing = list_setup_configs()
        name = _ask_setup_name(self, existing_names=existing)
        if not name:
            return
        try:
            fields = self._capture_setup_fields()
            save_setup_config(name, fields)
            themed_showinfo(self,
                "Setup Saved",
                f"Configuration saved as: {name}",
            )
            self.pg_setup._refresh_setup_list()
        except Exception as exc:
            themed_showerror(self, "Save Failed", str(exc))

    def load_setup_from(self, name: str) -> None:
        """Load a named setup configuration into the form."""
        try:
            fields = load_setup_config(name)
        except Exception as exc:
            themed_showerror(self, "Load Failed", str(exc))
            return
        self._restore_setup_fields(fields)
        self.var_source_analysis_status.set(
            f"Loaded configuration: {name}. Source analysis will re-run on next review."
        )

    def delete_setup(self, name: str) -> None:
        """Delete a named setup configuration after confirmation."""
        if not themed_askyesno(self,
            "Delete Configuration",
            f"Delete saved setup configuration \"{name}\"?",
        ):
            return
        try:
            delete_setup_config(name)
            self.pg_setup._refresh_setup_list()
        except Exception as exc:
            themed_showerror(self, "Delete Failed", str(exc))

    def _auto_save_setup(self, label: str) -> None:
        """Silently auto-save the current setup after a successful build."""
        try:
            fields = self._capture_setup_fields()
            save_setup_config(label, fields)
            self.pg_setup._refresh_setup_list()
        except Exception:
            pass

    # ── Layout ────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        outer = ttk.Frame(self)
        outer.grid(row=0, column=0, sticky="nsew")
        outer.columnconfigure(0, weight=1)
        # Keep footer always visible; only the page area expands
        outer.rowconfigure(3, weight=1)

        # ── Header ────────────────────────────────────────────────────
        hdr = ttk.Frame(outer)
        hdr.grid(row=0, column=0, sticky="ew", padx=20, pady=(16, 0))
        hdr.columnconfigure(0, weight=1)

        ttk.Label(
            hdr, text="PyEmbedBuilder", style="Title.TLabel",
        ).grid(row=0, column=0, sticky="w")

        ctrl = ttk.Frame(hdr)
        ctrl.grid(row=0, column=1, sticky="e")

        ttk.Label(ctrl, text="Theme:").grid(row=0, column=0, padx=(0, 4))
        self.theme_cb = ttk.Combobox(
            ctrl, textvariable=self.var_theme_mode,
            values=tuple(THEME_LABEL_TO_MODE.keys()),
            state="readonly", width=14, style="Header.TCombobox",
        )
        self.theme_cb.grid(row=0, column=1, padx=(0, 16))
        self.theme_cb.bind("<<ComboboxSelected>>", self._on_theme_selected)
        Tooltip(self.theme_cb, "Switch between Light, Dark, and High-Contrast themes")

        ttk.Label(ctrl, text="Text size:").grid(row=0, column=2, padx=(0, 4))
        self.size_cb = ttk.Combobox(
            ctrl, textvariable=self.var_text_size,
            values=("Small", "Medium", "Large"),
            state="readonly", width=10, style="Header.TCombobox",
        )
        self.size_cb.grid(row=0, column=3, padx=(0, 4))
        self.size_cb.bind("<<ComboboxSelected>>", self._on_text_size_selected)
        Tooltip(self.size_cb, "Choose small, medium, or large text size")

        about_btn = ttk.Button(
            ctrl, text="About", command=self._show_about,
        )
        about_btn.grid(row=0, column=4, padx=(12, 0))

        # ── Step bar ──────────────────────────────────────────────────
        self.step_bar = WizardStepBar(outer, WIZARD_STEPS, self._theme)
        self.step_bar.grid(row=1, column=0, sticky="ew", padx=20, pady=(12, 0))

        ttk.Separator(outer).grid(row=2, column=0, sticky="ew", padx=20, pady=(8, 0))

        # ── Page container ────────────────────────────────────────────
        self._page_frame = ttk.Frame(outer)
        self._page_frame.grid(row=3, column=0, sticky="nsew", padx=20, pady=8)

        self.pg_setup = _SetupPage(self._page_frame, app=self)
        self.pg_review = _ReviewPage(self._page_frame, app=self)
        self.pg_build = _BuildPage(self._page_frame, app=self)
        self.pg_complete = _CompletePage(self._page_frame, app=self)
        self._pages: list[ttk.Frame] = [
            self.pg_setup, self.pg_review, self.pg_build, self.pg_complete,
        ]

        # ── Footer ────────────────────────────────────────────────────
        ttk.Separator(outer).grid(row=4, column=0, sticky="ew", padx=20)
        ftr = ttk.Frame(outer)
        ftr.grid(row=5, column=0, sticky="ew", padx=20, pady=(8, 16))
        ftr.columnconfigure(0, weight=1)

        self.lbl_footer = ttk.Label(ftr, text="", style="Muted.TLabel")
        self.lbl_footer.grid(row=0, column=0, sticky="w")

        self.btn_back = ttk.Button(ftr, text="\u2190  Back", command=self._on_back)
        self.btn_cancel = ttk.Button(
            ftr, text="Cancel Build", style="Danger.TButton",
            command=self.cancel_build,
        )
        self.btn_next = ttk.Button(
            ftr, text="Review Plan  \u2192", style="Accent.TButton",
            command=self._on_next,
        )
        Tooltip(self.btn_back, "Return to the previous wizard step.")
        Tooltip(
            self.btn_cancel,
            "Stop the active build. Partially created files may remain in the output folder.",
        )
        Tooltip(
            self.btn_next,
            "Continue to the next step. On Setup, this generates the review plan.",
        )

        self.bind_all("<Control-Return>", lambda _: self._on_next())
        self.bind_all("<Escape>", lambda _: self._on_escape())

        self._show_page(0)

    def _refresh_header_dropdown_theme(self) -> None:
        """Refresh top-bar combobox widget and popup colors after theme changes."""
        entry_font = (UI_FONT, max(9, int(round(10 * self._theme.scale))))
        for cb in (getattr(self, "theme_cb", None), getattr(self, "size_cb", None)):
            if not isinstance(cb, ttk.Combobox):
                continue
            try:
                cb.configure(style="Header.TCombobox", font=entry_font)
            except Exception:
                pass
            self.after_idle(lambda widget=cb: self._clear_combobox_selection(widget))
            self._theme.theme_combobox_popdown(cb, entry_font=entry_font)

    # ── Navigation ────────────────────────────────────────────────────

    def _show_page(self, idx: int) -> None:
        for p in self._pages:
            p.pack_forget()
        self._pages[idx].pack(fill="both", expand=True)
        if hasattr(self._pages[idx], "scroll_to_top"):
            self._pages[idx].scroll_to_top()
        self._current_page = idx
        self.step_bar.set_active(idx)
        self._update_nav()

    def _update_nav(self) -> None:
        idx = self._current_page
        self.btn_back.grid_forget()
        self.btn_cancel.grid_forget()
        self.btn_next.grid_forget()
        self.btn_back.configure(text="\u2190  Back")

        if idx == 0:  # Setup
            self.btn_next.configure(text="Review Plan  \u2192")
            self.btn_next.grid(row=0, column=2, sticky="e")
            self.lbl_footer.configure(text="Configure your environment, then review the plan.")
        elif idx == 1:  # Review
            self.btn_back.grid(row=0, column=1, sticky="e", padx=(0, 8))
            self.btn_next.configure(text="Start Build  \u2192")
            self.btn_next.grid(row=0, column=2, sticky="e")
            self.lbl_footer.configure(text="Verify the plan, then start the build.")
        elif idx == 2:  # Build
            if self._build_state == "running":
                self.btn_cancel.grid(row=0, column=2, sticky="e")
                self.lbl_footer.configure(text="Build in progress\u2026")
            else:
                self.btn_back.configure(text="\u2190  Review Plan")
                self.btn_back.grid(row=0, column=1, sticky="e", padx=(0, 8))
                self.btn_next.configure(text="Retry Build  \u2192")
                self.btn_next.grid(row=0, column=2, sticky="e")
                if self._build_state == "cancelled":
                    self.lbl_footer.configure(
                        text="Build cancelled. Review log details and retry when ready."
                    )
                elif self._build_state == "failed":
                    self.lbl_footer.configure(
                        text="Build failed. Fix inputs or network conditions, then retry."
                    )
                else:
                    self.lbl_footer.configure(text="Ready to run build.")
        elif idx == 3:  # Complete
            self.btn_back.configure(text="\u2190  Setup")
            self.btn_back.grid(row=0, column=1, sticky="e", padx=(0, 8))
            self.btn_next.configure(text="Build Another")
            self.btn_next.grid(row=0, column=2, sticky="e")
            self.lbl_footer.configure(text="Environment ready.")

    def _on_next(self) -> None:
        if self._current_page == 0:
            plan = self._validate_setup()
            if plan:
                self._current_plan = plan
                self.pg_review.refresh(plan)
                self._show_page(1)
        elif self._current_page == 1:
            self._start_build()
        elif self._current_page == 2 and self._build_state != "running":
            self._start_build()
        elif self._current_page == 3:
            self._reset_for_new_build()

    def _on_back(self) -> None:
        if self._current_page == 1:
            self._show_page(0)
        elif self._current_page == 2 and self._build_state != "running":
            self._show_page(1)
        elif self._current_page == 3:
            self._show_page(0)

    def _on_escape(self) -> None:
        """Handle Escape key - cancel build if running, else go back."""
        if self._build_state == "running":
            self.cancel_build()
        elif self._current_page > 0:
            self._on_back()

    # ── Build actions ─────────────────────────────────────────────────

    def browse_requirements(self) -> None:
        path = filedialog.askopenfilename(
            title="Select requirements.txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if path:
            rel = _to_relative(path)
            self.var_requirements_path.set(rel)
            self.var_use_requirements.set(True)

    def browse_target_dir(self) -> None:
        path = filedialog.askdirectory(title="Select output location for portable environment")
        if path:
            rel = _to_relative(path)
            self.var_target_dir.set(rel)
            self._target_custom = True

    def browse_source_dir(self) -> None:
        path = filedialog.askdirectory(title="Select existing project folder")
        if not path:
            return
        rel = _to_relative(path)
        self.var_source_dir.set(rel)
        if self.var_project_mode.get() == "import":
            self._set_env_name_auto(Path(path).name)

    def browse_entry_point(self) -> None:
        mode = self.var_project_mode.get()
        if mode in {"git", "zip"}:
            themed_showinfo(self,
                "Entry Point",
                "For Git/ZIP sources, enter the entry-point path manually "
                "(for example: main.py or src\\app.py), or leave it blank "
                "to fall back to main.py.",
            )
            return

        if mode == "import":
            src_raw = self.var_source_dir.get().strip()
            if not src_raw:
                themed_showwarning(self,
                    "Source Folder Required",
                    "Select a local source folder first.",
                )
                return
            base = Path(src_raw)
            if not base.is_absolute():
                base = project_root() / base
        else:
            target_raw = self.var_target_dir.get().strip()
            base = Path(target_raw) if target_raw else output_base_dir()
            if not base.is_absolute():
                base = output_base_dir() / base
            env_name = self.var_env_name.get().strip()
            if self._target_custom and env_name and base.name != env_name:
                base = base / env_name

        start_dir = base if base.exists() else (base.parent if base.parent.exists() else project_root())
        path = filedialog.askopenfilename(
            title="Select Python entry point",
            initialdir=str(start_dir),
            filetypes=[("Python files", "*.py"), ("All files", "*.*")],
        )
        if not path:
            return

        p = Path(path)
        try:
            rel = p.resolve().relative_to(base.resolve())
            self.var_entry_point.set(str(rel))
        except Exception:
            self.var_entry_point.set(path)

    def pick_python_version(self) -> None:
        dlg = _VersionPickerDialog(
            self,
            arch=self.var_arch.get(),
        )
        self.wait_window(dlg)
        if dlg.selected_version:
            self.var_python_mode.set("custom")
            self.var_python_version.set(str(dlg.selected_version))
            # Keep version field editable when user chooses custom mode via dialog.
            self.pg_setup.sync_python_mode()

    def _validate_setup(self) -> BuildPlan | None:
        mode = self.var_project_mode.get().strip().lower()
        if mode not in {"create", "import", "git", "zip"}:
            mode = "create"

        # Environment name
        raw_name = self.var_env_name.get().strip()
        try:
            env_name = sanitize_env_name(raw_name)
        except ValueError as e:
            themed_showerror(self, "Invalid environment name", str(e))
            return None

        # Output directory
        target = self.var_target_dir.get().strip()
        if not target:
            themed_showerror(self,
                "Missing location",
                "Please choose an output location for the portable environment.",
            )
            return None
        rel_target = _to_relative(target)
        if rel_target != target:
            self.var_target_dir.set(rel_target)
        target_dir = Path(rel_target)
        if not target_dir.is_absolute():
            target_dir = output_base_dir() / target_dir

        # If user picked a base folder, append env name as subfolder
        if self._target_custom and target_dir.name != env_name:
            target_dir = target_dir / env_name
            self._suppress_target_trace = True
            try:
                self.var_target_dir.set(str(target_dir))
            finally:
                self._suppress_target_trace = False

        # Warn if target exists
        if target_dir.exists() and any(target_dir.iterdir()):
            ok = themed_askyesno(self,
                "Folder not empty",
                f"The folder already exists and is not empty:\n{target_dir}\n\n"
                "Contents may be overwritten. Continue?",
            )
            if not ok:
                return None

        # Source selection (optional for create mode)
        source_path: Path | None = None
        source_url = ""
        source_ref = ""
        analysis: ProjectSourceAnalysis | None = None

        if mode == "import":
            source_raw = self.var_source_dir.get().strip()
            if not source_raw:
                themed_showerror(self,
                    "Missing source folder",
                    "Please select an existing local project folder to import.",
                )
                return None
            rel_source = _to_relative(source_raw)
            if rel_source != source_raw:
                self.var_source_dir.set(rel_source)
            source_path = Path(rel_source)
            if not source_path.is_absolute():
                source_path = project_root() / source_path
            if not source_path.exists() or not source_path.is_dir():
                themed_showerror(self,
                    "Project folder not found",
                    f"The selected source folder does not exist:\n{source_path}",
                )
                return None

            src_res = source_path.resolve()
            dst_res = target_dir.resolve()
            if src_res != dst_res:
                try:
                    dst_res.relative_to(src_res)
                except ValueError:
                    pass
                else:
                    themed_showerror(self,
                        "Invalid output location",
                        "Output folder cannot be inside the source project folder.",
                    )
                    return None
                try:
                    src_res.relative_to(dst_res)
                except ValueError:
                    pass
                else:
                    themed_showerror(self,
                        "Invalid output location",
                        "Source project folder cannot be inside the output folder.",
                    )
                    return None
        elif mode in {"git", "zip"}:
            source_url_input = self.var_source_url.get().strip()
            if not source_url_input:
                themed_showerror(self,
                    "Missing source URL",
                    "Please enter the repository/ZIP URL for your project source.",
                )
                return None
            if mode == "git":
                source_ref = self.var_source_ref.get().strip()
                source_url, inline_ref = self._split_git_url_and_inline_ref(source_url_input)
                if inline_ref and not source_ref:
                    source_ref = inline_ref
                    self.var_source_ref.set(source_ref)
                if source_url != source_url_input:
                    self.var_source_url.set(source_url)
                try:
                    source_url = normalize_project_git_source(source_url)
                except ValueError as e:
                    themed_showerror(self, "Invalid source URL", str(e))
                    return None
                self.var_source_url.set(source_url)
            else:
                source_url = source_url_input
            try:
                validate_project_source_url(source_url, mode)
            except ValueError as e:
                themed_showerror(self, "Invalid source URL", str(e))
                return None

        if mode == "create":
            self._clear_source_analysis()
            self.var_source_analysis_status.set("Create mode has no source metadata to analyze.")
        elif self.var_auto_analyze_source.get():
            try:
                analysis = self._analyze_source_metadata(
                    mode=mode,
                    source_path=source_path,
                    source_url=source_url,
                    source_ref=source_ref,
                    arch=self.var_arch.get(),
                )
            except Exception as exc:
                self.var_source_analysis_status.set(f"Source analysis failed: {exc}")
                themed_showwarning(self,
                    "Source Analysis Failed",
                    "Could not analyze source metadata before build.\n"
                    f"Continuing with manual settings.\n\nDetails: {exc}",
                )
                analysis = None
            else:
                if (
                    mode == "import"
                    and analysis.requirements_file is not None
                    and not self.var_use_requirements.get()
                ):
                    self.var_requirements_path.set(_to_relative(str(analysis.requirements_file)))
                    self.var_use_requirements.set(True)
                    self.pg_setup.sync_dependency_ui()
                if (
                    analysis.suggested_python is not None
                    and analysis.requested_python is not None
                    and self.var_python_mode.get() == "recommended"
                ):
                    self.var_python_mode.set("custom")
                    self.var_python_version.set(str(analysis.suggested_python))
                    self.pg_setup.sync_python_mode()
        else:
            self._source_analysis = None
            self._source_analysis_key = ""
            self.var_source_analysis_status.set("Source analysis disabled.")

        # Entry-point file (optional; missing/blank falls back to main.py)
        entry_raw = self.var_entry_point.get().strip()
        entry_rel_s = ""
        if entry_raw:
            entry_p = Path(entry_raw)
            entry_base = source_path if mode == "import" and source_path else target_dir
            if entry_p.is_absolute():
                try:
                    entry_rel = entry_p.resolve().relative_to(entry_base.resolve())
                except Exception:
                    themed_showerror(self,
                        "Invalid entry point",
                        "Entry point must be inside the selected project source/output folder.",
                    )
                    return None
            else:
                entry_rel = entry_p
            if entry_rel.is_absolute() or ".." in entry_rel.parts:
                themed_showerror(self,
                    "Invalid entry point",
                    "Entry point must be a safe relative path inside your project folder.",
                )
                return None
            if entry_rel.suffix.lower() != ".py":
                themed_showerror(self,
                    "Invalid entry point",
                    "Entry point must be a .py file.",
                )
                return None
            entry_rel_s = str(entry_rel).replace("/", "\\")
            entry_abs = (entry_base / entry_rel).resolve()
            try:
                entry_abs.relative_to(entry_base.resolve())
            except Exception:
                themed_showerror(self,
                    "Invalid entry point",
                    "Entry point resolves outside your selected source/output folder.",
                )
                return None
            if entry_abs.exists() and not entry_abs.is_file():
                themed_showerror(self,
                    "Invalid entry point",
                    "Entry point path exists but is not a file.",
                )
                return None
            self.var_entry_point.set(entry_rel_s)
        else:
            self.var_entry_point.set("")

        # Python version
        try:
            version = Version.parse(self.var_python_version.get().strip())
        except ValueError:
            themed_showerror(self,
                "Invalid version",
                "Python version must be in X.Y.Z format, for example 3.7.9 or 3.13.2.",
            )
            return None

        arch = self.var_arch.get()
        try:
            version = self._resolve_embeddable_version(version, arch)
        except ValueError as exc:
            themed_showerror(self, "Unsupported Python Version", str(exc))
            return None

        # Requirements
        req_path: Path | None = None
        if self.var_use_requirements.get():
            req_raw = self.var_requirements_path.get().strip() or ""
            rel_req = _to_relative(req_raw) if req_raw else ""
            if rel_req and rel_req != req_raw:
                self.var_requirements_path.set(rel_req)
            p = Path(rel_req)
            if not p.is_absolute():
                candidates: list[Path] = []
                if source_path is not None:
                    candidates.append(source_path / p)
                candidates.append(project_root() / p)
                p = next((c for c in candidates if c.exists()), candidates[-1])
            if not p.exists() or not p.is_file():
                themed_showerror(self,
                    "File not found",
                    "The selected requirements.txt does not exist.",
                )
                return None
            req_path = p

        # Manual package list
        try:
            manual_packages = _parse_manual_packages(
                self.var_manual_packages.get().strip()
            )
        except ValueError as e:
            themed_showerror(self, "Invalid package list", str(e))
            return None

        ffmpeg_arch = self.var_ffmpeg_arch.get().strip().lower()
        if ffmpeg_arch not in {"auto", "x64", "x86"}:
            ffmpeg_arch = "auto"
            self.var_ffmpeg_arch.set(ffmpeg_arch)
        if self.var_install_ffmpeg.get() and ffmpeg_arch == "x86":
            themed_showerror(
                self,
                "FFmpeg x86 unavailable",
                "The selected gyan.dev FFmpeg source currently publishes "
                "64-bit Windows builds only. Choose auto or x64.",
            )
            return None

        return BuildPlan(
            env_name=env_name,
            target_dir=target_dir,
            project_mode=mode,
            source_path=source_path,
            source_url=source_url,
            source_ref=source_ref,
            entry_point_rel=entry_rel_s,
            window_only=self.var_window_only.get(),
            create_desktop_shortcut=self.var_create_desktop_shortcut.get(),
            create_start_menu_shortcut=self.var_create_start_menu_shortcut.get(),
            install_ffmpeg=self.var_install_ffmpeg.get(),
            ffmpeg_arch=ffmpeg_arch,
            version=version,
            arch=arch,
            dependency_no_deps=self.var_dependency_no_deps.get(),
            auto_install_project=self.var_auto_install_project.get(),
            requirements_txt=req_path,
            manual_packages=manual_packages,
            use_pymanager_components=self.var_use_pymanager_components.get(),
            clear_cache_on_success=self.var_clear_cache.get(),
        )

    def _start_build(self) -> None:
        if self._build_thread and self._build_thread.is_alive():
            themed_showinfo(self, "Busy", "A build is already running.")
            return

        plan = self._current_plan
        if not plan:
            return

        self._build_cancel.clear()
        self._build_state = "running"
        self._build_result = None
        self.pg_build.reset_ui()
        self._show_page(2)

        builder = EnvBuilder(cancel_event=self._build_cancel)

        def _step(u):
            self._ui_queue.put(("step", u))

        def _log(line):
            self._ui_queue.put(("log", line))

        def _progress(d, t):
            self._ui_queue.put(("progress", (d, t)))

        def _worker():
            try:
                res = builder.build(
                    plan, step_cb=_step, log_cb=_log, progress_cb=_progress,
                )
                self._ui_queue.put(("done", res))
            except CancelledError as e:
                self._ui_queue.put(("cancelled", str(e)))
            except Exception as e:
                tb = traceback.format_exc()
                self._ui_queue.put(("error", (str(e), tb)))

        self._build_thread = threading.Thread(target=_worker, daemon=True)
        self._build_thread.start()
        self.after(50, self._poll_queue)

    def cancel_build(self) -> None:
        if self._build_state == "running":
            self._build_cancel.set()

    def _poll_queue(self) -> None:
        try:
            while True:
                kind, payload = self._ui_queue.get_nowait()
                if kind == "step":
                    self.pg_build.on_step_update(payload)
                elif kind == "log":
                    self.pg_build.on_log(payload)
                elif kind == "progress":
                    self.pg_build.on_progress(*payload)
                elif kind == "done":
                    self._build_state = "succeeded"
                    self._build_result = payload
                    self.pg_build.on_done(payload)
                    self.pg_complete.set_result(payload)
                    env_name = getattr(self._current_plan, "env_name", "")
                    if env_name:
                        self._auto_save_setup(env_name)
                    self._show_page(3)
                elif kind == "cancelled":
                    self._build_state = "cancelled"
                    self.pg_build.on_cancelled(payload)
                    self._update_nav()
                elif kind == "error":
                    self._build_state = "failed"
                    msg, tb = payload if isinstance(payload, tuple) else (str(payload), "")
                    self.pg_build.on_error(msg, tb)
                    self._update_nav()
        except queue.Empty:
            pass

        if self._build_thread and self._build_thread.is_alive():
            self.after(80, self._poll_queue)
        elif self._build_state == "running":
            try:
                while True:
                    kind, payload = self._ui_queue.get_nowait()
                    if kind == "done":
                        self._build_state = "succeeded"
                        self._build_result = payload
                        self.pg_build.on_done(payload)
                        self.pg_complete.set_result(payload)
                        env_name = getattr(self._current_plan, "env_name", "")
                        if env_name:
                            self._auto_save_setup(env_name)
                        self._show_page(3)
                        return
                    elif kind == "cancelled":
                        self._build_state = "cancelled"
                        self.pg_build.on_cancelled(payload)
                        self._update_nav()
                        return
                    elif kind == "error":
                        self._build_state = "failed"
                        msg, tb = payload if isinstance(payload, tuple) else (str(payload), "")
                        self.pg_build.on_error(msg, tb)
                        self._update_nav()
                        return
                    elif kind == "step":
                        self.pg_build.on_step_update(payload)
                    elif kind == "log":
                        self.pg_build.on_log(payload)
            except queue.Empty:
                pass
            if self._build_state == "running":
                self._build_state = "failed"
                self.pg_build.on_error(
                    "Build thread exited unexpectedly without reporting a result.", ""
                )
                self._update_nav()

    def open_env_folder(self) -> None:
        if not self._build_result:
            return
        folder = Path(self._build_result.env_dir).resolve()
        if not folder.exists():
            themed_showerror(self, "Error", f"Folder not found:\n{folder}")
            return
        if not folder.is_dir():
            folder = folder.parent
        try:
            # Explicit Explorer launch avoids "default open action" surprises.
            explorer = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "explorer.exe"
            explorer_exe = str(explorer) if explorer.exists() else "explorer.exe"
            subprocess.Popen([explorer_exe, str(folder)])
        except Exception:
            themed_showerror(self, "Error", "Could not open the folder.")

    def open_audit_log(self) -> None:
        log_path = logs_dir() / "security_audit.log"
        if not log_path.exists():
            themed_showwarning(self, "Audit Log", "No security audit log found yet.")
            return
        try:
            os.startfile(str(log_path))  # type: ignore[attr-defined]
        except Exception:
            themed_showerror(self, "Error", "Could not open security_audit.log.")

    def export_env_zip(self) -> None:
        if not self._build_result:
            return
        env_dir = self._build_result.env_dir
        suggested = f"{env_dir.name}.zip"
        out = filedialog.asksaveasfilename(
            title="Export Portable ZIP",
            defaultextension=".zip",
            initialdir=str(env_dir.parent),
            initialfile=suggested,
            filetypes=[("ZIP archive", "*.zip")],
        )
        if not out:
            return
        try:
            zip_path = export_portable_zip(env_dir, Path(out))
        except Exception as exc:
            themed_showerror(self, "Export Failed", str(exc))
            return
        themed_showinfo(self, "Export Complete", f"Portable ZIP created:\n{zip_path}")

    def _reset_for_new_build(self) -> None:
        self._target_custom = False
        self.var_project_mode.set("create")
        self._set_env_name_auto(self._default_env_name())
        self._last_git_auto_env_name = ""
        self.var_target_dir.set(str(Path(self.var_env_name.get())))
        self.var_source_dir.set("")
        self.var_source_url.set("")
        self.var_source_ref.set("")
        self.var_entry_point.set("")
        self.var_auto_analyze_source.set(True)
        self.var_source_analysis_status.set("Source analysis not run yet.")
        self.var_window_only.set(False)
        self.var_create_desktop_shortcut.set(False)
        self.var_create_start_menu_shortcut.set(False)
        self.var_install_ffmpeg.set(False)
        self.var_ffmpeg_arch.set("auto")
        self.var_python_mode.set("recommended")
        self.var_python_version.set(str(DEFAULT_VERSION))
        self.var_use_requirements.set(False)
        self.var_requirements_path.set("")
        self.var_manual_packages.set("")
        self.var_dependency_no_deps.set(False)
        self.var_auto_install_project.set(True)
        self.var_use_pymanager_components.set(True)
        self.var_clear_cache.set(True)
        self._build_result = None
        self._build_state = "idle"
        self._current_plan = None
        self._source_analysis = None
        self._source_analysis_key = ""
        self.step_bar.reset()
        self.pg_setup.sync_shortcut_ui()
        self.pg_setup.sync_tooling_ui()
        self._show_page(0)


# ══════════════════════════════════════════════════════════════════════════
#  Page 1: Setup
# ══════════════════════════════════════════════════════════════════════════

class _SetupPage(ttk.Frame):
    def __init__(self, parent: tk.Widget, *, app: PyEmbedBuilderApp) -> None:
        super().__init__(parent)
        self.app = app
        self._build_ui()

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        self._scroll = ScrollableFrame(self)
        self._scroll.pack(fill="both", expand=True)
        body = self._scroll.inner
        body.columnconfigure(0, weight=1)

        ttk.Label(
            body,
            text="Configure your embedded Python environment",
            style="Heading.TLabel",
        ).pack(anchor="w", padx=4, pady=(0, 14))

        # -- Source card ----------------------------------------------------
        src = ttk.LabelFrame(body, text="  Project Source  ", padding=(16, 12))
        src.pack(fill="x", pady=(0, 10))
        src.columnconfigure(1, weight=1)

        rb_create = ttk.Radiobutton(
            src,
            text="Create an empty project (main.py default)",
            value="create",
            variable=self.app.var_project_mode,
            command=self._sync_project_mode,
            style="Card.TRadiobutton",
            takefocus=False,
        )
        rb_create.grid(row=0, column=0, columnspan=3, sticky="w")
        Tooltip(
            rb_create,
            "Start from a blank template. A default main.py is created if missing.",
        )

        rb_import = ttk.Radiobutton(
            src,
            text="Import an existing local project folder",
            value="import",
            variable=self.app.var_project_mode,
            command=self._sync_project_mode,
            style="Card.TRadiobutton",
            takefocus=False,
        )
        rb_import.grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 0))
        Tooltip(
            rb_import,
            "Copy a project from your local disk into the portable environment.",
        )

        rb_git = ttk.Radiobutton(
            src,
            text="Clone from Git repository URL",
            value="git",
            variable=self.app.var_project_mode,
            command=self._sync_project_mode,
            style="Card.TRadiobutton",
            takefocus=False,
        )
        rb_git.grid(row=2, column=0, columnspan=3, sticky="w", pady=(4, 0))
        Tooltip(
            rb_git,
            "Clone a repository before build. Supports HTTPS, SSH, and owner/repo shorthand.",
        )

        rb_zip = ttk.Radiobutton(
            src,
            text="Download from project ZIP URL",
            value="zip",
            variable=self.app.var_project_mode,
            command=self._sync_project_mode,
            style="Card.TRadiobutton",
            takefocus=False,
        )
        rb_zip.grid(row=3, column=0, columnspan=3, sticky="w", pady=(4, 8))
        Tooltip(
            rb_zip,
            "Download a source archive (.zip) and use it as the project input.",
        )

        ttk.Label(
            src,
            text="Create mode default: main.py is used as startup and auto-created if missing.",
            style="CardMuted.TLabel",
        ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(0, 8))

        self._source_dir_label = ttk.Label(src, text="Source folder:", style="Card.TLabel")
        self._source_dir_label.grid(row=5, column=0, sticky="w")
        self._source_dir_entry = ttk.Entry(src, textvariable=self.app.var_source_dir)
        self._source_dir_entry.grid(row=5, column=1, sticky="ew", padx=(12, 8))
        self._source_dir_btn = ttk.Button(src, text="Browse...", command=self.app.browse_source_dir)
        self._source_dir_btn.grid(row=5, column=2, sticky="e")
        Tooltip(
            self._source_dir_entry,
            "Folder to copy when 'Import local project' mode is selected.",
        )
        Tooltip(
            self._source_dir_btn,
            "Pick the local project folder to package.",
        )

        self._source_url_label = ttk.Label(src, text="Source URL:", style="Card.TLabel")
        self._source_url_label.grid(row=6, column=0, sticky="w", pady=(8, 0))
        self._source_url_entry = ttk.Entry(src, textvariable=self.app.var_source_url)
        self._source_url_entry.grid(row=6, column=1, columnspan=2, sticky="ew", padx=(12, 0), pady=(8, 0))
        Tooltip(
            self._source_url_entry,
            "Git supports HTTPS, SSH, owner/repo, or 'gh repo clone owner/repo'.\n"
            "Use Git ref field for branch/tag/commit (or paste 'URL @ ref' to auto-split).\n"
            "ZIP supports HTTPS archive links from github.com/codeload/github/gitlab/bitbucket hosts.",
        )

        self._source_ref_label = ttk.Label(src, text="Git ref (optional):", style="Card.TLabel")
        self._source_ref_label.grid(row=7, column=0, sticky="w", pady=(8, 0))
        self._source_ref_entry = ttk.Entry(src, textvariable=self.app.var_source_ref)
        self._source_ref_entry.grid(row=7, column=1, columnspan=2, sticky="ew", padx=(12, 0), pady=(8, 0))
        Tooltip(
            self._source_ref_entry,
            "Optional branch/tag/commit to checkout after clone, for example:\n"
            "main, feature/my-branch, v1.2.0, or a commit hash.",
        )

        self._auto_analyze_chk = ttk.Checkbutton(
            src,
            text="Analyze source metadata before build (detect requirements/pyproject, infer Python)",
            variable=self.app.var_auto_analyze_source,
            command=self._on_toggle_auto_analysis,
            style="Card.TCheckbutton",
            takefocus=False,
        )
        self._auto_analyze_chk.grid(row=8, column=0, columnspan=3, sticky="w", pady=(8, 0))
        Tooltip(
            self._auto_analyze_chk,
            "When enabled, PyEmbedBuilder inspects source metadata before build and can\n"
            "auto-adjust Python version/dependency defaults for convenience.",
        )

        self._analysis_status = ttk.Label(
            src,
            textvariable=self.app.var_source_analysis_status,
            style="CardMuted.TLabel",
            wraplength=CONTENT_WRAP_WIDTH,
            justify="left",
        )
        self._analysis_status.grid(row=9, column=0, columnspan=3, sticky="w", pady=(6, 0))
        Tooltip(
            self._analysis_status,
            "Shows detected metadata such as requirements, pyproject, and suggested Python version.",
        )

        # -- Portable project card -----------------------------------------
        env = ttk.LabelFrame(body, text="  Portable Project  ", padding=(16, 12))
        env.pack(fill="x", pady=(0, 10))
        env.columnconfigure(1, weight=1)

        ttk.Label(env, text="Name:", style="Card.TLabel").grid(row=0, column=0, sticky="w")
        self._name_entry = ttk.Entry(env, textvariable=self.app.var_env_name)
        self._name_entry.grid(row=0, column=1, sticky="ew", padx=(12, 0))
        Tooltip(
            self._name_entry,
            "Environment folder name (letters, digits, -, _, .). Used under output location.",
        )

        self._loc_label = ttk.Label(env, text="Output location:", style="Card.TLabel")
        self._loc_label.grid(row=1, column=0, sticky="w", pady=(8, 0))
        self._loc_entry = ttk.Entry(env, textvariable=self.app.var_target_dir)
        self._loc_entry.grid(row=1, column=1, sticky="ew", padx=(12, 8), pady=(8, 0))
        self._loc_btn = ttk.Button(env, text="Browse...", command=self.app.browse_target_dir)
        self._loc_btn.grid(row=1, column=2, sticky="e", pady=(8, 0))
        Tooltip(
            self._loc_entry,
            "Destination folder for the portable project output.",
        )
        Tooltip(
            self._loc_btn,
            "Choose the folder where the portable project directory will be created.",
        )

        ttk.Label(env, text="Entry point (.py, optional):", style="Card.TLabel").grid(
            row=2, column=0, sticky="w", pady=(8, 0),
        )
        self._entry_entry = ttk.Entry(env, textvariable=self.app.var_entry_point)
        self._entry_entry.grid(row=2, column=1, sticky="ew", padx=(12, 8), pady=(8, 0))
        self._entry_btn = ttk.Button(env, text="Browse...", command=self.app.browse_entry_point)
        self._entry_btn.grid(row=2, column=2, sticky="e", pady=(8, 0))
        Tooltip(
            self._entry_entry,
            "Startup script relative to project root (leave blank to auto-use main.py).",
        )
        Tooltip(
            self._entry_btn,
            "Select your startup Python file. Leave blank to fall back to main.py.",
        )

        self._window_chk = ttk.Checkbutton(
            env,
            text="Window-only launch mode (pythonw.exe, no console window)",
            variable=self.app.var_window_only,
            command=self._sync_shortcut_ui,
            style="Card.TCheckbutton",
            takefocus=False,
        )
        self._window_chk.grid(row=3, column=0, columnspan=3, sticky="w", pady=(8, 0))
        Tooltip(
            self._window_chk,
            "Enabled: launch.bat uses pythonw.exe and exits immediately.\n"
            "Disabled: launch.bat uses python.exe for console debugging output.",
        )

        self._desktop_shortcut_chk = ttk.Checkbutton(
            env,
            text="Create Desktop launch shortcut (Windows 10/11)",
            variable=self.app.var_create_desktop_shortcut,
            style="Card.TCheckbutton",
            takefocus=False,
        )
        self._desktop_shortcut_chk.grid(
            row=4, column=0, columnspan=3, sticky="w", pady=(6, 0),
        )
        Tooltip(
            self._desktop_shortcut_chk,
            "Creates a current-user Desktop .lnk using the selected launch mode.",
        )

        self._start_menu_shortcut_chk = ttk.Checkbutton(
            env,
            text="Create Start Menu launch shortcut (Windows 10/11)",
            variable=self.app.var_create_start_menu_shortcut,
            style="Card.TCheckbutton",
            takefocus=False,
        )
        self._start_menu_shortcut_chk.grid(
            row=5, column=0, columnspan=3, sticky="w", pady=(6, 0),
        )
        Tooltip(
            self._start_menu_shortcut_chk,
            "Creates a current-user Start Menu shortcut in Programs.",
        )

        # -- Python version card ------------------------------------------
        py = ttk.LabelFrame(body, text="  Python Version  ", padding=(16, 12))
        py.pack(fill="x", pady=(0, 10))
        py.columnconfigure(1, weight=1)

        rb_recommended = ttk.Radiobutton(
            py,
            text=f"Default version: Python {DEFAULT_VERSION} (amd64)",
            value="recommended",
            variable=self.app.var_python_mode,
            command=self._sync_mode,
            style="Card.TRadiobutton",
            takefocus=False,
        )
        rb_recommended.grid(row=0, column=0, columnspan=3, sticky="w")
        Tooltip(
            rb_recommended,
            "Use the default stable version known to work well for most projects.",
        )

        rb_custom = ttk.Radiobutton(
            py,
            text="Choose a different version:",
            value="custom",
            variable=self.app.var_python_mode,
            command=self._sync_mode,
            style="Card.TRadiobutton",
            takefocus=False,
        )
        rb_custom.grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))
        Tooltip(
            rb_custom,
            "Manually pick another embeddable Python version from the available list.",
        )

        ttk.Label(py, text="Version:", style="Card.TLabel").grid(row=2, column=0, sticky="w", pady=(12, 0))
        self._ver_entry = ttk.Entry(py, textvariable=self.app.var_python_version, state="readonly")
        self._ver_entry.grid(row=2, column=1, sticky="ew", padx=(12, 8), pady=(12, 0))
        self._pick_btn = ttk.Button(py, text="Pick...", command=self.app.pick_python_version)
        self._pick_btn.grid(row=2, column=2, sticky="e", pady=(12, 0))
        Tooltip(
            self._ver_entry,
            "Currently selected Python version for the embedded runtime.",
        )
        Tooltip(
            self._pick_btn,
            "Open the embeddable version picker (cached list loads first, then refreshes).",
        )

        ttk.Label(py, text="Architecture:", style="Card.TLabel").grid(row=3, column=0, sticky="w", pady=(8, 0))
        arch_cb = ttk.Combobox(
            py,
            textvariable=self.app.var_arch,
            values=("amd64", "win32"),
            state="readonly",
            width=12,
        )
        arch_cb.grid(row=3, column=1, sticky="w", padx=(12, 0), pady=(8, 0))
        Tooltip(
            arch_cb,
            "Target CPU architecture. Choose amd64 for most Windows 10/11 machines.",
        )
        ttk.Label(py, text="(amd64 for most modern systems)", style="CardMuted.TLabel").grid(
            row=3, column=2, sticky="w", padx=(8, 0), pady=(8, 0)
        )

        # -- Dependencies card -------------------------------------------
        deps = ttk.LabelFrame(body, text="  Dependencies  ", padding=(16, 12))
        deps.pack(fill="x", pady=(0, 10))
        deps.columnconfigure(1, weight=1)

        chk_requirements = ttk.Checkbutton(
            deps,
            text="Install packages from requirements.txt",
            variable=self.app.var_use_requirements,
            command=self._toggle_deps,
            style="Card.TCheckbutton",
            takefocus=False,
        )
        chk_requirements.grid(row=0, column=0, columnspan=3, sticky="w")
        Tooltip(
            chk_requirements,
            "Enable installation from a requirements.txt file during build.",
        )

        ttk.Label(deps, text="File:", style="Card.TLabel").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self._req_entry = ttk.Entry(deps, textvariable=self.app.var_requirements_path)
        self._req_entry.grid(row=1, column=1, sticky="ew", padx=(12, 8), pady=(8, 0))
        self._req_btn = ttk.Button(deps, text="Browse...", command=self.app.browse_requirements)
        self._req_btn.grid(row=1, column=2, sticky="e", pady=(8, 0))
        Tooltip(
            self._req_entry,
            "Path to requirements.txt (can be local file or one detected from source metadata).",
        )
        Tooltip(
            self._req_btn,
            "Browse for a requirements.txt file.",
        )

        ttk.Label(deps, text="Manual packages:", style="Card.TLabel").grid(
            row=2, column=0, sticky="w", pady=(8, 0),
        )
        pkg_e = ttk.Entry(deps, textvariable=self.app.var_manual_packages)
        pkg_e.grid(row=2, column=1, columnspan=2, sticky="ew", padx=(12, 0), pady=(8, 0))
        Tooltip(
            pkg_e,
            "Optional package list (space or comma separated), e.g.:\n"
            "requests numpy pandas\n"
            "or: requests, numpy, pandas\n"
            "If requirements.txt is selected, these are installed after it.",
        )

        chk_no_deps = ttk.Checkbutton(
            deps,
            text="Direct packages only (--no-deps) [advanced]",
            variable=self.app.var_dependency_no_deps,
            style="Card.TCheckbutton",
            takefocus=False,
        )
        chk_no_deps.grid(row=3, column=0, columnspan=3, sticky="w", pady=(8, 0))
        Tooltip(
            chk_no_deps,
            "If enabled, pip installs requested packages only and skips\n"
            "dependency resolution. Use this when you explicitly manage\n"
            "all transitive dependencies yourself.",
        )

        chk_auto_project = ttk.Checkbutton(
            deps,
            text="Auto-install project package (pip install .) when packaging metadata exists",
            variable=self.app.var_auto_install_project,
            style="Card.TCheckbutton",
            takefocus=False,
        )
        chk_auto_project.grid(row=4, column=0, columnspan=3, sticky="w", pady=(8, 0))
        Tooltip(
            chk_auto_project,
            "If enabled, PyEmbedBuilder tries pip install . first in auto-detect mode,\n"
            "then applies requirements/pyproject dependency auto-install as needed.\n"
            "Works when pyproject.toml/setup.py/setup.cfg packaging metadata is present.",
        )

        # -- Optional components card -----------------------------------
        extras = ttk.LabelFrame(body, text="  Optional Components  ", padding=(16, 12))
        extras.pack(fill="x", pady=(0, 10))
        extras.columnconfigure(0, weight=1)

        chk_pm = ttk.Checkbutton(
            extras,
            text="Add full stdlib + tools from python.org MSI packages",
            variable=self.app.var_use_pymanager_components,
            style="Card.TCheckbutton",
            takefocus=False,
        )
        chk_pm.grid(row=0, column=0, sticky="w")
        Tooltip(
            chk_pm,
            "Downloads the official per-version MSI packages (core, lib, tcltk,\n"
            "dev, tools) from python.org/ftp/python/{version}/{arch}/ and extracts\n"
            "Scripts, DLLs, tcl, Lib, libs, include into your environment.",
        )

        ttk.Label(
            extras,
            text="Extracts: Scripts, DLLs, tcl, Lib, libs, include via python.org MSI",
            style="CardMuted.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(4, 0))

        chk_cache = ttk.Checkbutton(
            extras,
            text="Clear download cache after build (keep audit log)",
            variable=self.app.var_clear_cache,
            style="Card.TCheckbutton",
            takefocus=False,
        )
        chk_cache.grid(row=2, column=0, sticky="w", pady=(8, 0))
        Tooltip(
            chk_cache,
            "Deletes downloaded archives and MSI packages after a successful build. "
            "Security audit logs are kept.",
        )

        # -- Optional tooling card --------------------------------------
        tooling = ttk.LabelFrame(body, text="  Optional Tooling  ", padding=(16, 12))
        tooling.pack(fill="x", pady=(0, 10))
        tooling.columnconfigure(1, weight=1)

        chk_ffmpeg = ttk.Checkbutton(
            tooling,
            text="Install FFmpeg release essentials from gyan.dev",
            variable=self.app.var_install_ffmpeg,
            command=self._sync_tooling_ui,
            style="Card.TCheckbutton",
            takefocus=False,
        )
        chk_ffmpeg.grid(row=0, column=0, columnspan=3, sticky="w")
        Tooltip(
            chk_ffmpeg,
            "Downloads ffmpeg-release-essentials.zip from gyan.dev, fetches "
            "its .sha256 file, verifies the SHA256 hash, and installs it under tools\\ffmpeg.",
        )

        ttk.Label(tooling, text="FFmpeg arch:", style="Card.TLabel").grid(
            row=1, column=0, sticky="w", pady=(8, 0),
        )
        self._ffmpeg_arch_cb = ttk.Combobox(
            tooling,
            textvariable=self.app.var_ffmpeg_arch,
            values=("auto", "x64", "x86"),
            state="readonly",
            width=12,
        )
        self._ffmpeg_arch_cb.grid(row=1, column=1, sticky="w", padx=(12, 0), pady=(8, 0))
        Tooltip(
            self._ffmpeg_arch_cb,
            "auto and x64 use the current gyan.dev Windows build. x86 is shown "
            "for configuration clarity, but gyan.dev currently publishes 64-bit builds only.",
        )

        # -- Setup configurations card ------------------------------------
        cfg = ttk.LabelFrame(body, text="  Setup Configurations  ", padding=(16, 12))
        cfg.pack(fill="x", pady=(0, 10))
        cfg.columnconfigure(0, weight=1)

        ttk.Label(
            cfg,
            text="Save and load complete setup configurations. Configurations are "
            "also auto-saved after each successful build.",
            style="CardMuted.TLabel",
            wraplength=CONTENT_WRAP_WIDTH,
        ).grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 8))

        self._cfg_combo = ttk.Combobox(cfg, state="readonly", width=36)
        self._cfg_combo.grid(row=1, column=0, sticky="ew", padx=(0, 8))
        Tooltip(
            self._cfg_combo,
            "Select a saved configuration to load or delete.",
        )

        load_btn = ttk.Button(
            cfg, text="Load", command=self._on_load_setup,
        )
        load_btn.grid(row=1, column=1, padx=(0, 4))
        Tooltip(load_btn, "Load the selected configuration into the setup form.")

        save_btn = ttk.Button(
            cfg, text="Save As...", command=self.app.save_setup_as,
        )
        save_btn.grid(row=1, column=2, padx=(0, 4))
        Tooltip(save_btn, "Save the current setup form as a named configuration.")

        del_btn = ttk.Button(
            cfg, text="Delete", command=self._on_delete_setup,
        )
        del_btn.grid(row=1, column=3)
        Tooltip(del_btn, "Delete the selected saved configuration.")

        self._refresh_setup_list()

        self._toggle_deps()
        self._sync_mode()
        self._sync_shortcut_ui()
        self._sync_tooling_ui()
        self._sync_project_mode()

    def _toggle_deps(self) -> None:
        on = self.app.var_use_requirements.get()
        st = "normal" if on else "disabled"
        self._req_entry.configure(state=st)
        self._req_btn.configure(state=st)

    def _sync_mode(self) -> None:
        if self.app.var_python_mode.get() == "recommended":
            self.app.var_python_version.set(str(DEFAULT_VERSION))
            self._ver_entry.configure(state="readonly")
        else:
            self._ver_entry.configure(state="normal")

    def _sync_shortcut_ui(self) -> None:
        self._desktop_shortcut_chk.configure(state="normal")
        self._start_menu_shortcut_chk.configure(state="normal")

    def _sync_tooling_ui(self) -> None:
        ffmpeg_state = "readonly" if self.app.var_install_ffmpeg.get() else "disabled"
        self._ffmpeg_arch_cb.configure(state=ffmpeg_state)

    def _on_toggle_auto_analysis(self) -> None:
        if not self.app.var_auto_analyze_source.get():
            self.app.var_source_analysis_status.set("Source analysis disabled.")
            return
        if self.app.var_project_mode.get() == "create":
            self.app.var_source_analysis_status.set("Create mode has no source metadata to analyze.")
        else:
            self.app.var_source_analysis_status.set("Source analysis will run before review/build.")

    def sync_python_mode(self) -> None:
        """Public: synchronize Python version UI with current mode."""
        self._sync_mode()

    def sync_dependency_ui(self) -> None:
        """Public: synchronize dependency file UI with current checkbox state."""
        self._toggle_deps()

    def sync_shortcut_ui(self) -> None:
        """Public: synchronize shortcut checkboxes with launch mode."""
        self._sync_shortcut_ui()

    def sync_tooling_ui(self) -> None:
        """Public: synchronize optional tooling controls."""
        self._sync_tooling_ui()

    def _refresh_setup_list(self) -> None:
        """Reload the setup configuration combobox from disk."""
        try:
            names = list_setup_configs()
            visible = [n for n in names if not n.startswith("_")]
        except Exception:
            visible = []
        self._cfg_combo["values"] = tuple(visible)
        if visible:
            self._cfg_combo.set(visible[0])
        else:
            self._cfg_combo.set("")

    def _on_load_setup(self) -> None:
        """Load the selected setup configuration into the form."""
        name = self._cfg_combo.get().strip()
        if not name:
            themed_showinfo(self.app, "Load Setup", "No configuration selected.")
            return
        self.app.load_setup_from(name)
        self._refresh_setup_list()

    def _on_delete_setup(self) -> None:
        """Delete the selected setup configuration."""
        name = self._cfg_combo.get().strip()
        if not name:
            themed_showinfo(self.app, "Delete Setup", "No configuration selected.")
            return
        self.app.delete_setup(name)

    def _sync_project_mode(self) -> None:
        mode = self.app.var_project_mode.get()
        self.app._source_analysis = None
        self.app._source_analysis_key = ""
        create_mode = mode == "create"
        import_mode = mode == "import"
        git_mode = mode == "git"
        zip_mode = mode == "zip"

        if create_mode:
            self.app._target_custom = False
            self.app._suppress_target_trace = True
            try:
                name = self.app.var_env_name.get().strip() or self.app._default_env_name()
                self.app.var_target_dir.set(str(Path(name)))
            finally:
                self.app._suppress_target_trace = False

        src_state = "normal" if import_mode else "disabled"
        for w in (self._source_dir_entry, self._source_dir_btn):
            w.configure(state=src_state)

        url_state = "normal" if (git_mode or zip_mode) else "disabled"
        self._source_url_entry.configure(state=url_state)

        ref_state = "normal" if git_mode else "disabled"
        self._source_ref_entry.configure(state=ref_state)
        if not git_mode:
            self.app.var_source_ref.set("")

        analyze_state = "disabled" if create_mode else "normal"
        self._auto_analyze_chk.configure(state=analyze_state)
        if create_mode:
            self.app.var_source_analysis_status.set("Create mode has no source metadata to analyze.")
        elif not self.app.var_auto_analyze_source.get():
            self.app.var_source_analysis_status.set("Source analysis disabled.")
        else:
            self.app.var_source_analysis_status.set("Source analysis will run before review/build.")

        entry_btn_state = "disabled" if (git_mode or zip_mode) else "normal"
        self._entry_btn.configure(state=entry_btn_state)
        if git_mode:
            self.app._on_source_url_change()

    def refresh_theme(self, c: dict[str, str]) -> None:
        self._scroll.set_bg(c["bg"])

    def scroll_to_top(self) -> None:
        self._scroll.scroll_to_top()

# ══════════════════════════════════════════════════════════════════════════
#  Page 2: Review
# ══════════════════════════════════════════════════════════════════════════

class _ReviewPage(ttk.Frame):
    def __init__(self, parent: tk.Widget, *, app: PyEmbedBuilderApp) -> None:
        super().__init__(parent)
        self.app = app
        self._build_ui()

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        self._scroll = ScrollableFrame(self)
        self._scroll.pack(fill="both", expand=True)
        body = self._scroll.inner
        body.columnconfigure(0, weight=1)

        ttk.Label(
            body,
            text="Review your build plan before proceeding",
            style="Heading.TLabel",
        ).pack(anchor="w", padx=4, pady=(0, 14))

        # ── Build Plan ────────────────────────────────────────────────
        plan_card = ttk.LabelFrame(body, text="  Build Plan  ", padding=(16, 12))
        plan_card.pack(fill="x", pady=(0, 10))
        self._lbl_plan = ttk.Label(
            plan_card, text="", justify="left", style="Card.TLabel", wraplength=CONTENT_WRAP_WIDTH,
        )
        self._lbl_plan.pack(anchor="w")

        # ── Security ──────────────────────────────────────────────────
        sec_card = ttk.LabelFrame(body, text="  Security Verification  ", padding=(16, 12))
        sec_card.pack(fill="x", pady=(0, 10))
        self._lbl_sec = ttk.Label(
            sec_card, text="", justify="left", style="Card.TLabel", wraplength=CONTENT_WRAP_WIDTH,
        )
        self._lbl_sec.pack(anchor="w")

        # ── Steps ─────────────────────────────────────────────────────
        steps_card = ttk.LabelFrame(body, text="  Steps to Execute  ", padding=(16, 12))
        steps_card.pack(fill="x")
        self._lbl_steps = ttk.Label(
            steps_card, text="", justify="left", style="Card.TLabel", wraplength=CONTENT_WRAP_WIDTH,
        )
        self._lbl_steps.pack(anchor="w")


    def refresh(self, plan: BuildPlan) -> None:
        req_name = plan.requirements_txt.name if plan.requirements_txt else "None"
        manual_packages = ", ".join(plan.manual_packages) if plan.manual_packages else "None"
        mode_map = {
            "create": "Create scaffold in output folder (default startup: main.py)",
            "import": "Import existing local folder",
            "git": "Clone from Git URL",
            "zip": "Download from ZIP URL",
        }
        mode_label = mode_map.get(plan.project_mode, plan.project_mode)
        if plan.project_mode == "import" and plan.source_path:
            source_label = _rel_path(plan.source_path)
        elif plan.project_mode in {"git", "zip"}:
            source_label = plan.source_url
        else:
            source_label = "(none)"
        git_ref_line = ""
        if plan.project_mode == "git":
            git_ref_line = (
                f"Git ref:                {plan.source_ref}\n"
                if plan.source_ref
                else "Git ref:                (default branch)\n"
            )
        launch_mode = (
            "Window-only (pythonw.exe)"
            if plan.window_only
            else "Console debug (python.exe)"
        )
        shortcuts_label = "None"
        shortcuts: list[str] = []
        if plan.create_desktop_shortcut:
            shortcuts.append("Desktop")
        if plan.create_start_menu_shortcut:
            shortcuts.append("Start Menu")
        if shortcuts:
            shortcuts_label = ", ".join(shortcuts)
        ffmpeg_label = (
            f"Yes ({'x64' if plan.ffmpeg_arch == 'auto' else plan.ffmpeg_arch})"
            if plan.install_ffmpeg
            else "No"
        )
        dep_mode = (
            "Direct only (--no-deps)"
            if plan.dependency_no_deps
            else "Resolved (default pip behavior)"
        )
        entry_label = (
            plan.entry_point_rel
            if plan.entry_point_rel
            else "(auto: main.py)"
        )
        self._lbl_plan.configure(text=(
            f"Environment:         {plan.env_name}\n"
            f"Source mode:        {mode_label}\n"
            f"Source:                {source_label}\n"
            f"{git_ref_line}"
            f"Output:                {_display_output_path(plan.target_dir)}\n"
            f"Entry point:         {entry_label}\n"
            f"Launch mode:       {launch_mode}\n"
            f"Shortcuts:          {shortcuts_label}\n"
            f"FFmpeg:             {ffmpeg_label}\n"
            f"Dependency mode: {dep_mode}\n"
            f"Python:                  {plan.version} ({plan.arch})\n"
            f"Requirements:       {req_name}\n"
            f"Manual packages:   {manual_packages}\n"
            f"Auto-install .:       {'Yes' if plan.auto_install_project else 'No'}\n"
            f"MSI components:   {'Yes' if plan.use_pymanager_components else 'No'}\n"
            f"Clear cache:          {'Yes' if plan.clear_cache_on_success else 'No'}"
        ))

        self._lbl_sec.configure(text=(
            "\u2713  HTTPS enforced on all downloads (TLS 1.2+)\n"
            "\u2713  Trusted source policies enforced for python.org, get-pip, and project hosts\n"
            "\u2713  Optional per-version MSI extraction from python.org\n"
            "\u2713  Optional FFmpeg download verifies SHA256 before extraction\n"
            "\u2713  ZIP extraction validated against path traversal (zip-slip)\n"
            "\u2713  Download size limits enforced (500 MB max)\n"
            "\u2713  Subprocess execution sandboxed (no shell, timeout + cancel support)\n"
            "\u2713  All actions logged to security_audit.log"
        ))

        steps: list[str] = []
        if plan.project_mode == "create":
            steps.append("1.   Prepare project scaffold in output folder")
        elif plan.project_mode == "import":
            steps.append("1.   Copy local project files into output folder")
        elif plan.project_mode == "git":
            steps.append("1.   Clone Git repository and copy project files")
        else:
            steps.append("1.   Download project ZIP and copy project files")

        steps.extend([
            "2.   Resolve download URL from python.org",
            "3.   Download Python embeddable ZIP (strict source policy)",
            "4.   Extract archive (zip-slip protection)",
        ])
        if plan.use_pymanager_components:
            steps.append("5.   Extract MSI components (Scripts, DLLs, tcl, Lib, libs, include)")
        else:
            steps.append("5.   MSI components  (skipped)")
        steps.append("6.   Configure pythonXY._pth (enable site-packages)")
        steps.append("7.   Bootstrap pip (get-pip.py from bootstrap.pypa.io)")
        if plan.requirements_txt and plan.manual_packages:
            steps.append(
                f"8.   Install dependencies from {req_name} + manual package list"
            )
        elif plan.requirements_txt:
            steps.append(f"8.   Install dependencies from {req_name}")
        elif plan.manual_packages:
            steps.append("8.   Install dependencies from manual package list")
        elif plan.auto_install_project:
            steps.append("8.   Try pip install . first, then auto-detect requirements/pyproject dependencies if needed")
        else:
            steps.append("8.   Install dependencies  (skipped)")
        steps.append("9.   Verify environment (import site, pip)")
        steps.append("10. Smoke-test launch target")
        if plan.install_ffmpeg:
            steps.append("11. Install optional FFmpeg tooling with SHA256 verification")
        else:
            steps.append("11. Optional FFmpeg tooling  (skipped)")
        steps.append("12. Create launch.bat, dependency scripts, and entry-point updater")
        if shortcuts:
            steps.append("13. Create Windows 10/11 launch shortcuts")
        else:
            steps.append("13. Windows launch shortcuts  (skipped)")
        steps.append("14. Write README_portable.txt + EMBEDDED_PYTHON_CONFIGURATION.md")

        self._lbl_steps.configure(text="\n".join(steps))

    def refresh_theme(self, c: dict[str, str]) -> None:
        self._scroll.set_bg(c["bg"])


# ══════════════════════════════════════════════════════════════════════════
#  Page 3: Build
# ══════════════════════════════════════════════════════════════════════════

class _BuildPage(ttk.Frame):
    STEPS = [
        ("source",    "Prepare project source"),
        ("meta",      "Resolve download metadata"),
        ("download",  "Download Python embeddable ZIP"),
        ("extract",   "Extract and verify archive"),
        ("pymanager", "MSI components (stdlib + tools)"),
        ("pth",       "Configure pythonXY._pth"),
        ("pip",       "Bootstrap pip"),
        ("reqs",      "Install dependencies"),
        ("verify",    "Verify environment"),
        ("smoke",     "Smoke-test launch target"),
        ("tooling",   "Install optional tools"),
        ("launcher",  "Create launcher scripts"),
        ("shortcuts", "Create Windows shortcuts"),
        ("guide",     "Write portable guides"),
    ]

    def __init__(self, parent: tk.Widget, *, app: PyEmbedBuilderApp) -> None:
        super().__init__(parent)
        self.app = app
        self._step_rows: dict[str, StepRow] = {}
        self._step_state: dict[str, str] = {sid: "pending" for sid, _ in self.STEPS}
        self._active_step_id: str | None = None
        self._download_done = 0
        self._download_total: int | None = None
        self._max_overall_pct = 0
        self._build_ui()

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        ttk.Label(
            self, text="Build progress", style="Heading.TLabel",
        ).pack(anchor="w", padx=4, pady=(0, 10))

        # ── Main body (steps + log side-by-side) ─────────────────────
        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=2)
        body.columnconfigure(1, weight=5)
        body.rowconfigure(0, weight=1)

        # Steps panel
        steps_lf = ttk.LabelFrame(body, text="  Steps  ", padding=(12, 8))
        steps_lf.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        steps_lf.columnconfigure(0, weight=1)

        for i, (sid, title) in enumerate(self.STEPS):
            row = StepRow(steps_lf, title)
            row.grid(row=i, column=0, sticky="ew", pady=3)
            self._step_rows[sid] = row

        # Log panel
        log_lf = ttk.LabelFrame(body, text="  Log  ", padding=(8, 6))
        log_lf.grid(row=0, column=1, sticky="nsew")
        log_lf.columnconfigure(0, weight=1)
        log_lf.rowconfigure(0, weight=1)

        self._log = tk.Text(
            log_lf, wrap="word", height=16, state="disabled",
            font=(MONO_FONT, BUILD_LOG_MONO_SIZE), borderwidth=0, padx=6, pady=4,
        )
        self._log.grid(row=0, column=0, sticky="nsew")

        log_sb = ttk.Scrollbar(log_lf, orient="vertical", command=self._log.yview)
        log_sb.grid(row=0, column=1, sticky="ns")
        self._log.configure(yscrollcommand=log_sb.set)

        action_row = ttk.Frame(log_lf)
        action_row.grid(row=1, column=0, columnspan=2, sticky="e", pady=(4, 0))
        self._open_audit_btn = ttk.Button(
            action_row,
            text="Open Audit Log",
            command=self.app.open_audit_log,
            state="disabled",
        )
        self._open_audit_btn.pack(side="right")
        Tooltip(
            self._open_audit_btn,
            "Open the security audit log file in your default text editor.",
        )
        self._copy_log_btn = ttk.Button(
            action_row,
            text="Copy Log",
            command=self._copy_log,
            state="disabled",
        )
        self._copy_log_btn.pack(side="right", padx=(0, 6))
        Tooltip(
            self._copy_log_btn,
            "Copy the full build log text to clipboard for sharing or troubleshooting.",
        )

        # Progress bar
        prog_frame = ttk.Frame(self)
        prog_frame.pack(fill="x", pady=(8, 0))
        prog_frame.columnconfigure(0, weight=1)

        self._progress = ttk.Progressbar(prog_frame, mode="determinate", maximum=100)
        self._progress.grid(row=0, column=0, sticky="ew")
        self._prog_label = ttk.Label(prog_frame, text="", style="Muted.TLabel")
        self._prog_label.grid(row=0, column=1, sticky="e", padx=(10, 0))


    def refresh_theme(self, c: dict[str, str]) -> None:
        self._log.configure(
            bg=c["log_bg"],
            fg=c["log_fg"],
            insertbackground=c["log_fg"],
            font=(MONO_FONT, max(9, int(9 * self.app._theme.scale))),
        )

    def reset_ui(self) -> None:
        for row in self._step_rows.values():
            row.set_status("pending")
        self._step_state = {sid: "pending" for sid, _ in self.STEPS}
        self._active_step_id = None
        self._download_done = 0
        self._download_total = None
        self._max_overall_pct = 0
        self._set_log("")
        self._progress.stop()
        self._progress.configure(mode="determinate")
        self._progress["value"] = 0
        self._prog_label.configure(text=f"0%  (0/{len(self.STEPS)} steps)")
        self._set_log_actions_enabled(False)

    def on_step_update(self, upd: StepUpdate) -> None:
        row = self._step_rows.get(upd.step_id)
        if row:
            row.set_status(upd.status, upd.detail)
        if upd.step_id in self._step_state:
            self._step_state[upd.step_id] = upd.status
        if upd.status == "running":
            self._active_step_id = upd.step_id
            if upd.step_id != "download":
                self._download_done = 0
                self._download_total = None
        elif upd.status in {"ok", "error"} and self._active_step_id == upd.step_id:
            self._active_step_id = None
        self._refresh_overall_progress()

    def on_log(self, line: str) -> None:
        self._append_log(line + "\n")

    def on_progress(self, done: int, total: int | None) -> None:
        self._download_done = max(0, int(done))
        self._download_total = int(total) if total is not None and total > 0 else None
        if self._step_state.get("download") == "running" or self._active_step_id == "download":
            self._refresh_overall_progress()

    def on_done(self, result: BuildResult) -> None:
        self._progress.stop()
        self._progress.configure(mode="determinate")
        self._progress["value"] = 100
        self._prog_label.configure(text="Complete")
        self._append_log(f"\n\u2713 Build completed: {_display_output_path(result.env_dir)}\n")
        self._set_log_actions_enabled(True)

    def on_cancelled(self, msg: str) -> None:
        self._progress.stop()
        self._append_log(f"\n\u2717 Cancelled: {msg}\n")
        self._append_log("Review the log, then use 'Retry Build' or 'Review Plan'.\n")
        pct = int(float(self._progress["value"]))
        self._prog_label.configure(text=f"{pct}%  (Cancelled)")
        self._set_log_actions_enabled(True)
        themed_showwarning(self.app, "Build Cancelled", msg)

    def on_error(self, msg: str, tb: str = "") -> None:
        self._progress.stop()
        pct = int(float(self._progress["value"]))
        self._prog_label.configure(text=f"{pct}%  (Failed)")
        self._append_log(f"\n\u2717 ERROR:\n{msg}\n")
        if tb:
            self._append_log(f"\nTraceback:\n{tb}\n")
        self._append_log("Review the log, then use 'Retry Build' or 'Review Plan'.\n")
        self._set_log_actions_enabled(True)
        themed_showerror(self.app, "Build Failed", msg[:2000])

    def _refresh_overall_progress(self) -> None:
        total_steps = len(self.STEPS)
        if total_steps <= 0:
            self._progress.configure(mode="determinate")
            self._progress["value"] = 0
            self._prog_label.configure(text="")
            return

        completed = sum(
            1 for sid, _ in self.STEPS
            if self._step_state.get(sid) == "ok"
        )
        running_sid = next(
            (sid for sid, _ in self.STEPS if self._step_state.get(sid) == "running"),
            None,
        )

        partial = 0.0
        suffix = ""
        if running_sid is not None:
            if running_sid == "download":
                if self._download_total and self._download_total > 0:
                    partial = min(0.98, self._download_done / self._download_total)
                    suffix = f", download {self._download_done:,}/{self._download_total:,} bytes"
                elif self._download_done > 0:
                    partial = 0.35
                    suffix = f", download {self._download_done:,} bytes"
                else:
                    partial = 0.15
            else:
                partial = 0.35

        units = completed + partial
        pct = int(min(100, max(0, (units / total_steps) * 100)))
        if pct < self._max_overall_pct:
            pct = self._max_overall_pct
        else:
            self._max_overall_pct = pct

        self._progress.configure(mode="determinate")
        self._progress["value"] = pct
        self._prog_label.configure(text=f"{pct}%  ({completed}/{total_steps} steps{suffix})")

    def _set_log(self, s: str) -> None:
        self._log.configure(state="normal")
        self._log.delete("1.0", "end")
        if s:
            self._log.insert("end", s)
        self._log.configure(state="disabled")
        self._log.see("end")

    _LOG_MAX_LINES = 10_000

    def _append_log(self, s: str) -> None:
        self._log.configure(state="normal")
        self._log.insert("end", s)
        total = int(self._log.index("end-1c").split(".")[0])
        if total > self._LOG_MAX_LINES:
            trim = total - self._LOG_MAX_LINES
            self._log.delete("1.0", f"{trim}.0")
        self._log.configure(state="disabled")
        self._log.see("end")

    def _set_log_actions_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        self._open_audit_btn.configure(state=state)
        self._copy_log_btn.configure(state=state)

    def _copy_log(self) -> None:
        content = self._log.get("1.0", "end").strip()
        if content:
            self.app.clipboard_clear()
            self.app.clipboard_append(content)


# ══════════════════════════════════════════════════════════════════════════
#  Page 4: Complete
# ══════════════════════════════════════════════════════════════════════════

class _CompletePage(ttk.Frame):
    def __init__(self, parent: tk.Widget, *, app: PyEmbedBuilderApp) -> None:
        super().__init__(parent)
        self.app = app
        self._build_ui()

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        self._scroll = ScrollableFrame(self)
        self._scroll.pack(fill="both", expand=True)
        body = self._scroll.inner
        body.columnconfigure(0, weight=1)

        # ── Success banner ────────────────────────────────────────────
        banner = ttk.Frame(body)
        banner.pack(fill="x", pady=(0, 14))
        ttk.Label(
            banner,
            text="\u2713  Environment Created Successfully",
            style="Success.TLabel",
            font=(UI_FONT, SUCCESS_BANNER_SIZE, "bold"),
        ).pack(anchor="w")

        # ── Details card ──────────────────────────────────────────────
        details = ttk.LabelFrame(body, text="  Details  ", padding=(16, 12))
        details.pack(fill="x", pady=(0, 10))
        self._lbl_details = ttk.Label(
            details, text="", justify="left", style="Card.TLabel", wraplength=CONTENT_WRAP_WIDTH,
        )
        self._lbl_details.pack(anchor="w")

        # ── Security audit card ───────────────────────────────────────
        audit_card = ttk.LabelFrame(body, text="  Security Audit  ", padding=(16, 12))
        audit_card.pack(fill="x", pady=(0, 10))
        self._lbl_audit = ttk.Label(
            audit_card, text="", justify="left", style="Card.TLabel", wraplength=CONTENT_WRAP_WIDTH,
        )
        self._lbl_audit.pack(anchor="w")

        # ── Actions ───────────────────────────────────────────────────
        actions = ttk.Frame(body)
        actions.pack(fill="x", pady=(4, 0))

        open_env_btn = ttk.Button(
            actions, text="Open Environment Folder",
            command=self.app.open_env_folder,
        )
        open_env_btn.pack(side="left", padx=(0, 8))
        Tooltip(
            open_env_btn,
            "Open the generated portable project directory.",
        )

        copy_path_btn = ttk.Button(
            actions, text="Copy Path to Clipboard",
            command=self._copy_path,
        )
        copy_path_btn.pack(side="left", padx=(0, 8))
        Tooltip(
            copy_path_btn,
            "Copy the absolute environment path to clipboard.",
        )

        open_audit_btn = ttk.Button(
            actions, text="Open Security Audit Log",
            command=self.app.open_audit_log,
        )
        open_audit_btn.pack(side="left", padx=(0, 8))
        Tooltip(
            open_audit_btn,
            "Open the build security audit log for verification details.",
        )

        export_zip_btn = ttk.Button(
            actions, text="Export Portable ZIP",
            command=self.app.export_env_zip,
        )
        export_zip_btn.pack(side="left", padx=(0, 8))
        Tooltip(
            export_zip_btn,
            "Create a ZIP archive of the built environment for easier sharing.",
        )


    def set_result(self, result: BuildResult) -> None:
        m = result.manifest or {}
        artifacts = m.get("artifacts", {})
        pz = artifacts.get("python_zip", {})
        manifest_file = m.get("manifest_file", "build_manifest.json")
        config_file = artifacts.get("configuration_markdown", "EMBEDDED_PYTHON_CONFIGURATION.md")
        entry_point = m.get("launch_target") or m.get("entry_point_rel") or "(auto)"
        launch_mode = m.get("launch_mode", "window_only")
        launch_mode_label = (
            "pythonw.exe (window-only)"
            if launch_mode == "window_only"
            else "python.exe (console debug)"
        )
        dep_mode = m.get("dependency_mode", "resolved")
        dep_mode_label = (
            "direct only (--no-deps)"
            if dep_mode == "no_deps"
            else "resolved (default pip behavior)"
        )
        src = m.get("source", {})
        source_mode = m.get("project_mode", "create")
        source_detail = src.get("detail") or src.get("path") or src.get("url") or "(none)"
        tooling_info = artifacts.get("tooling", {})
        tooling_label = _tooling_summary(tooling_info)

        self._lbl_details.configure(text=(
            f"Environment:    {m.get('env_name', '?')}\n"
            f"Source mode:      {source_mode}\n"
            f"Source:              {source_detail}\n"
            f"Location:          {_display_output_path(result.env_dir)}\n"
            f"Python root:     {_rel_to_base(result.py_root, result.env_dir)}\n"
            f"Python version: {m.get('python_version', '?')} ({m.get('arch', '?')})\n"
            f"Launch mode:     {launch_mode_label}\n"
            f"Dependencies:   {dep_mode_label}\n"
            f"Tooling:            {tooling_label}\n"
            f"Created at:       {m.get('created_at', '?')}\n\n"
            f"Manifest:         {manifest_file}\n"
            f"Agent config:    {config_file}\n\n"
            f"Quick start:\n"
            f"  \u2022  Run  launch.bat  to start your app ({entry_point})\n"
            f"  \u2022  Run  install_dependencies.bat  to install additional packages\n"
            f"  \u2022  Run  list_dependencies.bat  to view installed packages\n"
            f"  \u2022  Run  uninstall_dependencies.bat  to remove packages\n"
            f"  \u2022  Run  update_entry_point.bat  to change startup file without rebuilding\n"
            f"  \u2022  Open {config_file} for an agent-friendly environment breakdown\n"
            f"  \u2022  Open README_portable.txt for beginner-friendly usage notes\n"
            f"  \u2022  Rebuild only when project source or Python version changes"
        ))

        # ── Build audit summary ───────────────────────────────────────
        source = pz.get("source", "N/A")
        size_val = pz.get("size_bytes")
        if isinstance(size_val, int):
            size_text = f"{size_val:,} bytes"
        else:
            size_text = "N/A"

        # MSI components
        pm = artifacts.get("pymanager", {})
        pm_status = pm.get("status", "skipped")
        pm_reason = pm.get("reason", "")
        pm_dirs = ", ".join(pm.get("extracted_dirs") or [])
        cache = m.get("cache", {})
        shortcut_info = artifacts.get("shortcuts", {})
        tooling_label = _tooling_summary(tooling_info)

        # MSI components line
        if pm_status == "ok":
            pm_line = f"MSI components:          \u2713 {pm_dirs or 'components added'}"
        elif pm_status == "skipped" and pm_reason:
            pm_line = f"MSI components:          skipped ({pm_reason})"
        else:
            pm_line = "MSI components:          skipped"

        if cache.get("cleared"):
            cache_line = "Cache:                      \u2713 cleared"
        elif cache.get("reason") == "disabled":
            cache_line = "Cache:                      kept"
        elif cache.get("error"):
            cache_line = f"Cache:                      clear failed ({cache.get('error')})"
        else:
            cache_line = "Cache:                      kept"

        shortcut_labels: list[str] = []
        if shortcut_info.get("desktop"):
            shortcut_labels.append("Desktop")
        if shortcut_info.get("start_menu"):
            shortcut_labels.append("Start Menu")
        if shortcut_labels:
            shortcuts_line = f"Shortcuts:                 ✓ {', '.join(shortcut_labels)}"
        else:
            reason = shortcut_info.get("skipped_reason")
            shortcuts_line = (
                f"Shortcuts:                 skipped ({reason})"
                if reason and reason != "not requested"
                else "Shortcuts:                 skipped"
            )
        tooling_line = (
            f"Tooling:                    \u2713 {tooling_label} (SHA256 verified)"
            if tooling_label != "None"
            else "Tooling:                    skipped"
        )

        self._lbl_audit.configure(text=(
            f"Python ZIP source:      {source}\n"
            f"Python ZIP size:         {size_text}\n"
            f"{pm_line}\n"
            f"{shortcuts_line}\n"
            f"{tooling_line}\n"
            f"{cache_line}\n"
            f"Download:                   \u2713 HTTPS + trusted-source policy checks\n"
            f"Extraction:                   \u2713 Zip-slip validated\n\n"
            f"Full audit log:  .\\{APP_DATA_DIRNAME}\\logs\\security_audit.log"
        ))
        self._result = result

    def refresh_theme(self, c: dict[str, str]) -> None:
        self._scroll.set_bg(c["bg"])

    def _copy_path(self) -> None:
        if hasattr(self, "_result") and self._result:
            self.app.clipboard_clear()
            self.app.clipboard_append(str(self._result.env_dir))


# ══════════════════════════════════════════════════════════════════════════
#  Version Picker Dialog
# ══════════════════════════════════════════════════════════════════════════

class _VersionPickerDialog(ThemedDialog):
    """Modal dialog to pick an embeddable Python version from python.org."""

    def __init__(self, parent: tk.Tk, *, arch: str) -> None:
        super().__init__(
            parent,
            title="Choose Python Version",
            min_width=VERSION_PICKER_MIN_SIZE[0],
            min_height=VERSION_PICKER_MIN_SIZE[1],
        )
        self.selected_version: Version | None = None
        self._arch = arch
        self._versions: list[Version] = []
        self._q: queue.Queue[object] = queue.Queue()

        self._build_picker_ui()
        _disable_button_focus_recursive(self)
        self._load_async()

    def _build_picker_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        self._lbl_title = ttk.Label(
            self, text="All stable embeddable Python versions",
            style="Heading.TLabel",
        )
        self._lbl_title.grid(row=0, column=0, sticky="w", padx=16, pady=(16, 8))

        frm = ttk.Frame(self)
        frm.grid(row=1, column=0, sticky="nsew", padx=16, pady=(0, 8))
        frm.columnconfigure(0, weight=1)
        frm.rowconfigure(0, weight=1)

        self._listbox = tk.Listbox(frm, activestyle="dotbox")
        self._listbox.grid(row=0, column=0, sticky="nsew")
        self.theme_listbox(self._listbox)
        Tooltip(
            self._listbox,
            "Double-click or select a version, then press Select.",
        )

        sb = ttk.Scrollbar(frm, orient="vertical", command=self._listbox.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self._listbox.configure(yscrollcommand=sb.set)

        btns = ttk.Frame(self)
        btns.grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 16))
        btns.columnconfigure(0, weight=1)

        self._status = ttk.Label(btns, text="Loading\u2026", style="Muted.TLabel")
        self._status.grid(row=0, column=0, sticky="w")
        Tooltip(
            self._status,
            "Shows whether versions came from cache or from a fresh online refresh.",
        )

        cancel_btn = ttk.Button(btns, text="Cancel", command=self.destroy)
        cancel_btn.grid(
            row=0, column=1, sticky="e", padx=(0, 8),
        )
        Tooltip(cancel_btn, "Close picker without changing the selected version.")
        select_btn = ttk.Button(
            btns, text="Select", style="Accent.TButton", command=self._select,
        )
        select_btn.grid(row=0, column=2, sticky="e")
        Tooltip(
            select_btn,
            "Use the highlighted version as your Python runtime.",
        )

        self._listbox.bind("<Double-Button-1>", lambda _: self._select())
        self.center_over_parent()

    def _load_async(self) -> None:
        cached = load_cached_embeddable_versions(self._arch)
        if cached:
            self._set_versions(cached, preserve_selection=False)
            self._status.configure(
                text=f"{len(self._versions)} local embeddable versions loaded. Refreshing..."
            )
        else:
            self._status.configure(text="Loading embeddable versions...")

        def worker():
            try:
                versions = refresh_embeddable_versions_cache(self._arch)
                self._q.put(versions)
            except Exception as e:
                self._q.put(e)

        threading.Thread(target=worker, daemon=True).start()
        self.after(60, self._poll)

    def _poll(self) -> None:
        try:
            item = self._q.get_nowait()
        except queue.Empty:
            self.after(120, self._poll)
            return

        if isinstance(item, Exception):
            if self._versions:
                self._status.configure(text="Using local versions (refresh failed).")
            else:
                self._status.configure(text="Failed to load versions.")
                themed_showerror(self, "Load Failed", str(item))
            return

        self._set_versions(list(item), preserve_selection=True)
        if self._versions:
            self._status.configure(
                text=f"{len(self._versions)} embeddable versions available (updated)"
            )
        else:
            self._status.configure(text="No embeddable versions found for this architecture")

    def _set_versions(
        self,
        versions: list[Version],
        *,
        preserve_selection: bool,
    ) -> None:
        current: Version | None = None
        if preserve_selection:
            sel = self._listbox.curselection()
            if sel and 0 <= int(sel[0]) < len(self._versions):
                current = self._versions[int(sel[0])]

        uniq = sorted(set(versions), reverse=True)
        self._versions = list(uniq)
        self._listbox.delete(0, "end")
        for v in self._versions:
            self._listbox.insert("end", f"  Python {v}   (embed-{self._arch})")

        if self._versions:
            idx = 0
            if current is not None and current in self._versions:
                idx = self._versions.index(current)
            self._listbox.selection_set(idx)
            self._listbox.activate(idx)

    def _select(self) -> None:
        sel = self._listbox.curselection()
        if not sel:
            return
        idx = int(sel[0])
        if 0 <= idx < len(self._versions):
            self.selected_version = self._versions[idx]
            self.destroy()
