"""Compare two MP4s (e.g. your patched output vs. the reference app's output).

    python tools/compare.py mine.mp4 reference.mp4
Prints the timing properties side by side and the MP4 timing boxes that differ.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mp4_parser import MP4File


def rows(p):
    m = MP4File.open(p)
    out = {"mvhd.timescale": m.mvhd_timescale.value, "mvhd.duration": m.mvhd_duration.value}
    for t in m.tracks:
        k = f"{t.handler}[{t.track_id}]"
        out |= {f"{k}.codec": t.codec, f"{k}.mdhd.timescale": t.timescale,
                f"{k}.mdhd.duration": t.mdhd_duration.value, f"{k}.tkhd.duration": t.tkhd_duration.value,
                f"{k}.samples": t.sample_count, f"{k}.stts": t.stts.entries[:3],
                f"{k}.elst": [(e.segment_duration.value, e.media_time) for e in (t.elst or [])],
                f"{k}.seconds": round(float(t.media_seconds), 4),
                f"{k}.fps": round(float(t.fps), 4)}
    return out


a, b = rows(sys.argv[1]), rows(sys.argv[2])
for k in sorted(set(a) | set(b)):
    flag = "" if a.get(k) == b.get(k) else "   <-- differs"
    print(f"{k:28} {str(a.get(k)):28} {str(b.get(k)):28}{flag}")
