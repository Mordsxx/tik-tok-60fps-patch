"""Read-only video information, taken straight from the MP4 sample tables."""
from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import List, Optional

from mp4_parser import MP4File

CODEC_NAMES = {
    "hvc1": "HEVC / H.265", "hev1": "HEVC / H.265",
    "avc1": "AVC / H.264", "avc3": "AVC / H.264",
    "mp4a": "AAC", "ac-3": "AC-3", "ec-3": "E-AC-3", "Opus": "Opus",
    "av01": "AV1", "vp09": "VP9",
}


@dataclass
class VideoInfo:
    path: Path
    file_size: int
    width: int = 0
    height: int = 0
    fps: Optional[Fraction] = None
    is_vfr: bool = False
    duration: float = 0.0           # seconds (video track if present)
    video_codec: str = "—"
    audio_codec: str = "—"
    frame_count: Optional[int] = None
    track_count: int = 0
    warnings: List[str] = field(default_factory=list)

    @property
    def fps_text(self) -> str:
        if not self.fps:
            return "—"
        s = f"{float(self.fps):.3f}".rstrip("0").rstrip(".")
        return s + (" (variable, average)" if self.is_vfr else "")


def codec_name(fourcc: str) -> str:
    return CODEC_NAMES.get(fourcc, fourcc)


def analyze(path) -> VideoInfo:
    path = Path(path)
    mp4 = MP4File.open(path)
    v, a = mp4.first("vide"), mp4.first("soun")
    info = VideoInfo(path, mp4.size, track_count=len(mp4.tracks))
    if v:
        info.width, info.height = v.width, v.height
        info.fps, info.is_vfr = v.fps, v.is_vfr
        info.duration = float(v.media_seconds)
        info.video_codec = codec_name(v.codec)
        info.frame_count = v.sample_count
        if v.is_vfr:
            info.warnings.append("Variable frame rate: all timestamps will be scaled "
                                 "uniformly, so VFR pacing is preserved.")
    else:
        info.warnings.append("No video track found.")
        info.duration = max((float(t.media_seconds) for t in mp4.tracks), default=0.0)
    if a:
        info.audio_codec = codec_name(a.codec)
        if a.codec != "mp4a":
            info.warnings.append(f"Audio codec '{a.codec}' is not AAC; timing is still "
                                 f"patched, but playback is less tested.")
    return info
