"""In-place MP4 timing patcher (no re-encoding, no sample rewrite).

Every field we change has a fixed width, so no box changes size and no chunk
offset (stco/co64) moves.  The output is a byte-for-byte copy of the input
with a handful of integers in ``moov`` rewritten.  See README.md for the
algorithm description.
"""
from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Callable, List, Optional

from mp4_parser import MP4File, MP4Error, UnsupportedMP4, UIntField

CHUNK = 8 * 1024 * 1024


class Cancelled(Exception):
    pass


class ValidationError(MP4Error):
    pass


@dataclass
class Edit:
    offset: int
    data: bytes
    what: str


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class PatchResult:
    output: Path
    factor: Fraction
    elapsed: float
    input_size: int
    output_size: int
    edits: int
    strategies: List[str]
    checks: List[Check] = field(default_factory=list)
    info_before: object = None
    info_after: object = None


# --------------------------------------------------------------------------
# Planning: decide which integers change
# --------------------------------------------------------------------------
def _round(x: Fraction) -> int:
    return math.floor(x + Fraction(1, 2))


def _exact(value: int, factor: Fraction, what: str) -> int:
    x = Fraction(value) * factor
    if x.denominator != 1:
        raise UnsupportedMP4(f"{what}: {value} x {factor} is not an integer; "
                             f"cannot rescale without rounding error")
    return int(x)


def plan_patch(mp4: MP4File, factor: Fraction) -> tuple[List[Edit], List[str]]:
    """Return the list of byte edits and a human-readable strategy per track."""
    if factor <= 0:
        raise ValueError("factor must be positive")
    edits: List[Edit] = []
    notes: List[str] = []

    def put(fld: UIntField, new: int, what: str):
        if new < 0 or new >= 1 << (8 * fld.size):
            raise UnsupportedMP4(f"{what}: new value {new} does not fit in "
                                 f"{fld.size * 8} bits")
        if new != fld.value:
            edits.append(Edit(fld.offset, new.to_bytes(fld.size, "big"), what))

    put(mp4.mvhd_duration, _round(mp4.mvhd_duration.value * factor), "mvhd.duration")

    for t in mp4.tracks:
        tag = f"track {t.track_id} ({t.handler}/{t.codec})"
        put(t.tkhd_duration, _round(t.tkhd_duration.value * factor), f"{tag} tkhd.duration")

        new_ts = Fraction(t.timescale) / factor
        by_timescale = new_ts.denominator == 1 and new_ts >= 1
        if by_timescale:
            # Same sample deltas, smaller tick rate -> every timestamp is
            # stretched by exactly `factor`.  stts/ctts/media_time/mdhd.duration
            # keep their numeric values.
            put(t.mdhd_timescale, int(new_ts), f"{tag} mdhd.timescale")
            notes.append(f"{tag}: mdhd timescale {t.timescale} -> {int(new_ts)}")
        else:
            # Timescale can't be divided exactly -> scale every sample delta.
            put(t.mdhd_duration, _round(t.mdhd_duration.value * factor), f"{tag} mdhd.duration")
            new_stts = [(c, _exact(d, factor, f"{tag} stts delta")) for c, d in t.stts.entries]
            for c, d in new_stts:
                if d >= 1 << 32:
                    raise UnsupportedMP4(f"{tag}: scaled stts delta overflows 32 bits")
            edits.append(Edit(t.stts.entries_offset,
                              b"".join(c.to_bytes(4, "big") + d.to_bytes(4, "big")
                                       for c, d in new_stts), f"{tag} stts"))
            if t.ctts:
                signed = t.ctts.version == 1
                buf = b""
                for c, o in t.ctts.entries:
                    n = _exact(o, factor, f"{tag} ctts offset")
                    buf += c.to_bytes(4, "big") + n.to_bytes(4, "big", signed=signed)
                edits.append(Edit(t.ctts.entries_offset, buf, f"{tag} ctts"))
            notes.append(f"{tag}: timescale {t.timescale} kept, stts/ctts deltas x{factor}")

        for e in (t.elst or []):
            put(e.segment_duration, _round(e.segment_duration.value * factor),
                f"{tag} elst.segment_duration")
            if not by_timescale and e.media_time != -1:
                nm = _round(e.media_time * factor)
                edits.append(Edit(e.media_time_offset,
                                  nm.to_bytes(e.media_time_size, "big", signed=True),
                                  f"{tag} elst.media_time"))
    return edits, notes


