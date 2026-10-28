"""Fragmented MP4 helpers: split ffmpeg's output into init + fragments and read codec info."""
from __future__ import annotations

import struct

CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts", b"mvex", b"udta"}


class Mp4Splitter:
    """Feeds raw fMP4 bytes, returns ('init', bytes) and ('frag', bytes) items.

    ffmpeg with -movflags frag_keyframe+empty_moov+default_base_moof writes ftyp+moov once, then
    moof+mdat pairs; each fragment starts with a video keyframe.
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
