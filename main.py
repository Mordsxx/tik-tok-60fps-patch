"""TikTok 60FPS Patch - entry point.

    python main.py                       -> GUI
    python main.py in.mp4 [-o out.mp4]   -> command line (same engine)
"""
from __future__ import annotations

import argparse
import sys
from fractions import Fraction
from pathlib import Path


def cli(argv) -> int:
    from mp4_parser import MP4Error
    from mp4_patcher import patch_file, unique_output_path
    from video_analyzer import analyze

    ap = argparse.ArgumentParser(prog="tiktok-60fps-patch")
    ap.add_argument("input")
    ap.add_argument("-o", "--output")
    ap.add_argument("--factor", default="2", help="slow-down factor (default 2)")
    ap.add_argument("--no-ffprobe", action="store_true")
    a = ap.parse_args(argv)
    src = Path(a.input)
    try:
        info = analyze(src)
        print(f"{src.name}: {info.width}x{info.height} {info.fps_text} fps, "
              f"{info.duration:.2f}s, {info.frame_count} frames, "
              f"{info.video_codec} + {info.audio_codec}")
        dst = Path(a.output) if a.output else unique_output_path(src, src.parent)
        r = patch_file(src, dst, Fraction(a.factor), use_ffprobe=not a.no_ffprobe,
                       progress=lambda s, f: None)
    except (MP4Error, OSError, ValueError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    o = r.info_after
    print(f"-> {r.output}\n   {o.fps_text} fps, {o.duration:.2f}s, {o.frame_count} frames, "
          f"{r.output_size / 1e6:.2f} MB, {r.elapsed * 1000:.0f} ms")
    for s in r.strategies:
        print("   *", s)
    for c in r.checks:
        print(f"   [{'OK' if c.ok else 'FAIL'}] {c.name}  {c.detail}")
    return 0


def main():
    if len(sys.argv) > 1:
        sys.exit(cli(sys.argv[1:]))
    from gui import run
    run()


if __name__ == "__main__":
    main()