# --------------------------------------------------------------------------
# Patch (copy + in-place edits)
# --------------------------------------------------------------------------
ProgressFn = Callable[[str, float], None]


def unique_output_path(src: Path, out_dir: Path, suffix: str = "_30fps") -> Path:
    p = out_dir / f"{src.stem}{suffix}{src.suffix or '.mp4'}"
    n = 2
    while p.exists():
        p = out_dir / f"{src.stem}{suffix}_{n}{src.suffix or '.mp4'}"
        n += 1
    return p


def patch_file(src, dst, factor: Fraction | int = 2, progress: Optional[ProgressFn] = None,
               cancel: Optional[Callable[[], bool]] = None, validate: bool = True,
               use_ffprobe: bool = True) -> PatchResult:
    from video_analyzer import analyze          # local import avoids a cycle
    t0 = time.perf_counter()
    factor = Fraction(factor)
    src, dst = Path(src).resolve(), Path(dst).resolve()
    if dst == src or (dst.exists() and src.exists() and os.path.samefile(src, dst)):
        raise ValueError("Refusing to overwrite the original video.")
    if dst.exists():
        raise FileExistsError(f"Output already exists: {dst}")
    say = progress or (lambda s, f: None)

    say("Analyzing MP4 structure…", 0.0)
    mp4 = MP4File.open(src)
    before = analyze(src)
    edits, notes = plan_patch(mp4, factor)

    tmp = dst.with_name(dst.stem + ".partial" + dst.suffix)
    try:
        total = mp4.size
        done = 0
        with open(src, "rb") as fi, open(tmp, "wb") as fo:
            while True:
                if cancel and cancel():
                    raise Cancelled()
                buf = fi.read(CHUNK)
                if not buf:
                    break
                fo.write(buf)
                done += len(buf)
                say("Copying streams (no re-encode)…", 0.9 * done / max(total, 1))
            say("Patching timing metadata…", 0.92)
            for e in edits:
                fo.seek(e.offset)
                fo.write(e.data)
        report: List[Check] = []
        if validate:
            say("Validating output…", 0.95)
            report = validate_output(src, tmp, mp4, edits, factor, use_ffprobe)
        os.replace(tmp, dst)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    after = analyze(dst)
    say("Done", 1.0)
    return PatchResult(dst, factor, time.perf_counter() - t0, src.stat().st_size,
                       dst.stat().st_size, len(edits), notes, report, before, after)


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------
def _apply_edits_to_chunk(chunk: bytearray, base: int, edits: List[Edit]):
    end = base + len(chunk)
    for e in edits:
        eend = e.offset + len(e.data)
        if eend <= base or e.offset >= end:
            continue
        s, t = max(e.offset, base), min(eend, end)
        chunk[s - base:t - base] = e.data[s - e.offset:t - e.offset]


def find_ffprobe() -> Optional[str]:
    names = ["ffprobe.exe", "ffprobe"] if os.name == "nt" else ["ffprobe"]
    dirs = [Path(sys.executable).parent, Path(__file__).resolve().parent]
    if getattr(sys, "_MEIPASS", None):
        dirs.insert(0, Path(sys._MEIPASS))
    for d in dirs:
        for n in names:
            if (d / n).is_file():
                return str(d / n)
    return shutil.which("ffprobe")


def ffprobe_json(path) -> Optional[dict]:
    exe = find_ffprobe()
    if not exe:
        return None
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        r = subprocess.run([exe, "-v", "error", "-show_streams", "-show_format",
                            "-of", "json", str(path)], capture_output=True, text=True,
                           timeout=120, creationflags=flags)
        return json.loads(r.stdout) if r.returncode == 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def _frac(s: str) -> Fraction:
    try:
        return Fraction(s)
    except (ValueError, ZeroDivisionError):
        return Fraction(0)


