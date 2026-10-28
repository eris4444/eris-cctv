"""Small asyncio HTTP/1.1 server with routing, static files, ranges, cookies and WebSocket upgrade."""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import mimetypes
import os
import re
import select
import socket
import ssl as ssl_mod
import time
from email.utils import formatdate
from urllib.parse import parse_qs, unquote, urlsplit

from . import ws as wsmod

log = logging.getLogger("web")

STATUS = {200: "OK", 101: "Switching Protocols", 204: "No Content", 206: "Partial Content",
          301: "Moved Permanently", 302: "Found", 304: "Not Modified", 400: "Bad Request",
          401: "Unauthorized", 403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed",
          409: "Conflict", 411: "Length Required", 413: "Payload Too Large",
          416: "Range Not Satisfiable", 429: "Too Many Requests", 500: "Internal Server Error",
          503: "Service Unavailable"}

TYPES = {".js": "application/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".html": "text/html; charset=utf-8", ".svg": "image/svg+xml", ".json": "application/json",
         ".mp4": "video/mp4", ".png": "image/png", ".ico": "image/x-icon", ".woff2": "font/woff2",
         ".txt": "text/plain; charset=utf-8", ".log": "text/plain; charset=utf-8"}

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": ("default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; "
                                "connect-src 'self' ws: wss:; style-src 'self' 'unsafe-inline'; "
                                "script-src 'self'; frame-ancestors 'none'"),
}

MAX_BODY = 2 * 1024 * 1024


class HttpError(Exception):
    def __init__(self, status: int, message: str = ""):
        super().__init__(message or STATUS.get(status, "Error"))
        self.status = status
        self.message = message or STATUS.get(status, "Error")


