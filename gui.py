"""Dark Tkinter interface for TikTok 60FPS Patch (drag-and-drop if tkinterdnd2 exists)."""
from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from fractions import Fraction
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from mp4_parser import MP4Error
from mp4_patcher import Cancelled, patch_file, unique_output_path
from ui_logic import fmt, fps_to_factor, out_suffix, parse_factor, parse_factor_loose
from video_analyzer import analyze

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    _Root = TkinterDnD.Tk
except Exception:                      # optional dependency
    DND_FILES, _Root = None, tk.Tk

# ------------------------------------------------------------------ branding
APP_NAME = "TikTok 60FPS Patch"
CREDIT_TEXT = "Made by Mords"
TIKTOK_TEXT = "TikTok: @Mords_69"
TIKTOK_URL = "https://www.tiktok.com/@Mords_69"

# ------------------------------------------------------------------ theme
BG, CARD, FIELD, BORDER = "#0e0f14", "#171922", "#212432", "#2b2f40"
TEXT, MUTED = "#eceef5", "#8b91a5"
ACCENT, ACCENT_H = "#8b5cf6", "#a07bff"
OK, ERR, WARN = "#34d399", "#ff5d6c", "#fbbf24"
F = "Segoe UI"


def res(name: str) -> Path:
    base = getattr(sys, "_MEIPASS", None) or Path(__file__).resolve().parent
    return Path(base) / name


def _size(n: float) -> str:
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{int(n)} B" if u == "B" else f"{n:.1f} {u}"
        n /= 1024


