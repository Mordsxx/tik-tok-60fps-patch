"""TikTok 60FPS Patch - liquid-glass interface.

Everything is drawn on one Canvas: an animated blurred-blob background that reacts to the
mouse, frosted glass panels, pill buttons with hover glow + click ripples, and a liquid
progress bar.  Pillow renders the visuals; Tk draws the text on top.
"""
from __future__ import annotations

import math
import os
import queue
import subprocess
import sys
import threading
import time
import webbrowser
from fractions import Fraction
from pathlib import Path
from tkinter import filedialog, messagebox
import tkinter as tk

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageTk

from mp4_parser import MP4Error
from mp4_patcher import Cancelled, patch_file, unique_output_path
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
FACTOR = Fraction(2)                   # fixed: 60 FPS -> 30 FPS, 2x slower

# ------------------------------------------------------------------ look
W, H = 560, 820
LOW = 4                                # background is rendered at 1/4 size, then upscaled
TEXT, MUTED = "#f1f2f8", "#9aa0b6"
ACCENT, CYAN, OK, ERR, WARN = "#a78bfa", "#67e8f9", "#34d399", "#ff6b7a", "#fbbf24"
FONT = "Segoe UI"
PAD, SS = 12, 3

PANELS = {"drop": (20, 104, 540, 196), "info": (20, 208, 540, 360),
          "set": (20, 372, 540, 454), "res": (20, 586, 540, 752),
          "foot": (110, 764, 450, 808)}
BAR = (20, 532, 540, 546)


def px(points: float) -> int:          # pixel-sized fonts keep layout DPI-independent
    return -int(round(points * 1.33))


def res(name: str) -> Path:
    base = getattr(sys, "_MEIPASS", None) or Path(__file__).resolve().parent
    return Path(base) / name


def fmt(x, digits: int = 2) -> str:
    return (f"{float(x):.{digits}f}".rstrip("0").rstrip(".")) or "0"


def size_text(n: float) -> str:
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{int(n)} B" if u == "B" else f"{n:.1f} {u}"
        n /= 1024


def ellipsize(s: str, n: int, tail: bool = False) -> str:
    if len(s) <= n:
        return s
    return "…" + s[-(n - 1):] if tail else s[:n - 1] + "…"


class Btn:
    def __init__(self, name, rect, text, style, cmd):
        self.name, self.rect, self.text, self.style, self.cmd = name, rect, text, style, cmd
        self.enabled, self.hover, self.target = True, 0.0, 0.0
        self.ripples: list = []
        self.text_id = None

    def hit(self, x, y):
        x0, y0, x1, y1 = self.rect
        return x0 <= x <= x1 and y0 <= y <= y1


