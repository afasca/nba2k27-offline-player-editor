"""Dark ttk theme and small composite widgets for the player editor."""
from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont, ttk

UI = "Microsoft YaHei UI"
DISPLAY = "Bahnschrift SemiBold"
P = {
    "bg": "#111418", "panel": "#181c22", "panel_hi": "#20252d", "hover": "#2a303a",
    "field": "#0c0f13", "border": "#2a303a", "border_hi": "#3b424e",
    "text": "#e8eaed", "muted": "#8b93a1", "faint": "#5d6573",
    "accent": "#f5e400", "accent_hi": "#fff45e", "accent_down": "#d8c900", "accent_text": "#111418",
    "danger": "#e5484d", "banner": "#a3121a", "success": "#3ccf7a", "warning": "#ffb224",
    "changed_bg": "#2b2410", "info": "#4c9aff",
}
FONT = (UI, 10)
FONT_SMALL = (UI, 9)
FONT_BOLD = (UI, 10, "bold")
FONT_TITLE = (UI, 15, "bold")
FONT_NAME = (DISPLAY, 20)
FONT_SECTION = (UI, 9, "bold")


def _flat(color):
    return {"background": color, "bordercolor": color, "lightcolor": color, "darkcolor": color}


def dark_title_bar(window: tk.Misc):
    """Dark Windows title bar to match the theme (Windows 10 20H1+ / 11)."""
    try:
        import ctypes
        window.update_idletasks()
        hwnd = ctypes.c_void_p(int(window.wm_frame(), 16))
        enabled = ctypes.c_int(1)
        for attribute in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE, and its pre-20H1 id
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(enabled),
                                                          ctypes.sizeof(enabled)) == 0:
                break
        r, g, b = (int(P["bg"][i:i + 2], 16) for i in (1, 3, 5))
        caption = ctypes.c_int(r | (g << 8) | (b << 16))  # DWMWA_CAPTION_COLOR, Windows 11 only
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(caption), ctypes.sizeof(caption))
    except Exception:
        pass


