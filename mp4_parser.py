"""Minimal ISO-BMFF / MP4 box parser.

Only structure and timing metadata are read.  Sample data (``mdat``) is never
loaded: every box is located by file offset, so multi-GB files cost no RAM.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import BinaryIO, List, Optional


class MP4Error(Exception):
    """The file is not a structurally valid MP4."""


class UnsupportedMP4(MP4Error):
    """The file is valid but cannot be patched safely."""


CONTAINER_TYPES = {"moov", "trak", "mdia", "minf", "stbl", "edts", "dinf",
                   "mvex", "moof", "traf"}
_MAX_DEPTH = 12


# --------------------------------------------------------------------------
# Generic box tree
# --------------------------------------------------------------------------
@dataclass
class Box:
    type: str
    offset: int          # file offset of the box header
    size: int            # total size incl. header
    header_size: int     # 8, 16 (64-bit size) (+16 for uuid)
    parent: Optional["Box"] = None
    children: List["Box"] = field(default_factory=list)

    @property
    def payload_offset(self) -> int:
        return self.offset + self.header_size

    @property
    def payload_size(self) -> int:
        return self.size - self.header_size

    @property
    def end(self) -> int:
        return self.offset + self.size

    def child(self, box_type: str) -> Optional["Box"]:
        for c in self.children:
            if c.type == box_type:
                return c
        return None

    def children_of(self, box_type: str) -> List["Box"]:
        return [c for c in self.children if c.type == box_type]


def _read(f: BinaryIO, pos: int, n: int) -> bytes:
    f.seek(pos)
    data = f.read(n)
    if len(data) != n:
        raise MP4Error(f"unexpected end of file at offset {pos}")
    return data


def _u(data: bytes, signed: bool = False) -> int:
    return int.from_bytes(data, "big", signed=signed)


def parse_boxes(f: BinaryIO, start: int, end: int,
                parent: Optional[Box] = None, depth: int = 0) -> List[Box]:
    """Parse sibling boxes in [start, end); recurse into known containers."""
    if depth > _MAX_DEPTH:
        raise MP4Error("box nesting too deep")
    boxes: List[Box] = []
    pos = start
    while pos < end:
        if end - pos < 8:
            if parent is None:      # trailing padding after the last box
                break
            raise MP4Error(f"truncated box header at offset {pos}")
        head = _read(f, pos, 8)
        size = _u(head[:4])
        btype = head[4:8].decode("latin-1")
        hs = 8
        if size == 1:                       # 64-bit largesize
            size = _u(_read(f, pos + 8, 8))
            hs = 16
        elif size == 0:                     # box extends to end of container
            size = end - pos
        if btype == "uuid":
            hs += 16
        if size < hs or pos + size > end:
            raise MP4Error(f"box '{btype}' at offset {pos} has invalid size {size} "
                           f"(file truncated or corrupt)")
        box = Box(btype, pos, size, hs, parent)
        if btype in CONTAINER_TYPES:
            box.children = parse_boxes(f, box.payload_offset, box.end, box, depth + 1)
        boxes.append(box)
        pos += size
    return boxes


# --------------------------------------------------------------------------
# Typed views of the timing boxes (each remembers where its fields live)
# --------------------------------------------------------------------------
@dataclass
class UIntField:
    offset: int
    size: int      # bytes (4 or 8)
    value: int


def _field(f, pos, size) -> UIntField:
    return UIntField(pos, size, _u(_read(f, pos, size)))


def _need(box: Box, n: int):
    if box.payload_size < n:
        raise MP4Error(f"box '{box.type}' at {box.offset} is too short")


def parse_mvhd(f, box: Box):
    """-> (timescale, duration) fields.  v0: 32-bit, v1: 64-bit duration."""
    _need(box, 20)
    ver = _read(f, box.payload_offset, 1)[0]
    base = box.payload_offset + 4
    if ver == 0:
        _need(box, 20)
        return _field(f, base + 8, 4), _field(f, base + 12, 4)
    if ver == 1:
        _need(box, 32)
        return _field(f, base + 16, 4), _field(f, base + 20, 8)
    raise UnsupportedMP4(f"mvhd version {ver} is not supported")


def parse_mdhd(f, box: Box):
    return parse_mvhd(f, box)           # identical layout


def parse_tkhd(f, box: Box):
    """-> (track_id, duration) fields."""
    ver = _read(f, box.payload_offset, 1)[0]
    base = box.payload_offset + 4
    if ver == 0:
        _need(box, 24)
        return _field(f, base + 8, 4), _field(f, base + 16, 4)
    if ver == 1:
        _need(box, 36)
        return _field(f, base + 16, 4), _field(f, base + 24, 8)
    raise UnsupportedMP4(f"tkhd version {ver} is not supported")


@dataclass
class ElstEntry:
    segment_duration: UIntField      # movie timescale
    media_time_offset: int           # media timescale, -1 = empty edit
    media_time_size: int
    media_time: int


def parse_elst(f, box: Box) -> List[ElstEntry]:
    _need(box, 8)
    ver = _read(f, box.payload_offset, 1)[0]
    if ver not in (0, 1):
        raise UnsupportedMP4(f"elst version {ver} is not supported")
    count = _u(_read(f, box.payload_offset + 4, 4))
    seg_sz, stride = (4, 12) if ver == 0 else (8, 20)
    _need(box, 8 + count * stride)
    out = []
    pos = box.payload_offset + 8
    for _ in range(count):
        seg = _field(f, pos, seg_sz)
        mt = _u(_read(f, pos + seg_sz, seg_sz), signed=True)
        out.append(ElstEntry(seg, pos + seg_sz, seg_sz, mt))
        pos += stride
    return out


@dataclass
class SttsInfo:
    entries_offset: int
    entries: List[tuple]             # (sample_count, delta)

    @property
    def sample_count(self) -> int:
        return sum(c for c, _ in self.entries)

    @property
    def total_ticks(self) -> int:
        return sum(c * d for c, d in self.entries)


def parse_stts(f, box: Box) -> SttsInfo:
    _need(box, 8)
    count = _u(_read(f, box.payload_offset + 4, 4))
    _need(box, 8 + count * 8)
    raw = _read(f, box.payload_offset + 8, count * 8)
    entries = [(_u(raw[i:i + 4]), _u(raw[i + 4:i + 8])) for i in range(0, len(raw), 8)]
    return SttsInfo(box.payload_offset + 8, entries)


@dataclass
class CttsInfo:
    version: int
    entries_offset: int
    entries: List[tuple]             # (sample_count, offset)


def parse_ctts(f, box: Box) -> CttsInfo:
    _need(box, 8)
    ver = _read(f, box.payload_offset, 1)[0]
    if ver not in (0, 1):
        raise UnsupportedMP4(f"ctts version {ver} is not supported")
    count = _u(_read(f, box.payload_offset + 4, 4))
    _need(box, 8 + count * 8)
    raw = _read(f, box.payload_offset + 8, count * 8)
    entries = [(_u(raw[i:i + 4]), _u(raw[i + 4:i + 8], signed=(ver == 1)))
               for i in range(0, len(raw), 8)]
    return CttsInfo(ver, box.payload_offset + 8, entries)


# --------------------------------------------------------------------------
# Whole-file model
# --------------------------------------------------------------------------
@dataclass
class TrackInfo:
    index: int
    track_id: int
    handler: str                     # 'vide', 'soun', ...
    codec: str                       # sample-entry fourcc
    tkhd_duration: UIntField         # movie timescale
    mdhd_timescale: UIntField
    mdhd_duration: UIntField         # media timescale
    stts: SttsInfo
    ctts: Optional[CttsInfo]
    elst: Optional[List[ElstEntry]]
    sample_count: int
    width: int = 0
    height: int = 0
    sample_rate: int = 0

    @property
    def timescale(self) -> int:
        return self.mdhd_timescale.value

    @property
    def media_seconds(self) -> Fraction:
        """Exact media duration, derived from the sample table."""
        return Fraction(self.stts.total_ticks, self.timescale)

    @property
    def is_vfr(self) -> bool:
        return len({d for _, d in self.stts.entries if _ > 0}) > 1

    @property
    def fps(self) -> Fraction:
        ents = [(c, d) for c, d in self.stts.entries if c > 0]
        if not ents:
            return Fraction(0)
        if len({d for _, d in ents}) == 1 and ents[0][1] > 0:
            return Fraction(self.timescale, ents[0][1])
        ticks = self.stts.total_ticks
        return Fraction(self.sample_count * self.timescale, ticks) if ticks else Fraction(0)


class MP4File:
    def __init__(self, path: Path, size: int, boxes: List[Box]):
        self.path = path
        self.size = size
        self.boxes = boxes
        self.moov: Box
        self.mvhd_timescale: UIntField
        self.mvhd_duration: UIntField
        self.tracks: List[TrackInfo] = []

    @classmethod
    def open(cls, path) -> "MP4File":
        path = Path(path)
        size = path.stat().st_size
        with open(path, "rb") as f:
            boxes = parse_boxes(f, 0, size)
            if not boxes:
                raise MP4Error("not an MP4 file (no boxes found)")
            self = cls(path, size, boxes)
            self._load(f)
        return self

    # ----------------------------------------------------------------
    def _load(self, f):
        types = [b.type for b in self.boxes]
        if "moov" not in types:
            raise MP4Error("no 'moov' box: not an MP4/MOV or the file is incomplete")
        if types.count("moov") > 1:
            raise UnsupportedMP4("multiple 'moov' boxes")
        if "moof" in types or self.moov_child_exists("mvex"):
            raise UnsupportedMP4("fragmented MP4 (moof/mvex) is not supported")
        moov = next(b for b in self.boxes if b.type == "moov")
        self.moov = moov
        mvhd = moov.child("mvhd")
        if mvhd is None:
            raise MP4Error("missing 'mvhd'")
        self.mvhd_timescale, self.mvhd_duration = parse_mvhd(f, mvhd)
        if self.mvhd_timescale.value == 0:
            raise MP4Error("mvhd timescale is zero")
        for i, trak in enumerate(moov.children_of("trak")):
            self.tracks.append(self._load_track(f, i, trak))
        if not self.tracks:
            raise MP4Error("no tracks found")

    def moov_child_exists(self, t: str) -> bool:
        for b in self.boxes:
            if b.type == "moov" and b.child(t) is not None:
                return True
        return False

    def _load_track(self, f, index: int, trak: Box) -> TrackInfo:
        def need(parent: Optional[Box], t: str) -> Box:
            b = parent.child(t) if parent else None
            if b is None:
                raise MP4Error(f"track {index}: missing '{t}' box")
            return b
        tkhd = need(trak, "tkhd")
        mdia = need(trak, "mdia")
        mdhd = need(mdia, "mdhd")
        hdlr = need(mdia, "hdlr")
        minf = need(mdia, "minf")
        stbl = need(minf, "stbl")
        stts_b = need(stbl, "stts")
        stsd = need(stbl, "stsd")
        tid, tdur = parse_tkhd(f, tkhd)
        ts, mdur = parse_mdhd(f, mdhd)
        if ts.value == 0:
            raise MP4Error(f"track {index}: mdhd timescale is zero")
        handler = _read(f, hdlr.payload_offset + 8, 4).decode("latin-1")
        stts = parse_stts(f, stts_b)
        ctts_b = stbl.child("ctts")
        ctts = parse_ctts(f, ctts_b) if ctts_b else None
        elst_b = trak.child("edts").child("elst") if trak.child("edts") else None
        elst = parse_elst(f, elst_b) if elst_b else None

        sc = 0
        for t in ("stsz", "stz2"):
            b = stbl.child(t)
            if b:
                _need(b, 12)
                sc = _u(_read(f, b.payload_offset + 8, 4))
                break
        else:
            raise MP4Error(f"track {index}: missing 'stsz'")

        _need(stsd, 8)
        codec = _read(f, stsd.payload_offset + 12, 4).decode("latin-1") \
            if stsd.payload_size >= 16 else "?"
        info = TrackInfo(index, tid.value, handler, codec, tdur, ts, mdur,
                         stts, ctts, elst, sc)
        entry = stsd.payload_offset + 8
        if stsd.payload_size >= 8 + 36:
            if handler == "vide":
                info.width = _u(_read(f, entry + 32, 2))
                info.height = _u(_read(f, entry + 34, 2))
            elif handler == "soun":
                info.sample_rate = _u(_read(f, entry + 32, 4)) >> 16
        return info

    # convenience --------------------------------------------------------
    def first(self, handler: str) -> Optional[TrackInfo]:
        return next((t for t in self.tracks if t.handler == handler), None)