class Request:
    def __init__(self, method, target, headers, body, remote, secure):
        self.method = method
        parts = urlsplit(target)
        self.path = unquote(parts.path) or "/"
        self.query = {k: v[0] for k, v in parse_qs(parts.query, keep_blank_values=True).items()}
        self.headers = headers
        self.body = body
        self.remote = remote
        self.secure = secure
        self.params: dict = {}
        self.session = None
        self.session_token = ""

    def json(self):
        if not self.body:
            return {}
        try:
            data = json.loads(self.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise HttpError(400, "Invalid JSON body")
        if not isinstance(data, dict):
            raise HttpError(400, "JSON object expected")
        return data

    @property
    def cookies(self) -> dict:
        out = {}
        for part in self.headers.get("cookie", "").split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                out[k.strip()] = v.strip()
        return out


class Response:
    def __init__(self, body=b"", status: int = 200, content_type: str = "text/plain; charset=utf-8",
                 headers: dict = None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.body = body
        self.status = status
        self.headers = {"Content-Type": content_type}
        if headers:
            self.headers.update(headers)
        self.cookies = []

    def set_cookie(self, name, value, max_age=None, secure=False):
        c = "%s=%s; Path=/; HttpOnly; SameSite=Strict" % (name, value)
        if max_age is not None:
            c += "; Max-Age=%d" % max_age
        if secure:
            c += "; Secure"
        self.cookies.append(c)


def json_response(data, status: int = 200) -> Response:
    return Response(json.dumps(data, ensure_ascii=False), status, "application/json; charset=utf-8",
                    {"Cache-Control": "no-store"})


class FileResponse:
    def __init__(self, path: str, content_type: str = "", download_name: str = "", cache: bool = False):
        self.path = path
        self.content_type = content_type or TYPES.get(os.path.splitext(path)[1].lower()) or \
            mimetypes.guess_type(path)[0] or "application/octet-stream"
        self.download_name = download_name
        self.cache = cache
        self.cookies = []


def _route_regex(pattern: str):
    regex = "^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern) + "$"
    return re.compile(regex)


class HttpServer:
    def __init__(self, name: str = "eris-cctv"):
        self.name = name
        self.routes = []
        self.static_dirs = []
        self.server = None
        self.ssl_ctx = None
        self.on_request = None  # optional hook(req) -> None (sessions etc.)
        self.connections = set()

    # -- routing ---------------------------------------------------------------------
    def route(self, method: str, pattern: str):
        def deco(fn):
            self.routes.append((method.upper(), _route_regex(pattern), fn, "http"))
            return fn
        return deco

    def websocket(self, pattern: str):
        def deco(fn):
            self.routes.append(("GET", _route_regex(pattern), fn, "ws"))
            return fn
        return deco

    def static(self, prefix: str, directory: str):
        self.static_dirs.append((prefix.rstrip("/") + "/", os.path.abspath(directory)))

    # -- lifecycle -------------------------------------------------------------------
    async def start(self, host: str, port: int, ssl_ctx=None, also_plain: bool = False):
        """Listen on host:port. With ssl_ctx and also_plain, TLS and plain HTTP share the one port."""
        self.ssl_ctx = ssl_ctx
        if ssl_ctx is not None and also_plain:
            self.server = DualServer(self._handle, host, port, ssl_ctx)
            self.server.start()
        else:
            self.server = await asyncio.start_server(self._handle, host or None, port, ssl=ssl_ctx,
                                                     limit=65536, reuse_address=True)
        return self.server

    async def stop(self):
        if self.server:
            self.server.close()
            for w in list(self.connections):
                try:
                    w.close()
                except Exception:
                    pass
            try:
                await asyncio.wait_for(self.server.wait_closed(), 3)
            except (asyncio.TimeoutError, Exception):
                pass

    # -- connection handling ---------------------------------------------------------
    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.connections.add(writer)
        peer = writer.get_extra_info("peername")
        remote = peer[0] if peer else "?"
        secure = writer.get_extra_info("ssl_object") is not None
        try:
            first = True
            while True:
                try:
                    raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 30 if first else 75)
                except (asyncio.TimeoutError, asyncio.IncompleteReadError):
                    break
                except asyncio.LimitOverrunError:
                    await self._write_simple(writer, 413, "Header too large")
                    break
                first = False
                req = self._parse_head(raw, remote, secure)
                if req is None:
                    await self._write_simple(writer, 400, "Bad request")
                    break
                length = req.headers.get("content-length")
                if req.headers.get("transfer-encoding", "").lower() == "chunked":
                    await self._write_simple(writer, 411, "Chunked bodies are not supported")
                    break
                if length:
                    try:
                        n = int(length)
                    except ValueError:
                        await self._write_simple(writer, 400, "Bad Content-Length")
                        break
                    if n > MAX_BODY or n < 0:
                        await self._write_simple(writer, 413, "Body too large")
                        break
                    req.body = await asyncio.wait_for(reader.readexactly(n), 60) if n else b""
                keep = await self._dispatch(req, reader, writer)
                if not keep:
                    break
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.TimeoutError, OSError,
                ssl_mod.SSLError):
            pass
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("connection error")
        finally:
            self.connections.discard(writer)
            try:
                writer.close()
            except Exception:
                pass

    def _parse_head(self, raw: bytes, remote: str, secure: bool):
        try:
            text = raw.decode("latin-1")
            lines = text.split("\r\n")
            method, target, version = lines[0].split(" ", 2)
            if not version.startswith("HTTP/1."):
                return None
            headers = {}
            for line in lines[1:]:
                if not line:
                    continue
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
            req = Request(method.upper(), target, headers, b"", remote, secure)
            req.http10 = version == "HTTP/1.0"
            return req
        except ValueError:
            return None

    def _find(self, req):
        allowed = False
        for method, regex, fn, kind in self.routes:
            m = regex.match(req.path)
            if not m:
                continue
            if method != req.method and not (method == "GET" and req.method == "HEAD"):
                allowed = True
                continue
            req.params = {k: unquote(v) for k, v in m.groupdict().items()}
            return fn, kind
        if allowed:
            raise HttpError(405)
        return None, None

    async def _dispatch(self, req: Request, reader, writer) -> bool:
        keep = req.headers.get("connection", "").lower() != "close" and not getattr(req, "http10", False)
        try:
            if self.on_request:
                self.on_request(req)
            fn, kind = self._find(req)
            if fn is None:
                resp = self._static(req)
                if resp is None:
                    raise HttpError(404, "Not found")
            elif kind == "ws":
                if req.headers.get("upgrade", "").lower() != "websocket" or \
                        "sec-websocket-key" not in req.headers:
                    raise HttpError(400, "WebSocket upgrade expected")
                origin = req.headers.get("origin")
                if origin and not self._same_origin(origin, req.headers.get("host", "")):
                    raise HttpError(403, "Cross-origin WebSocket refused")
                writer.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                              "Connection: Upgrade\r\nSec-WebSocket-Accept: %s\r\n\r\n"
                              % wsmod.accept_key(req.headers["sec-websocket-key"])).encode())
                await writer.drain()
                sock = wsmod.WebSocket(reader, writer, is_client=False)
                try:
                    await fn(req, sock)
                except wsmod.WebSocketClosed:
                    pass
                finally:
                    await sock.close()
                return False
            else:
                result = fn(req)
                if inspect.isawaitable(result):
                    result = await result
                resp = self._coerce(result)
        except HttpError as e:
            resp = json_response({"error": e.message}, e.status)
        except Exception:
            log.exception("handler error for %s %s", req.method, req.path)
            resp = json_response({"error": "Internal server error"}, 500)
        if isinstance(resp, FileResponse):
            return await self._send_file(req, writer, resp, keep)
        await self._write(writer, req, resp, keep)
        return keep

    @staticmethod
    def _same_origin(origin: str, host: str) -> bool:
        try:
            return urlsplit(origin).netloc.lower() == host.lower()
        except ValueError:
            return False

    @staticmethod
    def _coerce(result):
        if isinstance(result, (Response, FileResponse)):
            return result
        if result is None:
            return json_response({"ok": True})
        if isinstance(result, (dict, list)):
            return json_response(result)
        return Response(str(result))

    def _static(self, req: Request):
        if req.method not in ("GET", "HEAD"):
            return None
        for prefix, directory in self.static_dirs:
            path = req.path
            if path + "/" == prefix:
                path = prefix
            if not path.startswith(prefix):
                continue
            rel = path[len(prefix):] or "index.html"
            full = os.path.abspath(os.path.join(directory, rel))
            if not full.startswith(directory + os.sep) and full != directory:
                return None
            if os.path.isdir(full):
                full = os.path.join(full, "index.html")
            if os.path.isfile(full):
                return FileResponse(full, cache=True)
        return None

    async def _write_simple(self, writer, status, text):
        try:
            body = text.encode()
            writer.write(("HTTP/1.1 %d %s\r\nContent-Type: text/plain\r\nContent-Length: %d\r\n"
                          "Connection: close\r\n\r\n" % (status, STATUS.get(status, ""), len(body))).encode()
                         + body)
            await writer.drain()
        except Exception:
            pass

    def _head_bytes(self, status, headers, cookies, keep):
        lines = ["HTTP/1.1 %d %s" % (status, STATUS.get(status, "OK"))]
        h = dict(SECURITY_HEADERS)
        h.update(headers)
        h["Connection"] = "keep-alive" if keep else "close"
        h["Server"] = self.name
        for k, v in h.items():
            lines.append("%s: %s" % (k, v))
        for c in cookies:
            lines.append("Set-Cookie: " + c)
        return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1", "replace")

    async def _write(self, writer, req, resp: Response, keep):
        headers = dict(resp.headers)
        headers["Content-Length"] = str(len(resp.body))
        writer.write(self._head_bytes(resp.status, headers, resp.cookies, keep))
        if req.method != "HEAD" and resp.body:
            writer.write(resp.body)
        await writer.drain()

    async def _send_file(self, req, writer, resp: FileResponse, keep) -> bool:
        try:
            st = os.stat(resp.path)
        except OSError:
            await self._write(writer, req, json_response({"error": "File not found"}, 404), keep)
            return keep
        size = st.st_size
        etag = '"%x-%x"' % (int(st.st_mtime), size)
        headers = {"Content-Type": resp.content_type, "Accept-Ranges": "bytes",
                   "Last-Modified": formatdate(st.st_mtime, usegmt=True)}
        if resp.cache:
            headers["Cache-Control"] = "no-cache"
            headers["ETag"] = etag
            if req.headers.get("if-none-match") == etag:
                writer.write(self._head_bytes(304, headers, resp.cookies, keep))
                await writer.drain()
                return keep
        if resp.download_name:
            safe = re.sub(r'[^\w.\-]', "_", resp.download_name)
            headers["Content-Disposition"] = 'attachment; filename="%s"' % safe
        start, end, status = 0, size - 1, 200
        rng = req.headers.get("range", "")
        m = re.match(r"bytes=(\d*)-(\d*)$", rng.strip())
        if m and size > 0:
            a, b = m.group(1), m.group(2)
            if a == "" and b:
                start = max(0, size - int(b))
            else:
                start = int(a or 0)
                if b:
                    end = min(size - 1, int(b))
            if start > end or start >= size:
                headers["Content-Range"] = "bytes */%d" % size
                headers["Content-Length"] = "0"
                writer.write(self._head_bytes(416, headers, resp.cookies, keep))
                await writer.drain()
                return keep
            status = 206
            headers["Content-Range"] = "bytes %d-%d/%d" % (start, end, size)
        length = max(0, end - start + 1) if size else 0
        headers["Content-Length"] = str(length)
        writer.write(self._head_bytes(status, headers, resp.cookies, keep))
        if req.method == "HEAD" or length == 0:
            await writer.drain()
            return keep
        loop = asyncio.get_running_loop()
        with open(resp.path, "rb") as f:
            f.seek(start)
            left = length
            while left > 0:
                chunk = await loop.run_in_executor(None, f.read, min(256 * 1024, left))
                if not chunk:
                    break
                writer.write(chunk)
                left -= len(chunk)
                await writer.drain()
        return keep


