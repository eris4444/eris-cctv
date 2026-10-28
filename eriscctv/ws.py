"""Minimal RFC 6455 WebSocket implementation on asyncio streams (server and client side)."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import ssl as ssl_mod
import struct
import time

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_CONT, OP_TEXT, OP_BIN, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


class WebSocketClosed(Exception):
    pass


class HandshakeError(Exception):
    pass


def accept_key(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()


def _mask(data: bytes, mask: bytes) -> bytes:
    n = len(data)
    if not n:
        return b""
    m = int.from_bytes((mask * (n // 4 + 1))[:n], "big")
    return (int.from_bytes(data, "big") ^ m).to_bytes(n, "big")


class WebSocket:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, is_client: bool,
                 max_size: int = 64 * 1024 * 1024):
        self.reader = reader
        self.writer = writer
        self.is_client = is_client
        self.max_size = max_size
        self.closed = False
        self.close_code = None
        self.last_recv = time.monotonic()
        self.bytes_out = 0
        self.bytes_in = 0
        self._wlock = asyncio.Lock()

    @property
    def peer(self):
        try:
            return self.writer.get_extra_info("peername")
        except Exception:
            return None

    async def _read_frame(self):
        head = await self.reader.readexactly(2)
        fin = bool(head[0] & 0x80)
        op = head[0] & 0x0F
        masked = bool(head[1] & 0x80)
        n = head[1] & 0x7F
        if n == 126:
            n = struct.unpack(">H", await self.reader.readexactly(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", await self.reader.readexactly(8))[0]
        if n > self.max_size:
            raise WebSocketClosed("frame too large")
        mask = await self.reader.readexactly(4) if masked else None
        data = await self.reader.readexactly(n) if n else b""
        if mask:
            data = _mask(data, mask)
        self.last_recv = time.monotonic()
        self.bytes_in += n
        return fin, op, data

    async def recv(self):
        """Next text (str) or binary (bytes) message. Control frames are handled here."""
        parts = []
        msg_op = None
        size = 0
        while True:
            if self.closed:
                raise WebSocketClosed("closed")
            try:
                fin, op, data = await self._read_frame()
            except (asyncio.IncompleteReadError, ConnectionError, OSError, ssl_mod.SSLError) as e:
                self.closed = True
                raise WebSocketClosed(str(e) or "connection lost")
            if op == OP_PING:
                try:
                    await self._send(OP_PONG, data[:125])
                except WebSocketClosed:
                    pass
                continue
            if op == OP_PONG:
                continue
            if op == OP_CLOSE:
                self.close_code = struct.unpack(">H", data[:2])[0] if len(data) >= 2 else 1005
                if not self.closed:
                    try:
                        await self._send(OP_CLOSE, data[:2])
                    except WebSocketClosed:
                        pass
                self.closed = True
                self._shutdown()
                raise WebSocketClosed("closed by peer (%s)" % self.close_code)
            if op in (OP_TEXT, OP_BIN):
                if msg_op is not None:
                    raise WebSocketClosed("protocol error")
                msg_op = op
            elif op == OP_CONT:
                if msg_op is None:
                    raise WebSocketClosed("protocol error")
            else:
                raise WebSocketClosed("unknown opcode %d" % op)
            parts.append(data)
            size += len(data)
            if size > self.max_size:
                raise WebSocketClosed("message too large")
            if fin:
                payload = parts[0] if len(parts) == 1 else b"".join(parts)
                if msg_op == OP_TEXT:
                    return payload.decode("utf-8", "replace")
                return payload

    async def _send(self, op: int, data: bytes) -> None:
        if self.closed and op != OP_CLOSE:
            raise WebSocketClosed("closed")
        n = len(data)
        b0 = 0x80 | op
        mbit = 0x80 if self.is_client else 0
        if n < 126:
            header = struct.pack(">BB", b0, mbit | n)
        elif n < 65536:
            header = struct.pack(">BBH", b0, mbit | 126, n)
        else:
            header = struct.pack(">BBQ", b0, mbit | 127, n)
        if self.is_client:
            mask = os.urandom(4)
            header += mask
            data = _mask(data, mask)
        async with self._wlock:
            try:
                self.writer.write(header)
                if data:
                    self.writer.write(data)
                await self.writer.drain()
            except (ConnectionError, OSError, RuntimeError, ssl_mod.SSLError) as e:
                self.closed = True
                raise WebSocketClosed(str(e) or "connection lost")
        self.bytes_out += n

    async def send_text(self, text: str) -> None:
        await self._send(OP_TEXT, text.encode("utf-8"))

    async def send_json(self, obj) -> None:
        await self.send_text(json.dumps(obj, separators=(",", ":")))

    async def send_bytes(self, data: bytes) -> None:
        await self._send(OP_BIN, data)

    async def ping(self) -> None:
        await self._send(OP_PING, b"")

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if self.closed:
            self._shutdown()
            return
        self.closed = True
        try:
            await asyncio.wait_for(self._send(OP_CLOSE, struct.pack(">H", code) + reason.encode()[:120]), 3)
        except Exception:
            pass
        self._shutdown()

    def _shutdown(self) -> None:
        try:
            self.writer.close()
        except Exception:
            pass

    def abort(self) -> None:
        self.closed = True
        try:
            transport = self.writer.transport
            transport.abort()
        except Exception:
            self._shutdown()


async def connect(host: str, port: int, path: str, ssl_ctx=None, timeout: float = 15,
                  headers: dict = None) -> WebSocket:
    """Open a client WebSocket (ws:// or, with ssl_ctx, wss://)."""
    kwargs = {}
    if ssl_ctx is not None:
        kwargs["ssl"] = ssl_ctx
        kwargs["server_hostname"] = host
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port, limit=65536, **kwargs), timeout)
    try:
        key = base64.b64encode(os.urandom(16)).decode()
        default_port = 443 if ssl_ctx is not None else 80
        host_header = host if port == default_port else "%s:%d" % (
            "[%s]" % host if ":" in host else host, port)
        lines = ["GET %s HTTP/1.1" % path, "Host: " + host_header, "Upgrade: websocket",
                 "Connection: Upgrade", "Sec-WebSocket-Key: " + key, "Sec-WebSocket-Version: 13"]
        for k, v in (headers or {}).items():
            lines.append("%s: %s" % (k, v))
        writer.write(("\r\n".join(lines) + "\r\n\r\n").encode())
        await writer.drain()
        raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout)
        head = raw.decode("latin-1").split("\r\n")
        status = head[0].split(" ", 2)
        if len(status) < 2 or status[1] != "101":
            raise HandshakeError("server answered: " + head[0][:100])
        hdrs = {}
        for line in head[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                hdrs[k.strip().lower()] = v.strip()
        if hdrs.get("sec-websocket-accept") != accept_key(key):
            raise HandshakeError("bad handshake from server")
    except BaseException:
        writer.close()
        raise
    return WebSocket(reader, writer, is_client=True)
