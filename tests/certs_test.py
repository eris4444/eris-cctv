"""The self-signed certificate generator: pure Python and (if present) openssl, then a real TLS handshake.

Run:  python tests/certs_test.py
"""
import asyncio
import os
import shutil
import ssl
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
HOME = tempfile.mkdtemp(prefix="eriscctv-certs-")
os.environ["ERIS_CCTV_HOME"] = HOME

from eriscctv import certs  # noqa: E402


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  ok -", msg)


async def handshake(cert, key):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)

    async def serve(reader, writer):
        writer.write(b"hello over tls")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0, ssl=ctx)
    port = server.sockets[0].getsockname()[1]
    cctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    cctx.check_hostname = False
    cctx.verify_mode = ssl.CERT_NONE
    reader, writer = await asyncio.open_connection("127.0.0.1", port, ssl=cctx)
    data = await reader.read(100)
    der = writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
    writer.close()
    server.close()
    await server.wait_closed()
    return data, der


def main():
    import hashlib
    for label, address in (("domain", "cctv.example.com"), ("IP", "203.0.113.10"), ("empty", "")):
        t = time.time()
        cert_pem, key_pem = certs.generate(address)
        took = time.time() - t
        folder = os.path.join(HOME, label)
        os.makedirs(folder)
        cert, key = os.path.join(folder, "c.pem"), os.path.join(folder, "k.pem")
        with open(cert, "w") as f:
            f.write(cert_pem)
        with open(key, "w") as f:
            f.write(key_pem)
        check(certs.usable(cert, key), "pure-Python %s certificate loads (%.1fs)" % (label, took))
        data, der = asyncio.run(handshake(cert, key))
        check(data == b"hello over tls", "TLS handshake with it works")
        check(hashlib.sha256(der).hexdigest() == certs.fingerprint_file(cert), "fingerprint matches the peer cert")
        info = ssl._ssl._test_decode_cert(cert)
        check(info["subject"] == ((("commonName", address or "eris-cctv"),),), "subject %s" % (info["subject"],))
        san = info.get("subjectAltName", ())
        check(san and san[0][1] == (address or "eris-cctv"), "subjectAltName %s" % (san,))
    cert, key = certs.make_self_signed("cctv.example.com")
    check(certs.usable(cert, key), "make_self_signed (%s) -> %s" % ("openssl" if shutil.which("openssl") else
                                                                     "pure Python", cert))
    print("\nALL CERT CHECKS PASSED")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(HOME, ignore_errors=True)