class DualServer:
    """One listening port for TLS and plain HTTP: a connection that starts with 0x16 (TLS handshake)
    gets TLS, anything else plain HTTP. Browsers keep using http:// while camera systems encrypt."""

    PEEK_TIMEOUT = 20

    def __init__(self, handler, host: str, port: int, ssl_ctx):
        self.handler = handler
        self.ssl_ctx = ssl_ctx
        host = host or "0.0.0.0"
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        self.sock = socket.create_server((host, port), family=family, backlog=128)
        self.sock.setblocking(False)
        self.sockets = [self.sock]
        self._task = None
        self._conns = set()

    def start(self) -> None:
        self._task = asyncio.ensure_future(self._accept())

    async def _accept(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            try:
                conn, _ = await loop.sock_accept(self.sock)
            except asyncio.CancelledError:
                raise
            except OSError as e:
                if self.sock.fileno() == -1:
                    return
                log.warning("accept failed: %s", e)
                await asyncio.sleep(0.5)
                continue
            task = asyncio.ensure_future(self._serve(conn))
            self._conns.add(task)
            task.add_done_callback(self._conns.discard)

    async def _serve(self, conn: socket.socket) -> None:
        loop = asyncio.get_running_loop()
        try:
            first = await asyncio.wait_for(_peek(loop, conn), self.PEEK_TIMEOUT)
            if not first:
                conn.close()
                return
            reader = asyncio.StreamReader(limit=65536)
            protocol = asyncio.StreamReaderProtocol(reader)
            kw = {"ssl": self.ssl_ctx, "ssl_handshake_timeout": 20} if first[0] == 0x16 else {}
            transport, _ = await loop.connect_accepted_socket(lambda: protocol, conn, **kw)
        except asyncio.CancelledError:
            conn.close()
            raise
        except Exception:  # noqa: BLE001 - failed handshake, timeout, reset: drop the connection
            conn.close()
            return
        await self.handler(reader, asyncio.StreamWriter(transport, protocol, reader, loop))

    def close(self) -> None:
        if self._task:
            self._task.cancel()
        try:
            self.sock.close()
        except OSError:
            pass

    async def wait_closed(self) -> None:
        if self._task:
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass


async def _peek(loop, conn: socket.socket) -> bytes:
    """First byte of a new connection, left in the socket for the TLS layer / HTTP parser."""
    while True:
        try:
            ready = loop.create_future()
            loop.add_reader(conn.fileno(), lambda: ready.done() or ready.set_result(None))
        except NotImplementedError:  # Windows proactor loop has no add_reader
            while not select.select([conn], [], [], 0)[0]:
                await asyncio.sleep(0.05)
        else:
            try:
                await ready
            finally:
                loop.remove_reader(conn.fileno())
        try:
            return conn.recv(1, socket.MSG_PEEK)
        except (BlockingIOError, InterruptedError):
            continue


def make_server_ssl(cert: str, key: str):
    ctx = ssl_mod.SSLContext(ssl_mod.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl_mod.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert, key)
    return ctx


def now_ms() -> int:
    return int(time.time() * 1000)
