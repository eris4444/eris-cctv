"""TLS certificates for the server: self-signed creation (openssl, or pure Python) and fingerprints.

Camera systems pin the server certificate on first contact, so a self-signed certificate is enough
to encrypt the camera -> server link. The pure-Python generator is the fallback for systems without
the openssl command (Windows, minimal containers).
"""
from __future__ import annotations

import datetime
import hashlib
import ipaddress
import os
import re
import secrets
import shutil
import ssl
import subprocess

from .util import IS_WINDOWS, NO_WINDOW, dirs


def default_paths() -> tuple:
    folder = os.path.join(dirs()["config"], "tls")
    return os.path.join(folder, "cert.pem"), os.path.join(folder, "key.pem")


def fingerprint_file(path: str) -> str:
    """SHA-256 of the first certificate in a PEM file (what camera systems pin)."""
    try:
        with open(path, encoding="ascii", errors="ignore") as f:
            pem = f.read()
        m = re.search(r"-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----", pem, re.S)
        if not m:
            return ""
        return hashlib.sha256(ssl.PEM_cert_to_DER_cert(m.group(0))).hexdigest()
    except (OSError, ValueError):
        return ""


def usable(cert: str, key: str) -> bool:
    if not (cert and key and os.path.isfile(cert) and os.path.isfile(key)):
        return False
    try:
        ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(cert, key)
        return True
    except (ssl.SSLError, OSError, ValueError):
        return False


def make_self_signed(address: str = "", cert: str = "", key: str = "") -> tuple:
    """Create a 10-year self-signed certificate. Returns (cert_path, key_path)."""
    if not cert or not key:
        cert, key = default_paths()
    os.makedirs(os.path.dirname(cert), exist_ok=True)
    address = (address or "").strip()
    if not _with_openssl(address, cert, key):
        cert_pem, key_pem = generate(address)
        _write(key, key_pem, private=True)
        _write(cert, cert_pem)
    if not IS_WINDOWS:
        os.chmod(key, 0o600)
    if not usable(cert, key):
        raise RuntimeError("the new certificate could not be loaded")
    return cert, key


def _write(path: str, text: str, private: bool = False) -> None:
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600 if private else 0o644)
    with os.fdopen(fd, "w", encoding="ascii") as f:
        f.write(text)
    os.replace(tmp, path)


def _with_openssl(address: str, cert: str, key: str) -> bool:
    exe = shutil.which("openssl")
    if not exe:
        return False
    base = [exe, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", cert,
            "-days", "3650", "-subj", "/CN=" + (address or "eris-cctv")]
    try:
        r = subprocess.run(base + ["-addext", "subjectAltName=" + _san_text(address)], capture_output=True,
                           text=True, timeout=60, creationflags=NO_WINDOW)
        if r.returncode != 0:
            r = subprocess.run(base, capture_output=True, text=True, timeout=60, creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0 and usable(cert, key)


def _san_text(address: str) -> str:
    if not address:
        return "DNS:eris-cctv"
    try:
        ipaddress.ip_address(address)
        return "IP:" + address
    except ValueError:
        return "DNS:" + address


# ---------------------------------------------------------------------------------------------
# Pure-Python RSA-2048 self-signed certificate (X.509 v3, sha256WithRSAEncryption).

def _len(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(b)]) + b


def _tlv(tag: int, content: bytes) -> bytes:
    return bytes([tag]) + _len(len(content)) + content


