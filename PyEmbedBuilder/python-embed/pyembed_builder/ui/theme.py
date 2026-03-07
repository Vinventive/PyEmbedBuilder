"""
Comprehensive theme manager and reusable UI widgets for PyEmbedBuilder.

Supports Light, Dark, and High-Contrast modes with full widget coverage.
"""
from __future__ import annotations

import sys
import tkinter as tk
from tkinter import font as tkfont, ttk


# -- Font families ----------------------------------------------------------

if sys.platform == "win32":
    UI_FONT_FAMILY = "Segoe UI"
    MONO_FONT_FAMILY = "Consolas"
elif sys.platform == "darwin":
    UI_FONT_FAMILY = "Helvetica Neue"
    MONO_FONT_FAMILY = "Menlo"
else:
    UI_FONT_FAMILY = "Sans"
    MONO_FONT_FAMILY = "Monospace"


# -- Window/layout ----------------------------------------------------------

WINDOW_MIN_SIZE = (1000, 720)
WINDOW_DEFAULT_SIZE = (1060, 750)
VERSION_PICKER_MIN_SIZE = (480, 440)

CONTENT_WRAP_WIDTH = 820
TOOLTIP_WRAP_WIDTH = 360


# -- Widget sizing ----------------------------------------------------------

STEP_BAR_RADIUS = 13
STEP_BAR_HEIGHT = 64
BUILD_LOG_MONO_SIZE = 9
SUCCESS_BANNER_SIZE = 16


# -- Public font aliases ----------------------------------------------------

UI_FONT = UI_FONT_FAMILY
MONO_FONT = MONO_FONT_FAMILY


# -- Color palettes ---------------------------------------------------------

COLORS: dict[str, dict[str, str]] = {
    "light": {
        # Tailwind-inspired modern light theme
        "bg":              "#f8fafc",      # Slate 50
        "card_bg":         "#ffffff",
        "card_border":     "#e2e8f0",      # Slate 200
        "fg":              "#0f172a",      # Slate 900
        "fg_secondary":    "#475569",      # Slate 600
        "fg_muted":        "#94a3b8",      # Slate 400
        "accent":          "#2563eb",      # Blue 600
        "accent_hover":    "#1d4ed8",      # Blue 700
        "accent_fg":       "#ffffff",
        "success":         "#16a34a",      # Green 600
        "success_bg":      "#f0fdf4",
        "success_fg":      "#ffffff",      # Tick mark color
        "error":           "#dc2626",      # Red 600
        "error_hover":     "#b91c1c",      # Red 700 (Darker red for clear white text hover)
        "error_bg":        "#fef2f2",
        "warning":         "#d97706",      # Amber 600
        "warning_bg":      "#fffbeb",
        "entry_bg":        "#f1f5f9",      # Slate 100
        "entry_border":    "#cbd5e1",      # Slate 300
        "step_line":       "#e2e8f0",
        "step_pending":    "#cbd5e1",
        "log_bg":          "#0f172a",
        "log_fg":          "#f8fafc",
        "separator":       "#e2e8f0",
        "treeview_stripe": "#f1f5f9",
        "tooltip_bg":      "#1e293b",      # Slate 800 (Dark tooltip for modern feel)
        "tooltip_fg":      "#f8fafc",
        "tooltip_border":  "#1e293b",
    },
    "dark": {
        # VS Code / Sublime-inspired sleek dark theme
        "bg":              "#1e1e1e",      # Deep sleek background
        "card_bg":         "#252526",      # Slightly elevated card
        "card_border":     "#3e3e42",      # Subtle borders
        "fg":              "#cccccc",      # Softened white for eye comfort
        "fg_secondary":    "#999999",
        "fg_muted":        "#7a7a7a",
        "accent":          "#007acc",      # VS Code Blue
        "accent_hover":    "#0062a3",
        "accent_fg":       "#ffffff",
        "success":         "#89d185",      # Soft pastel green
        "success_bg":      "#122217",
        "success_fg":      "#ffffff",      # White tick for step indicators in dark theme
        "error":           "#f48771",      # Soft pastel red
        "error_hover":     "#d86a56",      # Slightly darker pastel red for hover
        "error_bg":        "#331616",
        "warning":         "#cca700",
        "warning_bg":      "#332a00",
        "entry_bg":        "#3c3c3c",      # Distinct, flat entry background
        "entry_border":    "#3c3c3c",      # Border matches background to flatten 3D effect
        "step_line":       "#3e3e42",
        "step_pending":    "#555555",
        "log_bg":          "#111111",
        "log_fg":          "#d4d4d4",
        "separator":       "#3e3e42",
        "treeview_stripe": "#2a2a2b",
        "tooltip_bg":      "#2d2d30",
        "tooltip_fg":      "#cccccc",
        "tooltip_border":  "#454545",
    },
    "high_contrast": {
        "bg":              "#000000",
        "card_bg":         "#0a0a0a",
        "card_border":     "#ffffff",
        "fg":              "#ffffff",
        "fg_secondary":    "#ffffff",
        "fg_muted":        "#cccccc",
        "accent":          "#ffff00",
        "accent_hover":    "#00ff00",      # Contrast green hover for primary action buttons
        "accent_fg":       "#000000",
        "success":         "#00ff00",
        "success_bg":      "#002200",
        "success_fg":      "#000000",      # Black tick mark for readability on HC green
        "error":           "#ff4444",
        "error_hover":     "#ffff00",      # Yellow for HC hover
        "error_bg":        "#330000",
        "warning":         "#ffcc00",
        "warning_bg":      "#332200",
        "entry_bg":        "#111111",
        "entry_border":    "#ffffff",
        "step_line":       "#ffffff",
        "step_pending":    "#666666",
        "log_bg":          "#000000",
        "log_fg":          "#00ff00",
        "separator":       "#ffffff",
        "treeview_stripe":  "#111111",
        "tooltip_bg":       "#000000",
        "tooltip_fg":       "#ffffff",
        "tooltip_border":   "#ffffff",
    },
}