def apply_theme(root: tk.Tk) -> ttk.Style:
    style = ttk.Style(root)
    style.theme_use("clam")
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont", "TkCaptionFont", "TkTooltipFont"):
        try:
            tkfont.nametofont(name).configure(family=UI, size=10)
        except tk.TclError:
            pass
    root.configure(background=P["bg"])
    for pattern, value in (
        ("*TCombobox*Listbox.background", P["panel_hi"]), ("*TCombobox*Listbox.foreground", P["text"]),
        ("*TCombobox*Listbox.selectBackground", P["accent"]),
        ("*TCombobox*Listbox.selectForeground", P["accent_text"]),
        ("*TCombobox*Listbox.font", FONT), ("*TCombobox*Listbox.relief", "flat"),
        ("*Listbox.background", P["field"]), ("*Listbox.foreground", P["text"]),
        ("*Listbox.selectBackground", P["accent"]), ("*Listbox.selectForeground", P["accent_text"]),
        ("*Listbox.highlightThickness", 0), ("*Listbox.borderWidth", 0), ("*Listbox.font", FONT),
        ("*Toplevel.background", P["bg"]), ("*Canvas.background", P["bg"]),
    ):
        root.option_add(pattern, value)

    style.configure(".", background=P["bg"], foreground=P["text"], fieldbackground=P["field"],
                    bordercolor=P["border"], lightcolor=P["panel_hi"], darkcolor=P["panel"],
                    troughcolor=P["field"], selectbackground=P["accent"], selectforeground=P["accent_text"],
                    insertcolor=P["text"], focuscolor=P["bg"], font=FONT)
    style.map(".", foreground=[("disabled", P["faint"])])
    for name, color in (("TFrame", P["bg"]), ("Card.TFrame", P["panel"]), ("Bar.TFrame", P["panel"])):
        style.configure(name, background=color)
    labels = {
        "TLabel": {}, "Muted.TLabel": {"foreground": P["muted"]},
        "Small.TLabel": {"foreground": P["muted"], "font": FONT_SMALL},
        "Section.TLabel": {"foreground": P["muted"], "font": FONT_SECTION},
        "Title.TLabel": {"font": FONT_TITLE},
        "Card.TLabel": {"background": P["panel"]},
        "CardMuted.TLabel": {"background": P["panel"], "foreground": P["muted"]},
        "CardTitle.TLabel": {"background": P["panel"], "font": FONT_TITLE},
        "CardName.TLabel": {"background": P["panel"], "font": FONT_NAME},
        "CardSection.TLabel": {"background": P["panel"], "foreground": P["muted"], "font": FONT_SECTION},
        "Status.TLabel": {"background": P["panel"], "foreground": P["muted"]},
        "Online.TLabel": {"background": P["panel"], "foreground": P["success"], "font": FONT_BOLD},
        "Offline.TLabel": {"background": P["panel"], "foreground": P["danger"], "font": FONT_BOLD},
        "Busy.TLabel": {"background": P["panel"], "foreground": P["warning"], "font": FONT_BOLD},
        "Pending.TLabel": {"background": P["panel"], "foreground": P["warning"], "font": FONT_BOLD},
        "Changed.TLabel": {"foreground": P["warning"]},
    }
    for name, options in labels.items():
        style.configure(name, **options)

    style.configure("TButton", **_flat(P["panel_hi"]), foreground=P["text"], padding=(12, 6), relief="flat")
    style.map("TButton", background=[("disabled", P["panel"]), ("pressed", P["border_hi"]), ("active", P["hover"])],
              lightcolor=[("pressed", P["border_hi"]), ("active", P["hover"])],
              darkcolor=[("pressed", P["border_hi"]), ("active", P["hover"])],
              bordercolor=[("focus", P["border_hi"])])
    style.configure("Accent.TButton", **_flat(P["accent"]), foreground=P["accent_text"], font=FONT_BOLD,
                    padding=(16, 6))
    style.map("Accent.TButton",
              background=[("disabled", P["panel_hi"]), ("pressed", P["accent_down"]), ("active", P["accent_hi"])],
              lightcolor=[("disabled", P["panel_hi"]), ("pressed", P["accent_down"]), ("active", P["accent_hi"])],
              darkcolor=[("disabled", P["panel_hi"]), ("pressed", P["accent_down"]), ("active", P["accent_hi"])],
              bordercolor=[("disabled", P["panel_hi"]), ("pressed", P["accent_down"]), ("active", P["accent_hi"])],
              foreground=[("disabled", P["faint"])])
    style.configure("Bar.TButton", **_flat(P["panel_hi"]), foreground=P["text"], padding=(10, 5))
    style.map("Bar.TButton", background=[("disabled", P["panel"]), ("pressed", P["border_hi"]), ("active", P["hover"])],
              lightcolor=[("active", P["hover"])], darkcolor=[("active", P["hover"])])
    style.configure("Icon.TButton", **_flat(P["field"]), padding=(4, 2))
    style.map("Icon.TButton", background=[("active", P["hover"])], lightcolor=[("active", P["hover"])],
              darkcolor=[("active", P["hover"])], bordercolor=[("active", P["hover"])])

    entry = {"fieldbackground": P["field"], "foreground": P["text"], "bordercolor": P["border"],
             "lightcolor": P["field"], "darkcolor": P["field"], "insertcolor": P["text"], "padding": (6, 4)}
    style.configure("TEntry", **entry)
    style.map("TEntry", bordercolor=[("focus", P["accent"])], lightcolor=[("focus", P["accent"])],
              fieldbackground=[("disabled", P["panel"])], foreground=[("disabled", P["faint"])])
    style.configure("Bare.TEntry", **{**entry, "bordercolor": P["field"], "padding": (2, 4)})
    style.map("Bare.TEntry", bordercolor=[("focus", P["field"])], lightcolor=[("focus", P["field"])])
    changed = {"fieldbackground": P["changed_bg"], "foreground": P["warning"], "bordercolor": P["warning"],
               "lightcolor": P["changed_bg"], "darkcolor": P["changed_bg"]}
    style.configure("Changed.TEntry", **changed)
    style.map("Changed.TEntry", bordercolor=[("focus", P["accent"])], lightcolor=[("focus", P["accent"])])

    combo = {"fieldbackground": P["field"], "background": P["panel_hi"], "foreground": P["text"],
             "arrowcolor": P["muted"], "bordercolor": P["border"], "lightcolor": P["field"],
             "darkcolor": P["field"], "padding": (6, 3), "arrowsize": 13}
    combo_map = {"fieldbackground": [("readonly", P["field"]), ("disabled", P["panel"])],
                 "selectbackground": [("readonly", P["field"]), ("!focus", P["field"])],
                 "selectforeground": [("readonly", P["text"]), ("!focus", P["text"])],
                 "bordercolor": [("focus", P["accent"])], "arrowcolor": [("active", P["accent"])],
                 "background": [("active", P["hover"])]}
    style.configure("TCombobox", **combo)
    style.map("TCombobox", **combo_map)
    style.configure("Changed.TCombobox", **{**combo, **changed})
    style.map("Changed.TCombobox", **{**combo_map, "fieldbackground": [("readonly", P["changed_bg"])],
                                      "selectbackground": [("readonly", P["changed_bg"])],
                                      "selectforeground": [("readonly", P["warning"])]})

    style.configure("Treeview", background=P["field"], fieldbackground=P["field"], foreground=P["text"],
                    bordercolor=P["border"], lightcolor=P["field"], darkcolor=P["field"], rowheight=28)
    style.map("Treeview", background=[("selected", P["accent"])], foreground=[("selected", P["accent_text"])])
    style.configure("Treeview.Heading", **_flat(P["panel_hi"]), foreground=P["muted"], font=FONT_SECTION,
                    padding=(8, 5), relief="flat")
    style.map("Treeview.Heading", background=[("active", P["hover"])], lightcolor=[("active", P["hover"])],
              darkcolor=[("active", P["hover"])])

    style.configure("TNotebook", **_flat(P["bg"]), tabmargins=(0, 2, 0, 0))
    style.configure("TNotebook.Tab", **_flat(P["bg"]), foreground=P["muted"], padding=(12, 6))
    style.map("TNotebook.Tab", background=[("selected", P["panel"]), ("active", P["panel_hi"])],
              lightcolor=[("selected", P["accent"]), ("active", P["panel_hi"])],
              darkcolor=[("selected", P["panel"])], bordercolor=[("selected", P["panel"])],
              foreground=[("selected", P["accent"]), ("active", P["text"])])
    style.configure("Sub.TNotebook", **_flat(P["panel"]), tabmargins=(4, 4, 4, 0))
    style.configure("Sub.TNotebook.Tab", **_flat(P["panel"]), foreground=P["muted"], padding=(10, 4),
                    font=FONT_SMALL)
    style.map("Sub.TNotebook.Tab", background=[("selected", P["bg"]), ("active", P["panel_hi"])],
              lightcolor=[("selected", P["bg"])], darkcolor=[("selected", P["bg"])],
              bordercolor=[("selected", P["bg"])], foreground=[("selected", P["text"]), ("active", P["text"])])

    for orient in ("Vertical", "Horizontal"):
        style.layout(f"{orient}.TScrollbar", [(f"{orient}.Scrollbar.trough", {"sticky": "nswe", "children": [
            (f"{orient}.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})])
    style.configure("TScrollbar", troughcolor=P["bg"], **{k: P["border_hi"] for k in ("background", "lightcolor", "darkcolor")},
                    bordercolor=P["bg"], gripcount=0, arrowsize=9)
    style.map("TScrollbar", background=[("pressed", P["muted"]), ("active", P["faint"])],
              lightcolor=[("active", P["faint"])], darkcolor=[("active", P["faint"])])

    for base, bg in (("TCheckbutton", P["bg"]), ("TRadiobutton", P["bg"]),
                     ("Card.TCheckbutton", P["panel"]), ("Card.TRadiobutton", P["panel"])):
        style.configure(base, background=bg, foreground=P["text"], indicatorbackground=P["field"],
                        indicatorforeground=P["accent"], upperbordercolor=P["border_hi"],
                        lowerbordercolor=P["border_hi"], focuscolor=bg, indicatormargin=(0, 0, 6, 0))
        style.map(base, background=[("active", bg)], indicatorbackground=[("pressed", P["panel_hi"])],
                  foreground=[("active", P["accent_hi"])])
    style.configure("Changed.TCheckbutton", foreground=P["warning"], indicatorbackground=P["changed_bg"],
                    upperbordercolor=P["warning"], lowerbordercolor=P["warning"])
    style.configure("TPanedwindow", background=P["bg"])
    style.configure("Sash", sashthickness=8, gripcount=0, **_flat(P["bg"]))
    style.configure("Accent.Horizontal.TProgressbar", troughcolor=P["field"], **_flat(P["accent"]), thickness=4)
    style.configure("TSeparator", background=P["border"])
    return style


class ScrollFrame(ttk.Frame):
    """Vertically scrollable area. Call bind_wheel() after filling ``inner``."""

    def __init__(self, master, *, padding=12, style="TFrame", background=None):
        super().__init__(master, style=style)
        self.canvas = tk.Canvas(self, borderwidth=0, highlightthickness=0, background=background or P["bg"])
        self.bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas, padding=padding, style=style)
        window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.bar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.bar.pack(side="right", fill="y")
        self.inner.bind("<Configure>", lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(
            window, width=max(e.width, self.inner.winfo_reqwidth())))

    def _on_wheel(self, event):
        if event.delta:
            units = -int(event.delta / 120) or (-1 if event.delta > 0 else 1)
            self.canvas.yview_scroll(units * 3, "units")
        return "break"

    def bind_wheel(self, widget=None):
        """Scroll even while the pointer is over a label, entry or combobox."""
        widget = widget or self
        widget.bind("<MouseWheel>", self._on_wheel, add="+")
        for child in widget.winfo_children():
            self.bind_wheel(child)


class SearchBox(ttk.Frame):
    """Entry with a search glyph, placeholder text and a clear button."""

    def __init__(self, master, variable: tk.StringVar, placeholder: str, icons=None):
        super().__init__(master, style="Search.TFrame", padding=(8, 2, 4, 2))
        ttk.Style(self).configure("Search.TFrame", background=P["field"], bordercolor=P["border"])
        self.variable = variable
        if icons:
            ttk.Label(self, image=icons("search", P["muted"], 14), background=P["field"]).pack(side="left")
        self.entry = ttk.Entry(self, textvariable=variable, style="Bare.TEntry")
        self.entry.pack(side="left", fill="x", expand=True, padx=(6, 2))
        self.hint = tk.Label(self, text=placeholder, bg=P["field"], fg=P["faint"], font=FONT, cursor="xterm")
        self.hint.bind("<Button-1>", lambda _e: self.entry.focus_set())
        self.clear = ttk.Button(self, style="Icon.TButton", command=lambda: (variable.set(""), self.entry.focus_set()),
                                takefocus=False, **({"image": icons("clear", P["muted"], 12)} if icons else {"text": "×"}))
        variable.trace_add("write", lambda *_: self._sync())
        self.entry.bind("<Escape>", lambda _e: variable.set(""))
        self._sync()

    def _sync(self):
        if self.variable.get():
            self.hint.place_forget()
            self.clear.pack(side="right")
        else:
            self.clear.pack_forget()
            self.hint.place(in_=self.entry, x=4, rely=0.5, anchor="w")