def _int(i: int) -> bytes:
    b = i.to_bytes(i.bit_length() // 8 + 1, "big")  # one spare byte keeps it positive
    while len(b) > 1 and b[0] == 0 and b[1] < 0x80:
        b = b[1:]
    return _tlv(0x02, b)


def _seq(*items: bytes) -> bytes:
    return _tlv(0x30, b"".join(items))


def _oid(dotted: str) -> bytes:
    parts = [int(x) for x in dotted.split(".")]
    out = bytearray([40 * parts[0] + parts[1]])
    for p in parts[2:]:
        chunk = [p & 0x7F]
        p >>= 7
        while p:
            chunk.append(0x80 | (p & 0x7F))
            p >>= 7
        out += bytes(reversed(chunk))
    return _tlv(0x06, bytes(out))


def _time(t: datetime.datetime) -> bytes:
    if t.year < 2050:
        return _tlv(0x17, t.strftime("%y%m%d%H%M%SZ").encode())
    return _tlv(0x18, t.strftime("%Y%m%d%H%M%SZ").encode())


_NULL = b"\x05\x00"
_SMALL_PRIMES = [p for p in range(3, 2000, 2) if all(p % d for d in range(3, int(p ** 0.5) + 1, 2))]


def _probable_prime(n: int, rounds: int = 40) -> bool:
    for p in _SMALL_PRIMES:
        if n % p == 0:
            return n == p
    d, s = n - 1, 0
    while d % 2 == 0:
        d //= 2
        s += 1
    for _ in range(rounds):
        x = pow(secrets.randbelow(n - 3) + 2, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(s - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def _prime(bits: int, e: int) -> int:
    while True:
        c = secrets.randbits(bits) | (3 << (bits - 2)) | 1
        if (c - 1) % e and _probable_prime(c):
            return c


def _pem(label: str, der: bytes) -> str:
    import base64
    b64 = base64.b64encode(der).decode()
    lines = [b64[i:i + 64] for i in range(0, len(b64), 64)]
    return "-----BEGIN %s-----\n%s\n-----END %s-----\n" % (label, "\n".join(lines), label)


def generate(address: str = "", days: int = 3650) -> tuple:
    """Returns (cert_pem, key_pem) for a fresh RSA-2048 key."""
    e = 65537
    while True:
        p, q = _prime(1024, e), _prime(1024, e)
        n = p * q
        if p != q and n.bit_length() == 2048:
            break
    lam = (p - 1) * (q - 1) // _gcd(p - 1, q - 1)
    d = pow(e, -1, lam)
    key_der = _seq(_int(0), _int(n), _int(e), _int(d), _int(p), _int(q), _int(d % (p - 1)), _int(d % (q - 1)),
                   _int(pow(q, -1, p)))

    cn = (address or "eris-cctv")[:64]
    name = _seq(_tlv(0x31, _seq(_oid("2.5.4.3"), _tlv(0x0C, cn.encode()))))
    now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    validity = _seq(_time(now - datetime.timedelta(days=1)), _time(now + datetime.timedelta(days=days)))
    spki = _seq(_seq(_oid("1.2.840.113549.1.1.1"), _NULL), _tlv(0x03, b"\x00" + _seq(_int(n), _int(e))))
    try:
        ip = ipaddress.ip_address(address)
        general_name = _tlv(0x87, ip.packed)
    except ValueError:
        general_name = _tlv(0x82, (address or "eris-cctv").encode("idna"))
    san = _seq(_oid("2.5.29.17"), _tlv(0x04, _seq(general_name)))
    basic = _seq(_oid("2.5.29.19"), _tlv(0x04, _seq()))
    sig_alg = _seq(_oid("1.2.840.113549.1.1.11"), _NULL)
    serial = secrets.randbits(63) | (1 << 62)
    tbs = _seq(_tlv(0xA0, _int(2)), _int(serial), sig_alg, name, validity, name, spki, _tlv(0xA3, _seq(san, basic)))

    digest_info = _seq(_seq(_oid("2.16.840.1.101.3.4.2.1"), _NULL), _tlv(0x04, hashlib.sha256(tbs).digest()))
    k = 256
    em = b"\x00\x01" + b"\xff" * (k - 3 - len(digest_info)) + b"\x00" + digest_info
    sig = pow(int.from_bytes(em, "big"), d, n)
    if pow(sig, e, n) != int.from_bytes(em, "big"):
        raise RuntimeError("RSA self-check failed")
    cert_der = _seq(tbs, sig_alg, _tlv(0x03, b"\x00" + sig.to_bytes(k, "big")))
    return _pem("CERTIFICATE", cert_der), _pem("RSA PRIVATE KEY", key_der)


def _gcd(a: int, b: int) -> int:
    while b:
        a, b = b, a % b
    return a