class App:
    def __init__(self):
        self.root = r = _Root()
        r.title(APP_NAME)
        r.geometry(f"{W}x{H}")
        r.resizable(False, False)
        r.configure(bg="#0b0c12")
        try:
            r.iconbitmap(str(res("app.ico")))
        except Exception:
            pass
        self.canvas = c = tk.Canvas(r, width=W, height=H, highlightthickness=0, bd=0, bg="#0b0c12")
        c.pack()

        # state
        self.src: Path | None = None
        self.src_fps: Fraction | None = None
        self.out_dir = ""
        self.last_out: Path | None = None
        self.worker: threading.Thread | None = None
        self.q: queue.Queue = queue.Queue()
        self.cancel_ev = threading.Event()
        self.progress, self.show_bar = 0.0, False
        self.t, self.energy = 0.0, 1.0
        self.mx, self.my = W / 2 / LOW, H / 2 / LOW
        self.mtx, self.mty = self.mx, self.my
        self._tile_cache: dict = {}
        self._last = time.perf_counter()

        self._build_background()
        self._build_panels()
        self._build_widgets()
        self.photo = ImageTk.PhotoImage(self._render(self._last))
        self.bg_id = c.create_image(0, 0, anchor="nw", image=self.photo)
        c.tag_lower(self.bg_id)
        c.bind("<Motion>", self._motion)
        c.bind("<Leave>", self._leave)
        c.bind("<Button-1>", self._click)
        if DND_FILES:
            try:
                c.drop_target_register(DND_FILES)
                c.dnd_bind("<<Drop>>", self._on_drop)
            except Exception:
                pass
        r.after(33, self._tick)

    # ============================================================ visuals
    def _build_background(self):
        lw, lh = W // LOW, H // LOW
        base = Image.new("RGB", (lw, lh))
        d = ImageDraw.Draw(base)
        for y in range(lh):
            k = y / lh
            d.line([(0, y), (lw, y)], fill=(int(14 - 6 * k), int(15 - 6 * k), int(27 - 11 * k)))
        self.base = base
        spr = Image.radial_gradient("L").point(lambda v: int(255 * (1 - min(1.0, v / 180)) ** 2 * 0.95))
        specs = [  # colour, radius, centre, amplitude, speed, phase
            ((124, 92, 246), 62, (0.25, 0.20), (0.18, 0.10), (0.55, 0.43), (0.0, 1.0)),
            ((56, 189, 248), 56, (0.80, 0.45), (0.16, 0.14), (0.40, 0.62), (2.0, 0.5)),
            ((244, 114, 182), 54, (0.30, 0.78), (0.20, 0.12), (0.47, 0.36), (4.0, 2.2)),
            ((45, 212, 191), 40, (0.78, 0.88), (0.14, 0.08), (0.62, 0.50), (1.2, 3.1)),
        ]
        self.blobs = []
        for col, rad, (cx, cy), (ax, ay), (wx, wy), (px_, py_) in specs:
            self.blobs.append(dict(col=Image.new("RGB", (2 * rad, 2 * rad), col),
                                   mask=spr.resize((2 * rad, 2 * rad), Image.BILINEAR), r=rad,
                                   cx=cx * lw, cy=cy * lh, ax=ax * lw, ay=ay * lh,
                                   wx=wx, wy=wy, px=px_, py=py_))
        self.mouse_blob = dict(col=Image.new("RGB", (92, 92), (167, 139, 250)),
                               mask=spr.resize((92, 92), Image.BILINEAR), r=46)

    def _build_panels(self):
        s = 2
        layer = Image.new("RGBA", (W * s, H * s), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        for name, (x0, y0, x1, y1) in PANELS.items():
            r = (y1 - y0) // 2 if name == "foot" else 22
            d.rounded_rectangle((x0 * s, y0 * s, x1 * s, y1 * s), radius=r * s,
                                fill=(14, 15, 26, 150), outline=(255, 255, 255, 46), width=s)
        bx0, by0, bx1, by1 = BAR
        d.rounded_rectangle((bx0 * s, by0 * s, bx1 * s, by1 * s), radius=7 * s,
                            fill=(255, 255, 255, 24), outline=(255, 255, 255, 34), width=s)
        d.ellipse((22 * s, 18 * s, 82 * s, 78 * s), outline=(167, 139, 250, 170), width=2 * s)
        layer = layer.resize((W, H), Image.LANCZOS)
        try:
            logo = Image.open(res("app_icon.png")).convert("RGBA").resize((52, 52), Image.LANCZOS)
            m = Image.new("L", (208, 208), 0)
            ImageDraw.Draw(m).ellipse((0, 0, 207, 207), fill=255)
            layer.paste(logo, (26, 22), m.resize((52, 52), Image.LANCZOS))
        except Exception:
            pass
        self.panels = layer

    def _gradient(self, w, h, cols, lift=0.0):
        g = Image.new("RGB", (len(cols), 1))
        for i, c in enumerate(cols):
            g.putpixel((i, 0), tuple(int(v + (255 - v) * lift) for v in c))
        return g.resize((w, h), Image.BILINEAR)

    def _button_tile(self, b: Btn, now: float):
        x0, y0, x1, y1 = b.rect
        w, h = x1 - x0, y1 - y0
        b.ripples = [rp for rp in b.ripples if now - rp[0] < 0.7]
        key = (b.name, int(b.hover * 8 + 0.5), b.enabled)
        if not b.ripples and key in self._tile_cache:
            return self._tile_cache[key]
        w2, h2 = w * SS, h * SS
        rad = 22 if b.style == "zone" else h // 2
        mask = Image.new("L", (w2, h2), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, w2 - 1, h2 - 1), radius=rad * SS, fill=255)
        hv = b.hover
        glow_col = (124, 92, 246)
        if b.style == "primary" and b.enabled:
            fill = self._gradient(w2, h2, [(124, 92, 246), (99, 102, 241), (56, 189, 248)],
                                  lift=hv * 0.14).convert("RGBA")
            fill.putalpha(mask)
            outline = (255, 255, 255, 80)
        else:
            a = int((14 + hv * 34) if b.enabled else 9)
            fill = Image.new("RGBA", (w2, h2), (255, 255, 255, a))
            fill.putalpha(ImageChops.multiply(fill.getchannel("A"), mask))
            outline = (167, 139, 250, int(46 + hv * 170)) if b.style == "zone" else \
                      (255, 255, 255, int(48 + hv * 70) if b.enabled else 26)
            glow_col = (167, 139, 250)
        ol = Image.new("RGBA", (w2, h2), (0, 0, 0, 0))
        ImageDraw.Draw(ol).rounded_rectangle((0, 0, w2 - 1, h2 - 1), radius=rad * SS,
                                             outline=outline, width=SS)
        fill.alpha_composite(ol)
        for t0, rx, ry in b.ripples:
            p = (now - t0) / 0.7
            rr = max(w, h) * 1.15 * p * SS
            cm = Image.new("L", (w2, h2), 0)
            ImageDraw.Draw(cm).ellipse((rx * SS - rr, ry * SS - rr, rx * SS + rr, ry * SS + rr),
                                       fill=int(120 * (1 - p)))
            cm = ImageChops.multiply(cm, mask)
            wl = Image.new("RGBA", (w2, h2), (255, 255, 255, 0))
            wl.putalpha(cm)
            fill.alpha_composite(wl)
        tile = Image.new("RGBA", (w + 2 * PAD, h + 2 * PAD), (0, 0, 0, 0))
        if hv > 0.02 and b.enabled:
            gm = Image.new("L", tile.size, 0)
            ImageDraw.Draw(gm).rounded_rectangle((PAD, PAD, PAD + w, PAD + h), radius=rad,
                                                 fill=int(120 * hv))
            gc = Image.new("RGBA", tile.size, glow_col + (0,))
            gc.putalpha(gm.filter(ImageFilter.GaussianBlur(7)))
            tile.alpha_composite(gc)
        tile.alpha_composite(fill.resize((w, h), Image.LANCZOS), (PAD, PAD))
        if not b.ripples:
            self._tile_cache[key] = tile
        return tile

    def _bar_tile(self, frac: float, phase: float):
        w, h = BAR[2] - BAR[0], BAR[3] - BAR[1]
        w2, h2 = w * SS, h * SS
        track = Image.new("L", (w2, h2), 0)
        ImageDraw.Draw(track).rounded_rectangle((0, 0, w2 - 1, h2 - 1), radius=h2 // 2, fill=255)
        edge = frac * w2
        m = Image.new("L", (w2, h2), 0)
        amp = 0 if frac >= 1 else 3.2 * SS
        pts = [(0, 0)] + [(edge + amp * math.sin(phase + yy / (SS * 2.2)), yy)
                          for yy in range(0, h2 + 1, 2)] + [(0, h2)]
        ImageDraw.Draw(m).polygon(pts, fill=255)
        m = ImageChops.multiply(m, track)
        g = self._gradient(w2, h2, [(124, 92, 246), (56, 189, 248), (244, 114, 182)]).convert("RGBA")
        g.putalpha(m)
        return g.resize((w, h), Image.LANCZOS)

    def _render(self, now: float) -> Image.Image:
        img = self.base.copy()
        for b in self.blobs:
            x = b["cx"] + b["ax"] * math.sin(b["wx"] * self.t + b["px"])
            y = b["cy"] + b["ay"] * math.cos(b["wy"] * self.t + b["py"])
            img.paste(b["col"], (int(x - b["r"]), int(y - b["r"])), b["mask"])
        mb = self.mouse_blob
        img.paste(mb["col"], (int(self.mx - mb["r"]), int(self.my - mb["r"])), mb["mask"])
        frame = img.resize((W, H), Image.BICUBIC).convert("RGBA")
        frame.alpha_composite(self.panels)
        for b in self.buttons:
            if b.style == "zone" and b.hover < 0.02 and not b.ripples:
                continue
            frame.alpha_composite(self._button_tile(b, now), (b.rect[0] - PAD, b.rect[1] - PAD))
        if self.show_bar and self.progress > 0:
            frame.alpha_composite(self._bar_tile(min(self.progress, 1.0), self.t * 4.0),
                                  (BAR[0], BAR[1]))
        return frame.convert("RGB")

    # ============================================================ widgets
    def _text(self, x, y, text="", size=10, bold=False, fill=TEXT, anchor="w", **kw):
        return self.canvas.create_text(x, y, text=text, fill=fill, anchor=anchor,
                                       font=(FONT, px(size), "bold" if bold else "normal"), **kw)

    def _build_widgets(self):
        t = self._text
        t(98, 36, APP_NAME, 19, True)
        t(98, 62, "Slow 60 FPS → 30 FPS  ·  timing only, no re-encode", 9, fill=MUTED)

        self.zone_txt = t(280, 150, "Drop an MP4 here\nor click to browse", 12, anchor="center",
                          justify="center", width=480)

        self.info = {}
        keys = [("Size", "Resolution"), ("Frame rate", "Duration"), ("Video", "Audio"),
                ("Frames", None)]
        for r, pair in enumerate(keys):
            for col, k in enumerate(pair):
                if not k:
                    continue
                x, y = 44 + col * 250, 224 + r * 34
                t(x, y, k, 8, fill=MUTED)
                self.info[k] = t(x, y + 15, "—", 10, True)

        self.mode_txt = t(40, 392, f"60 FPS  →  30 FPS   ·   {fmt(FACTOR)}× slower", 11, True, fill=CYAN)
        t(40, 424, "Output", 8, fill=MUTED)
        self.out_txt = t(92, 424, "same folder as the video", 9, fill=TEXT)

        self.done = {}
        for k, (col, row) in {"File": (0, 0), "Duration": (0, 1), "Frame rate": (1, 1),
                              "Size": (0, 2), "Time": (1, 2)}.items():
            x, y = 44 + col * 250, 600 + row * 36
            t(x, y, k, 8, fill=MUTED)
            self.done[k] = t(x, y + 16, "—", 10, True)

        self.status_txt = t(W // 2, 570, "Select an MP4 file to begin.", 9, fill=MUTED,
                            anchor="center", justify="center", width=500)

        self.credit_l = t(266, 786, CREDIT_TEXT, 10, True, fill=TEXT, anchor="e")
        t(280, 786, "•", 10, fill=MUTED, anchor="center")
        self.credit_r = t(294, 786, TIKTOK_TEXT, 10, True, fill=ACCENT)

        self.buttons = [
            Btn("zone", PANELS["drop"], "", "zone", self.browse),
            Btn("start", (20, 468, 372, 516), "Start Processing", "primary", self.start),
            Btn("cancel", (384, 468, 540, 516), "Cancel", "ghost", self.cancel),
            Btn("browse", (432, 408, 524, 440), "Browse", "ghost", self.pick_dir),
            Btn("open", (150, 706, 410, 740), "Open Output Folder", "ghost", self.open_folder),
        ]
        for b in self.buttons:
            if b.text:
                x0, y0, x1, y1 = b.rect
                b.text_id = t((x0 + x1) / 2, (y0 + y1) / 2, b.text, 10, True, anchor="center")
        self.btn = {b.name: b for b in self.buttons}
        for name in ("start", "cancel", "open"):
            self._enable(self.btn[name], False)

    def _enable(self, b: Btn, on: bool):
        b.enabled = on
        if not on:
            b.target = 0.0
        if b.text_id:
            self.canvas.itemconfig(b.text_id, fill=TEXT if on else "#5d6277")

    def _set(self, item, text, fill=None):
        self.canvas.itemconfig(item, text=text, **({"fill": fill} if fill else {}))

    def _status(self, text, kind="info"):
        col = {"info": MUTED, "ok": OK, "err": ERR, "warn": WARN}[kind]
        self._set(self.status_txt, text, col)

    # ============================================================ events
    def _hit(self, x, y):
        for b in reversed(self.buttons):
            if b.hit(x, y) and (b.enabled or b.style == "zone"):
                return b
        return None

    def _link_hit(self, x, y):
        bb = self.canvas.bbox(self.credit_r)
        return bool(bb) and bb[0] - 6 <= x <= bb[2] + 6 and bb[1] - 6 <= y <= bb[3] + 6

    def _motion(self, e):
        self.mtx, self.mty = e.x / LOW, e.y / LOW
        hit = self._hit(e.x, e.y)
        for b in self.buttons:
            b.target = 1.0 if (b is hit and b.enabled) else 0.0
        self.canvas.config(cursor="hand2" if (hit or self._link_hit(e.x, e.y)) else "")

    def _leave(self, _e):
        for b in self.buttons:
            b.target = 0.0
        self.mtx, self.mty = W / 2 / LOW, H / 2 / LOW

    def _click(self, e):
        if self._link_hit(e.x, e.y):
            webbrowser.open(TIKTOK_URL)
            self.energy += 1.0
            return
        b = self._hit(e.x, e.y)
        if b:
            b.ripples.append((time.perf_counter(), e.x - b.rect[0], e.y - b.rect[1]))
            self.energy += 1.2
            b.cmd()

    def _on_drop(self, ev):
        paths = self.root.tk.splitlist(ev.data)
        if paths:
            self.load(Path(paths[0]))

    # ============================================================ actions
    def browse(self):
        p = filedialog.askopenfilename(filetypes=[("MP4 video", "*.mp4 *.mov *.m4v"),
                                                  ("All files", "*.*")])
        if p:
            self.load(Path(p))

    def pick_dir(self):
        d = filedialog.askdirectory()
        if d:
            self.out_dir = d
            self._set(self.out_txt, ellipsize(d, 44, tail=True))

    def load(self, path: Path):
        try:
            info = analyze(path)
        except (MP4Error, OSError) as e:
            self.src = self.src_fps = None
            self._enable(self.btn["start"], False)
            self._status(f"Cannot use this file: {e}", "err")
            messagebox.showerror("Unsupported file", str(e))
            return
        self.src, self.src_fps = path, info.fps
        self.energy += 1.5
        self._set(self.zone_txt, f"{ellipsize(path.name, 46)}\n{size_text(info.file_size)}")
        vals = {"Size": size_text(info.file_size),
                "Resolution": f"{info.width} × {info.height}" if info.width else "—",
                "Frame rate": info.fps_text + (" fps" if info.fps else ""),
                "Duration": f"{info.duration:.2f} s", "Video": info.video_codec,
                "Audio": info.audio_codec, "Frames": str(info.frame_count or "—")}
        for k, v in vals.items():
            self._set(self.info[k], v)
        if not self.out_dir:
            self.out_dir = str(path.parent)
            self._set(self.out_txt, ellipsize(self.out_dir, 44, tail=True))
        if info.fps:
            self._set(self.mode_txt, f"{fmt(info.fps)} FPS  →  {fmt(info.fps / FACTOR)} FPS"
                                     f"   ·   {fmt(FACTOR)}× slower")
        msgs = list(info.warnings)
        if info.fps and abs(float(info.fps) - 60) > 1:
            msgs.append(f"Input is {info.fps_text} fps, not 60.")
        self._status(" ".join(msgs) or "Ready.", "warn" if msgs else "info")
        self._enable(self.btn["start"], True)

    def start(self):
        if not self.src or self.worker:
            return
        out_dir = Path(self.out_dir or self.src.parent)
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            messagebox.showerror("Output folder", str(e))
            return
        suffix = f"_{fmt(self.src_fps / FACTOR)}fps" if self.src_fps else "_slow"
        dst = unique_output_path(self.src, out_dir, suffix)
        self.cancel_ev.clear()
        self._enable(self.btn["start"], False)
        self._enable(self.btn["cancel"], True)
        self._enable(self.btn["open"], False)
        for item in self.done.values():
            self._set(item, "—")
        self.progress, self.show_bar = 0.01, True
        src = self.src

        def work():
            try:
                r = patch_file(src, dst, FACTOR,
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
        self._status("Cancelling…", "warn")

    def open_folder(self):
        if not self.last_out:
            return
        d = str(self.last_out.parent)
        if os.name == "nt":
            os.startfile(d)                                # type: ignore[attr-defined]
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", d])

    def _drain(self):
        try:
            while True:
                m = self.q.get_nowait()
                if m[0] == "p":
                    self._status(m[1])
                    self.progress = max(0.01, m[2])
                    continue
                self.worker = None
                self._enable(self.btn["cancel"], False)
                self._enable(self.btn["start"], bool(self.src))
                if m[0] == "ok":
                    self._done(m[1])
                elif m[0] == "cancel":
                    self.show_bar = False
                    self._status("Cancelled. No output was written.", "warn")
                else:
                    self.show_bar = False
                    self._status("Failed. The original file was not modified.", "err")
                    messagebox.showerror("Processing failed", m[1])
        except queue.Empty:
            pass

    def _done(self, r):
        o = r.info_after
        self.last_out = r.output
        self.progress = 1.0
        self.energy += 2.0
        self._status("Done — all validation checks passed.", "ok")
        vals = {"File": ellipsize(r.output.name, 52), "Duration": f"{o.duration:.2f} s",
                "Frame rate": o.fps_text + " fps", "Size": size_text(r.output_size),
                "Time": f"{r.elapsed:.2f} s"}
        for k, v in vals.items():
            self._set(self.done[k], v)
        self._enable(self.btn["open"], True)

    # ============================================================ loop
    def _tick(self):
        try:
            now = time.perf_counter()
            dt = min(now - self._last, 0.1)
            self._last = now
            self._drain()
            target = 2.2 if self.worker else 1.0
            self.energy += (target - self.energy) * min(1.0, dt * 2.0)
            self.t += dt * self.energy * 0.9
            k = min(1.0, dt * 10)
            for b in self.buttons:
                b.hover += (b.target - b.hover) * k
            m = min(1.0, dt * 4)
            self.mx += (self.mtx - self.mx) * m
            self.my += (self.mty - self.my) * m
            self.photo.paste(self._render(now))
            # shimmering link colour
            s = 0.5 + 0.5 * math.sin(self.t * 1.6)
            r1, g1, b1 = (0xa7, 0x8b, 0xfa)
            r2, g2, b2 = (0x67, 0xe8, 0xf9)
            col = "#%02x%02x%02x" % (int(r1 + (r2 - r1) * s), int(g1 + (g2 - g1) * s),
                                     int(b1 + (b2 - b1) * s))
            self.canvas.itemconfig(self.credit_r, fill=col)
        finally:
            self.root.after(33, self._tick)


def run():
    App().root.mainloop()