def validate_output(src: Path, out: Path, before: MP4File, edits: List[Edit],
                    factor: Fraction, use_ffprobe: bool = True) -> List[Check]:
    checks: List[Check] = []

    def add(name, ok, detail=""):
        checks.append(Check(name, bool(ok), detail))

    # 1. structure: re-parse the whole box tree of the output
    try:
        after = MP4File.open(out)
        add("MP4 box structure valid", True)
    except MP4Error as e:
        raise ValidationError(f"Output failed structural validation: {e}")

    # 2. output == input + exactly the planned edits (=> samples untouched)
    if after.size != before.size:
        raise ValidationError("Output size differs from input size")
    ordered = sorted(edits, key=lambda e: e.offset)
    with open(src, "rb") as a, open(out, "rb") as b:
        pos = 0
        while True:
            ca = bytearray(a.read(CHUNK))
            cb = b.read(CHUNK)
            if not ca and not cb:
                break
            _apply_edits_to_chunk(ca, pos, ordered)
            if bytes(ca) != cb:
                raise ValidationError(f"Unexpected byte differences near offset {pos}")
            pos += len(ca)
    add("Encoded video/audio samples byte-identical (no re-encode)", True,
        f"{len(edits)} metadata fields changed, everything else identical")

    # 3. per-track timing
    for tb, ta in zip(before.tracks, after.tracks):
        tag = f"track {tb.track_id} ({tb.handler})"
        if tb.sample_count != ta.sample_count or tb.stts.sample_count != ta.stts.sample_count:
            raise ValidationError(f"{tag}: sample count changed")
        add(f"{tag}: frame/sample count unchanged", True, str(ta.sample_count))
        want = tb.media_seconds * factor
        if ta.media_seconds != want:
            raise ValidationError(f"{tag}: duration {float(ta.media_seconds):.4f}s, "
                                  f"expected {float(want):.4f}s")
        add(f"{tag}: duration x{factor}", True,
            f"{float(tb.media_seconds):.3f}s -> {float(ta.media_seconds):.3f}s")
        if tb.handler == "vide" and tb.fps:
            if ta.fps != tb.fps / factor:
                raise ValidationError(f"{tag}: fps {float(ta.fps):.3f}, "
                                      f"expected {float(tb.fps / factor):.3f}")
            add(f"{tag}: frame rate / {factor}", True,
                f"{float(tb.fps):.3f} -> {float(ta.fps):.3f} fps")
    # 4. A/V sync: every track scaled by the same exact factor
    ratios = {ta.media_seconds / tb.media_seconds for tb, ta in zip(before.tracks, after.tracks)
              if tb.media_seconds}
    add("Audio/video synchronization preserved (identical scale on all tracks)",
        len(ratios) <= 1, f"ratio={ratios}")
    if len(ratios) > 1:
        raise ValidationError("Tracks were scaled by different ratios")

    # 5. independent check with ffprobe
    if use_ffprobe:
        j = ffprobe_json(out)
        if j is None:
            add("FFprobe independent check", True, "skipped (ffprobe not available)")
        else:
            for tb, ta in zip(before.tracks, after.tracks):
                s = next((x for x in j["streams"] if x.get("index") == ta.index), None)
                if not s:
                    continue
                tag = f"ffprobe stream {ta.index} ({s.get('codec_type')})"
                try:
                    dur = float(s.get("duration", "nan"))
                except ValueError:
                    dur = float("nan")
                # ffprobe reports the *presented* duration (after edit-list trimming of
                # AAC priming), which is what tkhd.duration describes.
                exp = ta.tkhd_duration.value / after.mvhd_timescale.value
                if exp <= 0:
                    exp = float(ta.media_seconds)
                if not math.isnan(dur) and abs(dur - exp) > 0.05:
                    raise ValidationError(f"{tag}: ffprobe duration {dur} != {exp:.3f}")
                if ta.handler == "vide":
                    nb = s.get("nb_frames")
                    if nb and int(nb) != ta.sample_count:
                        raise ValidationError(f"{tag}: ffprobe frames {nb} != {ta.sample_count}")
                    rf = _frac(s.get("avg_frame_rate", "0/1"))
                    if rf and abs(float(rf) - float(ta.fps)) > 0.01 * float(ta.fps):
                        raise ValidationError(f"{tag}: ffprobe fps {float(rf):.3f} "
                                              f"!= {float(ta.fps):.3f}")
                add(tag, True, f"duration={dur:.3f}s codec={s.get('codec_name')}")
    return checks
