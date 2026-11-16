"""Tkinter desktop interface (drag-and-drop if tkinterdnd2 is installed)."""
from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from fractions import Fraction
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from mp4_parser import MP4Error
from mp4_patcher import Cancelled, patch_file, unique_output_path
from video_analyzer import analyze

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    _Root = TkinterDnD.Tk
except Exception:                      # optional dependency
    DND_FILES, _Root = None, tk.Tk


def _size(n: int) -> str:
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.1f} {u}" if u != "B" else f"{n} B"
        n /= 1024


class App:
    def __init__(self):
        self.root = r = _Root()
        r.title("TikTok 60FPS Patch")
        r.geometry("560x640")
        r.minsize(520, 600)
        self.src: Path | None = None
        self.out_dir = tk.StringVar()
        self.factor = tk.StringVar(value="2")
        self.status = tk.StringVar(value="Select an MP4 file to begin.")
        self.name_var = tk.StringVar(value="No file selected")
        self.info_vars = {k: tk.StringVar(value="—") for k in
                          ("Size", "Resolution", "Frame rate", "Duration",
                           "Video codec", "Audio codec", "Frames")}
        self.done_vars = {k: tk.StringVar(value="—") for k in
                          ("Output file", "Duration", "Frame rate", "File size", "Time")}
        self.q: queue.Queue = queue.Queue()
        self.cancel_ev = threading.Event()
        self.worker: threading.Thread | None = None
        self.last_out: Path | None = None
        self._build()
        r.after(80, self._poll)

    # ---------------------------------------------------------------- UI
    def _build(self):
        pad = dict(padx=14, pady=6)
        st = ttk.Style()
        if "vista" in st.theme_names():
            st.theme_use("vista")
        f = ttk.LabelFrame(self.root, text="1. Video")
        f.pack(fill="x", **pad)
        drop = ttk.Label(f, textvariable=self.name_var, anchor="center", relief="groove",
                         padding=14)
        drop.pack(fill="x", padx=10, pady=(8, 4))
        hint = "Drop an MP4 here or " if DND_FILES else ""
        ttk.Button(f, text=hint + "Browse…", command=self.browse).pack(pady=(0, 8))
        if DND_FILES:
            for w in (drop, f, self.root):
                w.drop_target_register(DND_FILES)
                w.dnd_bind("<<Drop>>", self._on_drop)

        f = ttk.LabelFrame(self.root, text="2. Video information")
        f.pack(fill="x", **pad)
        for i, (k, v) in enumerate(self.info_vars.items()):
            ttk.Label(f, text=k + ":").grid(row=i // 2, column=(i % 2) * 2, sticky="e",
                                            padx=(10, 4), pady=2)
            ttk.Label(f, textvariable=v).grid(row=i // 2, column=(i % 2) * 2 + 1,
                                              sticky="w", padx=(0, 10))

        f = ttk.LabelFrame(self.root, text="3. Settings")
        f.pack(fill="x", **pad)
        ttk.Label(f, text="Mode:").grid(row=0, column=0, sticky="e", padx=8, pady=4)
        cb = ttk.Combobox(f, textvariable=self.factor, values=("2", "4"), width=4,
                          state="readonly")
        cb.grid(row=0, column=1, sticky="w")
        self.mode_lbl = ttk.Label(f, text="60 FPS → 30 FPS  (2× slower)")
        self.mode_lbl.grid(row=0, column=2, sticky="w", padx=8)
        cb.bind("<<ComboboxSelected>>", lambda e: self._mode_text())
        ttk.Label(f, text="Output folder:").grid(row=1, column=0, sticky="e", padx=8, pady=4)
        ttk.Entry(f, textvariable=self.out_dir, width=38).grid(row=1, column=1, columnspan=2,
                                                               sticky="we")
        ttk.Button(f, text="…", width=3, command=self.pick_dir).grid(row=1, column=3, padx=6)

        f = ttk.Frame(self.root)
        f.pack(fill="x", **pad)
        self.btn_start = ttk.Button(f, text="Start Processing", command=self.start,
                                    state="disabled")
        self.btn_start.pack(side="left", expand=True, fill="x", padx=(0, 4))
        self.btn_cancel = ttk.Button(f, text="Cancel", command=self.cancel, state="disabled")
        self.btn_cancel.pack(side="left", expand=True, fill="x", padx=(4, 0))
        self.bar = ttk.Progressbar(self.root, maximum=100)
        self.bar.pack(fill="x", padx=14)
        ttk.Label(self.root, textvariable=self.status, wraplength=520).pack(padx=14, pady=4)

        f = ttk.LabelFrame(self.root, text="Result")
        f.pack(fill="x", **pad)
        for i, (k, v) in enumerate(self.done_vars.items()):
            ttk.Label(f, text=k + ":").grid(row=i, column=0, sticky="e", padx=(10, 4), pady=1)
            ttk.Label(f, textvariable=v).grid(row=i, column=1, sticky="w")
        self.btn_open = ttk.Button(f, text="Open Output Folder", command=self.open_folder,
                                   state="disabled")
        self.btn_open.grid(row=len(self.done_vars), column=0, columnspan=2, pady=8)

    # ------------------------------------------------------------- actions
    def _mode_text(self):
        n = int(self.factor.get())
        self.mode_lbl.config(text=f"60 FPS → {60 // n} FPS  ({n}× slower)")

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
            self.btn_start.config(state="disabled")
            self.status.set(f"Cannot use this file: {e}")
            messagebox.showerror("Unsupported file", str(e))
            return
        self.src = path
        self.name_var.set(path.name)
        v = self.info_vars
        v["Size"].set(_size(info.file_size))
        v["Resolution"].set(f"{info.width} × {info.height}" if info.width else "—")
        v["Frame rate"].set(info.fps_text + (" fps" if info.fps else ""))
        v["Duration"].set(f"{info.duration:.2f} s")
        v["Video codec"].set(info.video_codec)
        v["Audio codec"].set(info.audio_codec)
        v["Frames"].set(str(info.frame_count or "—"))
        if not self.out_dir.get():
            self.out_dir.set(str(path.parent))
        msgs = list(info.warnings)
        if info.fps and abs(float(info.fps) - 60) > 1:
            msgs.append(f"Input is {info.fps_text} fps, not 60.")
        self.status.set(" ".join(msgs) or "Ready.")
        self.btn_start.config(state="normal")

    def start(self):
        if not self.src or self.worker:
            return
        out_dir = Path(self.out_dir.get() or self.src.parent)
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            messagebox.showerror("Output folder", str(e))
            return
        dst = unique_output_path(self.src, out_dir)
        self.cancel_ev.clear()
        self.btn_start.config(state="disabled")
        self.btn_cancel.config(state="normal")
        self.btn_open.config(state="disabled")
        for v in self.done_vars.values():
            v.set("—")
        factor = Fraction(int(self.factor.get()))

        def work():
            try:
                r = patch_file(self.src, dst, factor,
                               progress=lambda s, f: self.q.put(("p", s, f)),
                               cancel=self.cancel_ev.is_set)
                self.q.put(("ok", r))
            except Cancelled:
                self.q.put(("cancel",))
            except Exception as e:                       # show, never crash
                self.q.put(("err", f"{type(e).__name__}: {e}"))
        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def cancel(self):
        self.cancel_ev.set()
        self.status.set("Cancelling…")

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
                    self.status.set(m[1])
                    self.bar["value"] = m[2] * 100
                else:
                    self.worker = None
                    self.btn_cancel.config(state="disabled")
                    self.btn_start.config(state="normal" if self.src else "disabled")
                    if m[0] == "ok":
                        self._done(m[1])
                    elif m[0] == "cancel":
                        self.bar["value"] = 0
                        self.status.set("Cancelled. No output was written.")
                    else:
                        self.bar["value"] = 0
                        self.status.set("Failed. The original file was not modified.")
                        messagebox.showerror("Processing failed", m[1])
        except queue.Empty:
            pass
        self.root.after(80, self._poll)

    def _done(self, r):
        o = r.info_after
        self.last_out = r.output
        self.bar["value"] = 100
        self.status.set("Done — all validation checks passed.")
        d = self.done_vars
        d["Output file"].set(r.output.name)
        d["Duration"].set(f"{o.duration:.2f} s")
        d["Frame rate"].set(o.fps_text + " fps")
        d["File size"].set(_size(r.output_size))
        d["Time"].set(f"{r.elapsed:.2f} s")
        self.btn_open.config(state="normal")


def run():
    App().root.mainloop()
