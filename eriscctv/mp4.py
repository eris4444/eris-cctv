"""Fragmented MP4 helpers: split ffmpeg's output into init + fragments and read codec info."""
from __future__ import annotations

import struct

CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts", b"mvex", b"udta"}


class Mp4Splitter:
    """Feeds raw fMP4 bytes, returns ('init', bytes) and ('frag', bytes) items.

    ffmpeg with -movflags frag_keyframe+empty_moov+default_base_moof (+ -frag_duration) writes
    ftyp+moov once, then moof+mdat pairs; only some fragments start with a keyframe - see KeyframeDetector.
    """

    def __init__(self):
        self.buf = bytearray()
        self.init_parts = []
        self.frag = None

    def feed(self, data: bytes) -> list:
        self.buf += data
        out = []
        buf = self.buf
        while len(buf) >= 8:
            size = struct.unpack_from(">I", buf, 0)[0]
            btype = bytes(buf[4:8])
            if size == 1:
                if len(buf) < 16:
                    break
                size = struct.unpack_from(">Q", buf, 8)[0]
            elif size == 0 or size < 8:
                raise ValueError("unsupported MP4 box size in stream")
            if len(buf) < size:
                break
            box = bytes(buf[:size])
            del buf[:size]
            if btype == b"ftyp":
                self.init_parts = [box]
                self.frag = None
            elif btype == b"moov":
                self.init_parts.append(box)
                out.append(("init", b"".join(self.init_parts)))
                self.init_parts = []
            elif btype == b"moof":
                self.frag = [box]
            elif btype == b"mdat":
                if self.frag is not None:
                    self.frag.append(box)
                    out.append(("frag", b"".join(self.frag)))
                    self.frag = None
            elif self.frag is not None:
                self.frag.append(box)
        return out


def iter_boxes(data: bytes, start: int = 0, end: int = None):
    end = len(data) if end is None else end
    pos = start
    while pos + 8 <= end:
        size, btype = struct.unpack_from(">I4s", data, pos)
        header = 8
        if size == 1:
            size = struct.unpack_from(">Q", data, pos + 8)[0]
            header = 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            return
        yield btype, pos + header, pos + size
        pos += size


def _sample_entries(data: bytes, start: int, end: int):
    """Yields (type, payload_start, end) for every stsd sample entry below a box range."""
    for btype, ps, pe in iter_boxes(data, start, end):
        if btype in CONTAINERS:
            yield from _sample_entries(data, ps, pe)
        elif btype == b"stsd":
            yield from iter_boxes(data, ps + 8, pe)


NON_SYNC = 0x00010000  # sample_is_non_sync_sample in ISO BMFF sample flags


class KeyframeDetector:
    """Tells whether a fragment starts with a video keyframe (a point a decoder can start from).

    Fragments may be dropped (slow link, slow viewer) only up to the next keyframe fragment, and new
    viewers and recordings must start at one.
    """

    def __init__(self, init: bytes):
        self.track = 0
        self.default_flags = None
        try:
            self._read_init(init)
        except (struct.error, IndexError):
            self.track = 0

    def _read_init(self, init: bytes) -> None:
        trex = {}
        for btype, ps, pe in iter_boxes(init):
            if btype != b"moov":
                continue
            for t, tps, tpe in iter_boxes(init, ps, pe):
                if t == b"trak":
                    track, handler = self._trak(init, tps, tpe)
                    if handler == b"vide" and not self.track:
                        self.track = track
                elif t == b"mvex":
                    for x, xps, _xpe in iter_boxes(init, tps, tpe):
                        if x == b"trex":
                            trex[struct.unpack_from(">I", init, xps + 4)[0]] = \
                                struct.unpack_from(">I", init, xps + 20)[0]
        self.default_flags = trex.get(self.track)

    @staticmethod
    def _trak(data: bytes, start: int, end: int):
        track, handler = 0, b""
        for t, ps, pe in iter_boxes(data, start, end):
            if t == b"tkhd":
                track = struct.unpack_from(">I", data, ps + (20 if data[ps] == 1 else 12))[0]
            elif t == b"mdia":
                for m, mps, _mpe in iter_boxes(data, ps, pe):
                    if m == b"hdlr":
                        handler = bytes(data[mps + 8:mps + 12])
        return track, handler

    def is_key(self, frag: bytes) -> bool:
        """True when unsure, so a parsing problem never blocks the stream."""
        if not self.track:
            return True
        try:
            flags = self._first_sample_flags(frag)
        except (struct.error, IndexError):
            return True
        return flags is None or not flags & NON_SYNC

    def _first_sample_flags(self, frag: bytes):
        for btype, ps, pe in iter_boxes(frag):
            if btype != b"moof":
                continue
            for t, tps, tpe in iter_boxes(frag, ps, pe):
                if t != b"traf":
                    continue
                default, trun = self.default_flags, None
                track = None
                for b, bps, _bpe in iter_boxes(frag, tps, tpe):
                    if b == b"tfhd":
                        tf = struct.unpack_from(">I", frag, bps)[0] & 0xFFFFFF
                        track = struct.unpack_from(">I", frag, bps + 4)[0]
                        pos = bps + 8
                        pos += 8 if tf & 0x01 else 0
                        pos += 4 if tf & 0x02 else 0
                        pos += 4 if tf & 0x08 else 0
                        pos += 4 if tf & 0x10 else 0
                        if tf & 0x20:
                            default = struct.unpack_from(">I", frag, pos)[0]
                    elif b == b"trun" and trun is None:
                        trun = bps
                if track != self.track or trun is None:
                    continue
                tr = struct.unpack_from(">I", frag, trun)[0] & 0xFFFFFF
                pos = trun + 8 + (4 if tr & 0x01 else 0)
                if tr & 0x04:
                    return struct.unpack_from(">I", frag, pos)[0]
                if tr & 0x400:
                    pos += 4 if tr & 0x100 else 0
                    pos += 4 if tr & 0x200 else 0
                    return struct.unpack_from(">I", frag, pos)[0]
                return default
        return None


def codec_info(init: bytes) -> dict:
    """Codec string (for MediaSource) and picture size from an init segment."""
    info = {"video": "", "audio": "", "width": 0, "height": 0}
    for btype, ps, pe in iter_boxes(init):
        if btype != b"moov":
            continue
        for etype, eps, epe in _sample_entries(init, ps, pe):
            if etype in (b"avc1", b"avc3") and not info["video"]:
                info["width"], info["height"] = struct.unpack_from(">HH", init, eps + 24)
                for ctype, cps, _cpe in iter_boxes(init, eps + 78, epe):
                    if ctype == b"avcC":
                        prof, compat, level = init[cps + 1], init[cps + 2], init[cps + 3]
                        info["video"] = "%s.%02x%02x%02x" % (etype.decode(), prof, compat, level)
            elif etype == b"mp4a" and not info["audio"]:
                info["audio"] = "mp4a.40.2"
    codecs = [c for c in (info["video"], info["audio"]) if c]
    info["codec"] = ",".join(codecs)
    return info