class ThemeManager:
    """Manages Light / Dark / High-Contrast themes with font scaling."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.style = ttk.Style(root)
        self._mode = "light"
        self._scale = 1.0

        # Capture base font sizes at startup
        self._base_fonts: dict[str, int] = {}
        for name in ("TkDefaultFont", "TkTextFont", "TkFixedFont", "TkHeadingFont"):
            try:
                size = tkfont.nametofont(name).cget("size")
                self._base_fonts[name] = size if size else 9
            except Exception:
                self._base_fonts[name] = 9

    @property
    def colors(self) -> dict[str, str]:
        return COLORS.get(self._mode, COLORS["light"])

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def scale(self) -> float:
        return self._scale

    def apply(self, mode: str = "light", scale: float = 1.0) -> None:
        """Apply the given theme mode and font scale."""
        self._mode = mode
        self._scale = max(0.8, min(2.0, scale))
        c = self.colors

        # -- Fonts ---------------------------------------------------
        for name, base in self._base_fonts.items():
            try:
                f = tkfont.nametofont(name)
                f.configure(size=max(8, int(round(abs(base) * self._scale))))
            except Exception:
                pass

        # -- Use 'clam' as base (most customizable) ------------------
        try:
            self.style.theme_use("clam")
        except tk.TclError:
            pass

        self.root.configure(bg=c["bg"])

        # -- Base ----------------------------------------------------
        self.style.configure(
            ".",
            background=c["bg"],
            foreground=c["fg"],
            borderwidth=0,
            focusthickness=1,
            focuscolor=c["accent"],
        )

        # -- Frame ---------------------------------------------------
        self.style.configure("TFrame", background=c["bg"])
        self.style.configure("Card.TFrame", background=c["card_bg"])

        # -- Label ---------------------------------------------------
        self.style.configure("TLabel", background=c["bg"], foreground=c["fg"])
        self.style.configure("Card.TLabel", background=c["card_bg"], foreground=c["fg"])
        self.style.configure("Secondary.TLabel", background=c["bg"], foreground=c["fg_secondary"])
        self.style.configure("Muted.TLabel", background=c["bg"], foreground=c["fg_muted"])
        self.style.configure("CardMuted.TLabel", background=c["card_bg"], foreground=c["fg_muted"])

        sz = self._scale
        self.style.configure(
            "Title.TLabel", background=c["bg"], foreground=c["fg"],
            font=(UI_FONT, max(14, int(16 * sz)), "bold"),
        )
        self.style.configure(
            "Heading.TLabel", background=c["bg"], foreground=c["fg"],
            font=(UI_FONT, max(10, int(12 * sz)), "bold"),
        )
        self.style.configure(
            "Success.TLabel", background=c["bg"], foreground=c["success"],
        )
        self.style.configure(
            "Error.TLabel", background=c["bg"], foreground=c["error"],
        )
        self.style.configure(
            "CardSuccess.TLabel", background=c["card_bg"], foreground=c["success"],
        )
        self.style.configure(
            "CardError.TLabel", background=c["card_bg"], foreground=c["error"],
        )
        self.style.configure(
            "CardHeading.TLabel", background=c["card_bg"], foreground=c["fg"],
            font=(UI_FONT, max(10, int(11 * sz)), "bold"),
        )

        # -- LabelFrame ----------------------------------------------
        self.style.configure(
            "TLabelframe", background=c["card_bg"],
            borderwidth=0, relief="flat",
        )
        self.style.configure(
            "TLabelframe.Label", background=c["card_bg"],
            foreground=c["accent"],
            font=(UI_FONT, max(10, int(11 * sz)), "bold"),
            padding=(0, 8),
        )

        # -- Button --------------------------------------------------
        self.style.configure(
            "TButton", padding=(14, 8),
            background=c["card_bg"], foreground=c["fg"],
            borderwidth=1, relief="solid",
            bordercolor=c["card_border"],
            lightcolor=c["card_border"],
            darkcolor=c["card_border"]
        )

        btn_active_fg = c["bg"] if self._mode == "high_contrast" else c["fg"]

        self.style.map(
            "TButton",
            background=[("active", c["card_border"]), ("pressed", c["bg"])],
            foreground=[("active", btn_active_fg), ("pressed", c["fg"]), ("disabled", c["fg_muted"])],
            bordercolor=[("active", c["card_border"]), ("pressed", c["card_border"])],
        )

        self.style.configure(
            "Accent.TButton", padding=(16, 8),
            background=c["accent"], foreground=c["accent_fg"],
            borderwidth=1,
            bordercolor=c["accent"],
            lightcolor=c["accent"],
            darkcolor=c["accent"],
            font=(UI_FONT, max(9, int(10 * sz)), "bold"),
        )
        self.style.map(
            "Accent.TButton",
            background=[("active", c["accent_hover"]), ("pressed", c["accent_hover"])],
            foreground=[("active", c["accent_fg"]), ("pressed", c["accent_fg"]), ("disabled", c["fg_muted"])],
            bordercolor=[("active", c["accent_hover"]), ("pressed", c["accent_hover"])],
            lightcolor=[("active", c["accent_hover"]), ("pressed", c["accent_hover"])],
            darkcolor=[("active", c["accent_hover"]), ("pressed", c["accent_hover"])],
        )

        # Apply specific hover styles for the Cancel (Danger) button
        danger_active_bg = c.get("error_hover", c["error"])
        danger_active_fg = "#000000" if self._mode == "high_contrast" else "#ffffff"

        self.style.configure(
            "Danger.TButton", padding=(14, 8),
            background=c["error"], foreground="#ffffff",
            borderwidth=0,
        )
        self.style.map(
            "Danger.TButton",
            background=[("active", danger_active_bg)],
            foreground=[("active", danger_active_fg)],
        )

        # -- Entry ---------------------------------------------------
        pad_y = max(6, int(round(6 * sz)))
        entry_font = (UI_FONT, max(9, int(round(10 * sz))))
        self.style.configure(
            "TEntry", padding=(8, pad_y),
            fieldbackground=c["entry_bg"], foreground=c["fg"],
            insertcolor=c["fg"], borderwidth=1,
            bordercolor=c["entry_border"],
            lightcolor=c["entry_border"],
            darkcolor=c["entry_border"],
            font=entry_font,
        )

        # -- Combobox ------------------------------------------------
        self.style.configure(
            "TCombobox", padding=(8, pad_y),
            fieldbackground=c["entry_bg"], foreground=c["fg"],
            background=c["entry_bg"],
            borderwidth=1,
            bordercolor=c["entry_border"],
            lightcolor=c["entry_border"],
            darkcolor=c["entry_border"],
            arrowcolor=c["fg"],
            selectbackground=c["entry_bg"],
            selectforeground=c["fg"],
            font=entry_font,
        )
        self.style.map(
            "TCombobox",
            fieldbackground=[("readonly", c["entry_bg"]), ("disabled", c["card_border"])],
            foreground=[("readonly", c["fg"]), ("disabled", c["fg_muted"])],
            background=[("readonly", c["entry_bg"]), ("active", c["accent"])],
            arrowcolor=[("readonly", c["fg"]), ("active", c["accent_fg"])],
        )

        # Header comboboxes (theme/text-size) follow the same palette explicitly.
        self.style.configure(
            "Header.TCombobox", padding=(8, pad_y),
            fieldbackground=c["entry_bg"], foreground=c["fg"],
            background=c["entry_bg"],
            borderwidth=1,
            bordercolor=c["entry_border"],
            lightcolor=c["entry_border"],
            darkcolor=c["entry_border"],
            arrowcolor=c["fg"],
            selectbackground=c["entry_bg"],
            selectforeground=c["fg"],
            font=entry_font,
        )
        self.style.map(
            "Header.TCombobox",
            fieldbackground=[("readonly", c["entry_bg"]), ("disabled", c["card_border"])],
            foreground=[("readonly", c["fg"]), ("disabled", c["fg_muted"])],
            background=[("readonly", c["entry_bg"]), ("active", c["accent"])],
            arrowcolor=[("readonly", c["fg"]), ("active", c["accent_fg"])],
        )

        # -- Dropdown Popup Listbox Options --------------------------
        self.root.option_add("*TCombobox*Listbox.background", c["entry_bg"])
        self.root.option_add("*TCombobox*Listbox.foreground", c["fg"])
        self.root.option_add("*TCombobox*Listbox.selectBackground", c["accent"])
        self.root.option_add("*TCombobox*Listbox.selectForeground", c["accent_fg"])
        self.root.option_add("*TCombobox*Listbox.borderwidth", "0")
        self.root.option_add("*TCombobox*Listbox.relief", "flat")

        # -- Update all existing Entry/Combobox widget fonts ---------
        self._refresh_entry_fonts(entry_font)

        # -- Selection colors (Entry/Combobox) -----------------------
        sel_bg = c["accent"]
        sel_fg = c["accent_fg"]

        for opt in (
            "*Entry.selectBackground",
            "*TEntry.selectBackground",
            "*Text.selectBackground",
            "*Listbox.selectBackground",
        ):
            self.root.option_add(opt, sel_bg)
        for opt in (
            "*Entry.selectForeground",
            "*TEntry.selectForeground",
            "*Text.selectForeground",
            "*Listbox.selectForeground",
        ):
            self.root.option_add(opt, sel_fg)

        for opt in (
            "*Entry.font",
            "*Listbox.font",
            "*TCombobox*Listbox.font",
        ):
            self.root.option_add(opt, entry_font)

        # -- Checkbutton / Radiobutton -------------------------------
        for w in ("TCheckbutton", "TRadiobutton"):
            self.style.configure(w, background=c["bg"], foreground=c["fg"], padding=(6, 4))
            self.style.configure(f"Card.{w}", background=c["card_bg"], foreground=c["fg"], padding=(6, 4))
            
            # Hover colors match the theme's Accent button color for all styles
            self.style.map(
                w,
                background=[("active", c["accent"])],
                foreground=[("active", c["accent_fg"])]
            )
            self.style.map(
                f"Card.{w}",
                background=[("active", c["accent"])],
                foreground=[("active", c["accent_fg"])]
            )

            if self._mode == "high_contrast":
                self.style.configure(
                    w,
                    indicatorbackground=c["entry_bg"],
                    indicatorcolor=c["fg_muted"]
                )
                self.style.configure(
                    f"Card.{w}",
                    indicatorbackground=c["entry_bg"],
                    indicatorcolor=c["fg_muted"]
                )
                self.style.map(
                    w,
                    indicatorbackground=[("active", c["card_border"]), ("selected", c["accent"])],
                )
                self.style.map(
                    f"Card.{w}",
                    indicatorbackground=[("active", c["card_border"]), ("selected", c["accent"])],
                )
            else:
                self.style.configure(
                    w,
                    indicatorbackground="#ffffff",
                    indicatorcolor="#000000"
                )
                self.style.configure(
                    f"Card.{w}",
                    indicatorbackground="#ffffff",
                    indicatorcolor="#000000"
                )
                self.style.map(
                    w,
                    indicatorbackground=[("pressed", "#eeeeee"), ("selected", "#ffffff")],
                )
                self.style.map(
                    f"Card.{w}",
                    indicatorbackground=[("pressed", "#eeeeee"), ("selected", "#ffffff")],
                )

        # -- Progressbar ---------------------------------------------
        self.style.configure(
            "TProgressbar",
            troughcolor=c["card_border"],
            background=c["accent"],
            bordercolor=c["card_border"],
            lightcolor=c["accent"],
            darkcolor=c["accent"],
            borderwidth=0,
            thickness=10,
        )

        # -- Separator -----------------------------------------------
        self.style.configure("TSeparator", background=c["separator"])

        # -- Scale ---------------------------------------------------
        self.style.configure(
            "TScale", background=c["bg"], troughcolor=c["card_border"],
        )

        # -- Scrollbar -----------------------------------------------
        self.style.configure(
            "TScrollbar",
            background=c["card_border"],
            troughcolor=c["bg"],
            bordercolor=c["bg"],
            lightcolor=c["bg"],
            darkcolor=c["bg"],
            arrowcolor=c["fg_muted"],
            borderwidth=0,
        )
        self.style.map(
            "TScrollbar",
            background=[("active", c["fg_muted"])],
        )

    # -- helpers ------------------------------------------------------

    def _refresh_entry_fonts(self, font_spec: tuple) -> None:
        """Walk all widgets and update font on Entry/Combobox/Listbox."""
        self._walk_and_set_font(self.root, font_spec)

    def _walk_and_set_font(self, widget: tk.Widget, font_spec: tuple) -> None:
        for child in widget.winfo_children():
            cls = child.winfo_class()
            if cls in ("TEntry", "Entry", "TCombobox", "Listbox"):
                try:
                    child.configure(font=font_spec)
                except tk.TclError:
                    pass
            self._walk_and_set_font(child, font_spec)


# -- Wizard Step Indicator --------------------------------------------------

class WizardStepBar(tk.Canvas):
    """Horizontal step indicator: circles connected by lines, with labels."""

    RADIUS = STEP_BAR_RADIUS
    HEIGHT = STEP_BAR_HEIGHT

    def __init__(
        self,
        parent: tk.Widget,
        steps: list[str],
        theme_mgr: ThemeManager,
        **kw,
    ) -> None:
        kw.setdefault("height", self.HEIGHT)
        kw.setdefault("highlightthickness", 0)
        kw.setdefault("borderwidth", 0)
        super().__init__(parent, **kw)

        self._steps = steps
        self._theme = theme_mgr
        self._active = 0
        self._completed: set[int] = set()

        self.bind("<Configure>", self._redraw)

    def set_active(self, idx: int) -> None:
        for i in range(idx):
            self._completed.add(i)
        self._active = idx
        self._redraw()

    def mark_completed(self, idx: int) -> None:
        self._completed.add(idx)
        self._redraw()

    def reset(self) -> None:
        self._active = 0
        self._completed.clear()
        self._redraw()

    def _redraw(self, _event=None) -> None:
        self.delete("all")
        w = self.winfo_width()
        h = self.winfo_height()
        n = len(self._steps)
        if n == 0 or w < 120:
            return

        c = self._theme.colors
        bg = c["bg"]
        accent = c["accent"]
        accent_fg = c["accent_fg"]
        success = c["success"]
        success_fg = c["success_fg"]
        pending_c = c["step_pending"]
        line_c = c["step_line"]
        fg = c["fg"]
        muted = c["fg_muted"]

        self.configure(bg=bg)

        r = self.RADIUS
        margin = max(70, w // (n + 2))
        spacing = (w - 2 * margin) / max(1, n - 1)
        y_c = h // 2 - 6
        y_lbl = y_c + r + 15

        xs =[int(margin + i * spacing) for i in range(n)]

        # -- Connecting lines ----------------------------------------
        for i in range(n - 1):
            color = success if i < self._active else line_c
            self.create_line(
                xs[i] + r + 2, y_c, xs[i + 1] - r - 2, y_c,
                fill=color, width=2,
            )

        # -- Circles + labels ----------------------------------------
        for i, (x, label) in enumerate(zip(xs, self._steps)):
            if i in self._completed and i != self._active:
                # completed
                self.create_oval(x - r, y_c - r, x + r, y_c + r, fill=success, outline=success)
                self.create_text(
                    x,
                    y_c,
                    text="\u2713",
                    fill=success_fg,
                    font=(UI_FONT, max(10, int(11 * self._theme.scale)), "bold"),
                )
                self.create_text(
                    x,
                    y_lbl,
                    text=label,
                    fill=fg,
                    font=(UI_FONT, max(9, int(9 * self._theme.scale))),
                )
            elif i == self._active:
                # active
                self.create_oval(x - r, y_c - r, x + r, y_c + r, fill=accent, outline=accent)
                self.create_text(
                    x,
                    y_c,
                    text=str(i + 1),
                    fill=accent_fg,
                    font=(UI_FONT, max(9, int(10 * self._theme.scale)), "bold"),
                )
                self.create_text(
                    x,
                    y_lbl,
                    text=label,
                    fill=fg,
                    font=(UI_FONT, max(9, int(9 * self._theme.scale)), "bold"),
                )
            else:
                # pending
                self.create_oval(
                    x - r,
                    y_c - r,
                    x + r,
                    y_c + r,
                    fill=bg,
                    outline=pending_c,
                    width=2,
                )
                self.create_text(
                    x,
                    y_c,
                    text=str(i + 1),
                    fill=muted,
                    font=(UI_FONT, max(9, int(10 * self._theme.scale))),
                )
                self.create_text(
                    x,
                    y_lbl,
                    text=label,
                    fill=muted,
                    font=(UI_FONT, max(9, int(9 * self._theme.scale))),
                )


# -- Scrollable Frame -------------------------------------------------------

class ScrollableFrame(ttk.Frame):
    """A scrollable frame with vertical scrollbar (mouse-wheel aware)."""

    def __init__(self, parent: tk.Widget, **kw) -> None:
        super().__init__(parent, **kw)

        self._canvas = tk.Canvas(self, borderwidth=0, highlightthickness=0)
        self._vsb = ttk.Scrollbar(self, orient="vertical", command=self._canvas.yview)
        self.inner = ttk.Frame(self._canvas)

        self._win = self._canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self._canvas.configure(yscrollcommand=self._vsb.set)

        self._vsb.pack(side="right", fill="y")
        self._canvas.pack(side="left", fill="both", expand=True)

        self.inner.bind("<Configure>", self._on_inner)
        self._canvas.bind("<Configure>", self._on_canvas)
        self._canvas.bind("<Enter>", lambda _: self._bind_wheel())
        self._canvas.bind("<Leave>", lambda _: self._unbind_wheel())

    def set_bg(self, color: str) -> None:
        self._canvas.configure(bg=color)

    def scroll_to_top(self) -> None:
        self._canvas.yview_moveto(0)

    def _on_inner(self, _e=None) -> None:
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))

    def _on_canvas(self, e=None) -> None:
        self._canvas.itemconfig(self._win, width=e.width if e else self._canvas.winfo_width())

    def _bind_wheel(self) -> None:
        self._canvas.bind_all("<MouseWheel>", self._wheel)

    def _unbind_wheel(self) -> None:
        self._canvas.unbind_all("<MouseWheel>")

    def _wheel(self, e) -> None:
        self._canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")


# -- Build Step Row ---------------------------------------------------------

_STEP_ICONS = {
    "pending": "\u25cb",   # ○
    "running": "\u25cf",   # ●
    "ok":      "\u2713",   # ✓
    "error":   "\u2717",   # ✗
}


class StepRow(ttk.Frame):
    """A single build-step row with icon, title, and status text."""

    def __init__(self, parent: tk.Widget, title: str, **kw) -> None:
        super().__init__(parent, **kw)
        self.columnconfigure(1, weight=1)

        self._icon = ttk.Label(
            self, text=f"  {_STEP_ICONS['pending']}  ", width=4, style="CardMuted.TLabel",
        )
        self._icon.grid(row=0, column=0, sticky="w", padx=(0, 4))

        self._title = ttk.Label(self, text=title, style="Card.TLabel")
        self._title.grid(row=0, column=1, sticky="w")

        self._status = ttk.Label(self, text="Pending", style="CardMuted.TLabel", anchor="e")
        self._status.grid(row=0, column=2, sticky="e", padx=(12, 0))

    def set_status(self, status: str, detail: str = "") -> None:
        icon = _STEP_ICONS.get(status, "?")
        style_map = {
            "pending": "CardMuted.TLabel",
            "running": "Card.TLabel",
            "ok":      "CardSuccess.TLabel",
            "error":   "CardError.TLabel",
        }
        st = style_map.get(status, "Card.TLabel")

        self._icon.configure(text=f"  {icon}  ", style=st, foreground="")
        theme = getattr(self.winfo_toplevel(), "_theme", None)
        if isinstance(theme, ThemeManager) and theme.mode == "dark" and status == "ok":
            self._icon.configure(foreground=theme.colors.get("success_fg", "#ffffff"))
        self._status.configure(text=detail or status.capitalize(), style=st)

        size = tkfont.nametofont("TkDefaultFont").cget("size")
        if status == "running":
            self._title.configure(font=(UI_FONT, size, "bold"))
        else:
            self._title.configure(font=(UI_FONT, size))


# -- Tooltip ----------------------------------------------------------------

class Tooltip:
    """Lightweight tooltip for any tkinter widget."""

    def __init__(self, widget: tk.Widget, text: str, delay: int = 500) -> None:
        self._widget = widget
        self._text = text
        self._delay = delay
        self._tip: tk.Toplevel | None = None
        self._after_id: str | None = None

        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")

    def _schedule(self, _e=None) -> None:
        self._after_id = self._widget.after(self._delay, self._show)

    def _show(self) -> None:
        if self._tip:
            return
        x = self._widget.winfo_rootx() + 20
        y = self._widget.winfo_rooty() + self._widget.winfo_height() + 4

        tw = tk.Toplevel(self._widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        tw.attributes("-topmost", True)

        owner = self._widget.winfo_toplevel()
        theme = getattr(owner, "_theme", None)
        if not isinstance(theme, ThemeManager):
            parent_app = getattr(owner, "_app", None)
            theme = getattr(parent_app, "_theme", None)

        if isinstance(theme, ThemeManager):
            c = theme.colors
            scale = theme.scale
            bg = c.get("tooltip_bg", "#2d2d30")
            fg = c.get("tooltip_fg", "#cccccc")
            border = c.get("tooltip_border", "#454545")
            fsize = max(9, int(round(9 * scale)))
        else:
            bg, fg, border, fsize = "#2d2d30", "#cccccc", "#454545", 9

        lbl = tk.Label(
            tw,
            text=self._text,
            justify="left",
            background=bg,
            foreground=fg,
            highlightbackground=border,
            highlightcolor=border,
            highlightthickness=1,
            bd=0,
            font=(UI_FONT, fsize),
            padx=8,
            pady=4,
            wraplength=TOOLTIP_WRAP_WIDTH,
        )
        lbl.pack()
        self._tip = tw

    def _hide(self, _e=None) -> None:
        if self._after_id:
            self._widget.after_cancel(self._after_id)
            self._after_id = None
        if self._tip:
            self._tip.destroy()
            self._tip = None