class App:
    def __init__(self):
        self.root = r = _Root()
        r.title(APP_NAME)
        r.configure(bg=BG)
        r.geometry("620x820")
        r.minsize(580, 760)
        try:
            r.iconbitmap(str(res("app.ico")))
        except Exception:
            pass
        self.src: Path | None = None
        self.src_fps: Fraction | None = None
        self.last_out: Path | None = None
        self.worker: threading.Thread | None = None
        self.q: queue.Queue = queue.Queue()
        self.cancel_ev = threading.Event()

        self.drop_var = tk.StringVar(value="Drop an MP4 here\nor click to browse")
        self.out_dir = tk.StringVar()
        self.factor_var = tk.StringVar(value="2")
        self.fps_var = tk.StringVar(value="")
        self.mode_var = tk.StringVar(value="2× slower")
        self.status_var = tk.StringVar(value="Select an MP4 file to begin.")
        keys = ("Size", "Resolution", "Frame rate", "Duration", "Video", "Audio", "Frames")
        self.info = {k: tk.StringVar(value="—") for k in keys}
        self.done = {k: tk.StringVar(value="—") for k in
                     ("File", "Duration", "Frame rate", "Size", "Time")}
        self._style()
        self._build()
        r.after(80, self._poll)

    # ------------------------------------------------------------ widgets
    def _style(self):
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure("A.Horizontal.TProgressbar", troughcolor=FIELD, background=ACCENT,
                     bordercolor=BG, lightcolor=ACCENT, darkcolor=ACCENT, thickness=10)

    def _label(self, parent, text="", var=None, fg=TEXT, bg=CARD, size=10, bold=False, **kw):
        font = (F, size, "bold") if bold else (F, size)
        if var is not None:
            return tk.Label(parent, textvariable=var, fg=fg, bg=bg, font=font, **kw)
        return tk.Label(parent, text=text, fg=fg, bg=bg, font=font, **kw)

    def _card(self, title: str):
        outer = tk.Frame(self.root, bg=BG)
        outer.pack(fill="x", padx=20, pady=(0, 10))
        self._label(outer, title.upper(), fg=MUTED, bg=BG, size=8, bold=True).pack(anchor="w", pady=(0, 4))
        body = tk.Frame(outer, bg=CARD, highlightthickness=1, highlightbackground=BORDER)
        body.pack(fill="x")
        return body

    def _entry(self, parent, var, width=9):
        return tk.Entry(parent, textvariable=var, width=width, bg=FIELD, fg=TEXT,
                        insertbackground=TEXT, relief="flat", font=(F, 11), justify="center",
                        highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACCENT)

    def _button(self, parent, text, cmd, primary=False, **kw):
        bg = ACCENT if primary else FIELD
        hover = ACCENT_H if primary else BORDER
        b = tk.Button(parent, text=text, command=cmd, bg=bg, fg="#ffffff" if primary else TEXT,
                      activebackground=hover, activeforeground="#ffffff", relief="flat", bd=0,
                      cursor="hand2", font=(F, 10, "bold"), padx=14, pady=8,
                      disabledforeground=MUTED, **kw)
        b._bg, b._hover = bg, hover
        b.bind("<Enter>", lambda e: b["state"] == "normal" and b.config(bg=b._hover))
        b.bind("<Leave>", lambda e: b.config(bg=b._bg if b["state"] == "normal" else FIELD))
        return b

    @staticmethod
    def _enable(btn, on: bool):
        btn.config(state="normal" if on else "disabled", bg=btn._bg if on else FIELD)

    def _build(self):
        # header
        head = tk.Frame(self.root, bg=BG)
        head.pack(fill="x", padx=20, pady=(18, 12))
        try:
            self.logo = tk.PhotoImage(file=str(res("app_icon.png")))
            tk.Label(head, image=self.logo, bg=BG, bd=0).pack(side="left", padx=(0, 14))
        except Exception:
            pass
        t = tk.Frame(head, bg=BG)
        t.pack(side="left")
        self._label(t, APP_NAME, bg=BG, size=18, bold=True).pack(anchor="w")
        self._label(t, "Slow 60 FPS to 30 FPS by editing timing only — no re-encode",
                    fg=MUTED, bg=BG, size=9).pack(anchor="w")

        # 1. video
        c = self._card("1  ·  Video")
        self.drop = tk.Label(c, textvariable=self.drop_var, bg=FIELD, fg=TEXT, font=(F, 11),
                             pady=16, cursor="hand2", justify="center")
        self.drop.pack(fill="x", padx=12, pady=12)
        self.drop.bind("<Button-1>", lambda e: self.browse())
        if DND_FILES:
            for w in (self.drop, self.root):
                w.drop_target_register(DND_FILES)
                w.dnd_bind("<<Drop>>", self._on_drop)

        # 2. info
        c = self._card("2  ·  Video information")
        g = tk.Frame(c, bg=CARD)
        g.pack(fill="x", padx=12, pady=10)
        for i, (k, v) in enumerate(self.info.items()):
            r, col = divmod(i, 2)
            cell = tk.Frame(g, bg=CARD)
            cell.grid(row=r, column=col, sticky="w", padx=(0, 30), pady=3)
            self._label(cell, k, fg=MUTED, size=8).pack(anchor="w")
            self._label(cell, var=v, size=10, bold=True).pack(anchor="w")
        g.grid_columnconfigure(0, weight=1)
        g.grid_columnconfigure(1, weight=1)

        # 3. settings
        c = self._card("3  ·  Settings")
        row = tk.Frame(c, bg=CARD)
        row.pack(fill="x", padx=12, pady=(12, 4))
        for col, (title, var, handler) in enumerate((("Slow-down  ×", self.factor_var, self._factor_typed),
                                                     ("Output FPS", self.fps_var, self._fps_typed))):
            box = tk.Frame(row, bg=CARD)
            box.pack(side="left", padx=(0, 20))
            self._label(box, title, fg=MUTED, size=8).pack(anchor="w")
            e = self._entry(box, var)
            e.pack(ipady=6)
            e.bind("<KeyRelease>", handler)
        chips = tk.Frame(row, bg=CARD)
        chips.pack(side="left", anchor="s")
        for val in ("1.5", "2", "3", "4"):
            b = self._button(chips, f"{val}×", lambda v=val: self._chip(v))
            b.config(padx=9, pady=5)
            b.pack(side="left", padx=2)
        self._label(c, var=self.mode_var, fg=ACCENT_H, size=10, bold=True).pack(anchor="w", padx=12, pady=(4, 6))
        row = tk.Frame(c, bg=CARD)
        row.pack(fill="x", padx=12, pady=(0, 12))
        self._label(row, "Output folder", fg=MUTED, size=8).pack(anchor="w")
        line = tk.Frame(row, bg=CARD)
        line.pack(fill="x")
        e = tk.Entry(line, textvariable=self.out_dir, bg=FIELD, fg=TEXT, insertbackground=TEXT,
                     relief="flat", font=(F, 10), highlightthickness=1,
                     highlightbackground=BORDER, highlightcolor=ACCENT)
        e.pack(side="left", fill="x", expand=True, ipady=6)
        self._button(line, "Browse", self.pick_dir).pack(side="left", padx=(8, 0))

        # controls
        bar = tk.Frame(self.root, bg=BG)
        bar.pack(fill="x", padx=20, pady=(2, 8))
        self.btn_start = self._button(bar, "Start Processing", self.start, primary=True)
        self.btn_start.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.btn_cancel = self._button(bar, "Cancel", self.cancel)
        self.btn_cancel.pack(side="left", fill="x", expand=True, padx=(6, 0))
        self._enable(self.btn_start, False)
        self._enable(self.btn_cancel, False)
        self.bar = ttk.Progressbar(self.root, maximum=100, style="A.Horizontal.TProgressbar")
        self.bar.pack(fill="x", padx=20)
        self.status_lbl = self._label(self.root, var=self.status_var, fg=MUTED, bg=BG, size=9,
                                      wraplength=560, justify="center")
        self.status_lbl.pack(padx=20, pady=6)

        # result
        c = self._card("Result")
        g = tk.Frame(c, bg=CARD)
        g.pack(fill="x", padx=12, pady=(10, 4))
        for i, (k, v) in enumerate(self.done.items()):
            self._label(g, k, fg=MUTED, size=9).grid(row=i, column=0, sticky="w", pady=1)
            self._label(g, var=v, size=10, bold=True).grid(row=i, column=1, sticky="w", padx=14)
        self.btn_open = self._button(c, "Open Output Folder", self.open_folder)
        self.btn_open.pack(pady=(4, 12))
        self._enable(self.btn_open, False)

        # footer / credits
        foot = tk.Frame(self.root, bg=BG)
        foot.pack(side="bottom", fill="x", pady=(0, 12))
        tk.Frame(foot, bg=BORDER, height=1).pack(fill="x", padx=20, pady=(0, 8))
        self._label(foot, CREDIT_TEXT, fg=MUTED, bg=BG, size=9).pack()
        link = self._label(foot, TIKTOK_TEXT, fg=ACCENT_H, bg=BG, size=9, bold=True, cursor="hand2")
        link.pack()
        link.bind("<Button-1>", lambda e: webbrowser.open(TIKTOK_URL))

    # ------------------------------------------------------------ helpers
    def _set_status(self, text: str, kind: str = "info"):
        self.status_var.set(text)
        self.status_lbl.config(fg={"info": MUTED, "ok": OK, "err": ERR, "warn": WARN}[kind])

    def _refresh_mode(self):
        f = parse_factor_loose(self.factor_var.get())
        if not f:
            self.mode_var.set("Enter a number such as 2 or 1.5")
        elif self.src_fps:
            self.mode_var.set(f"{fmt(self.src_fps, 2)} FPS  →  {fmt(self.src_fps / f, 2)} FPS"
                              f"   ·   {fmt(f, 2)}× slower")
        else:
            self.mode_var.set(f"{fmt(f, 2)}× slower")

    def _factor_typed(self, *_):
        f = parse_factor_loose(self.factor_var.get())
        if f and self.src_fps:
            self.fps_var.set(fmt(self.src_fps / f, 2))
        self._refresh_mode()

    def _fps_typed(self, *_):
        if not self.src_fps:
            return
        f = fps_to_factor(self.src_fps, self.fps_var.get())
        if f:
            self.factor_var.set(fmt(f, 3))
        self._refresh_mode()

    def _chip(self, val: str):
        self.factor_var.set(val)
        self._factor_typed()

    # ------------------------------------------------------------ actions
    def _on_drop(self, ev):
        paths = self.root.tk.splitlist(ev.data)
        if paths:
            self.load(Path(paths[0]))

    def browse(self):
        p = filedialog.askopenfilename(filetypes=[("MP4 video", "*.mp4 *.mov *.m4v"),
                                                  ("All files", "*.*")])
        if p:
            self.load(Path(p))

    def pick_dir(self):
        d = filedialog.askdirectory()
        if d:
            self.out_dir.set(d)

    def load(self, path: Path):
        try:
            info = analyze(path)
        except (MP4Error, OSError) as e:
            self.src = None
            self.src_fps = None
            self._enable(self.btn_start, False)
            self._set_status(f"Cannot use this file: {e}", "err")
            messagebox.showerror("Unsupported file", str(e))
            return
        self.src, self.src_fps = path, info.fps
        self.drop_var.set(f"{path.name}\n{_size(info.file_size)}")
        v = self.info
        v["Size"].set(_size(info.file_size))
        v["Resolution"].set(f"{info.width} × {info.height}" if info.width else "—")
        v["Frame rate"].set(info.fps_text + (" fps" if info.fps else ""))
        v["Duration"].set(f"{info.duration:.2f} s")
        v["Video"].set(info.video_codec)
        v["Audio"].set(info.audio_codec)
        v["Frames"].set(str(info.frame_count or "—"))
        if not self.out_dir.get():
            self.out_dir.set(str(path.parent))
        self._factor_typed()
        msgs = list(info.warnings)
        if info.fps and abs(float(info.fps) - 60) > 1:
            msgs.append(f"Input is {info.fps_text} fps, not 60.")
        self._set_status(" ".join(msgs) or "Ready.", "warn" if msgs else "info")
        self._enable(self.btn_start, True)

    def start(self):
        if not self.src or self.worker:
            return
        try:
            factor = parse_factor(self.factor_var.get())
        except ValueError as e:
            messagebox.showwarning("Slow-down factor", str(e))
            return
        out_dir = Path(self.out_dir.get() or self.src.parent)
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            messagebox.showerror("Output folder", str(e))
            return
        dst = unique_output_path(self.src, out_dir, out_suffix(self.src_fps, factor))
        self.cancel_ev.clear()
        self._enable(self.btn_start, False)
        self._enable(self.btn_cancel, True)
        self._enable(self.btn_open, False)
        for var in self.done.values():
            var.set("—")
        self.bar["value"] = 0
        src = self.src

        def work():
            try:
                r = patch_file(src, dst, factor,
                               progress=lambda s, f: self.q.put(("p", s, f)),
                               cancel=self.cancel_ev.is_set)
                self.q.put(("ok", r))
            except Cancelled:
                self.q.put(("cancel",))
            except Exception as e:                       # show, never crash
                self.q.put(("err", f"{e}"))
        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def cancel(self):
        self.cancel_ev.set()
        self._set_status("Cancelling…", "warn")

    def open_folder(self):
        if not self.last_out:
            return
        d = str(self.last_out.parent)
        if os.name == "nt":
            os.startfile(d)                                # type: ignore[attr-defined]
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", d])

    def _poll(self):
        try:
            while True:
                m = self.q.get_nowait()
                if m[0] == "p":
                    self._set_status(m[1])
                    self.bar["value"] = m[2] * 100
                    continue
                self.worker = None
                self._enable(self.btn_cancel, False)
                self._enable(self.btn_start, bool(self.src))
                if m[0] == "ok":
                    self._done(m[1])
                elif m[0] == "cancel":
                    self.bar["value"] = 0
                    self._set_status("Cancelled. No output was written.", "warn")
                else:
                    self.bar["value"] = 0
                    self._set_status("Failed. The original file was not modified.", "err")
                    messagebox.showerror("Processing failed", m[1])
        except queue.Empty:
            pass
        self.root.after(80, self._poll)

    def _done(self, r):
        o = r.info_after
        self.last_out = r.output
        self.bar["value"] = 100
        self._set_status("Done — all validation checks passed.", "ok")
        d = self.done
        d["File"].set(r.output.name)
        d["Duration"].set(f"{o.duration:.2f} s")
        d["Frame rate"].set(o.fps_text + " fps")
        d["Size"].set(_size(r.output_size))
        d["Time"].set(f"{r.elapsed:.2f} s")
        self._enable(self.btn_open, True)


def run():
    App().root.mainloop()
