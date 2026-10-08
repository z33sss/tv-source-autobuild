"""Shared helpers: config IO, HTTP fetching, decoding."""

from __future__ import annotations

import gzip
import json
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "sources.json"
STATE_DIR = ROOT / "state"
DIST_DIR = ROOT / "dist"

USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 11; TV) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_CTX_STRICT = ssl.create_default_context()
_CTX_RELAXED = ssl._create_unverified_context()


# --------------------------------------------------------------------------- IO


def load_json(path, default=None):
    p = Path(path)
    if not p.exists():
        return default
    with p.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def save_json(path, data):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    tmp.replace(p)


def load_config():
    cfg = load_json(CONFIG_PATH, None)
    if cfg is None:
        raise SystemExit(f"config not found: {CONFIG_PATH}")
    return cfg


# -------------------------------------------------------------------------- HTTP


def _decompress(raw: bytes, encoding: str | None) -> bytes:
    if not raw:
        return b""
    enc = (encoding or "").lower()
    try:
        if "gzip" in enc:
            return gzip.decompress(raw)
        if "deflate" in enc:
            try:
                return zlib.decompress(raw)
            except zlib.error:
                return zlib.decompress(raw, -zlib.MAX_WBITS)
    except Exception:
        pass
    if raw[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(raw)
        except Exception:
            pass
    return raw


def decode_text(body: bytes, content_type: str = "") -> str:
    charset = None
    m = re.search(r"charset=([\w\-]+)", content_type or "", re.I)
    if m:
        charset = m.group(1)
    for cs in (charset, "utf-8", "gb18030", "big5", "latin-1"):
        if not cs:
            continue
        try:
            return body.decode(cs)
        except Exception:
            continue
    return body.decode("utf-8", "ignore")


def http_get(url, timeout=10, headers=None, max_bytes=3_000_000, tries=2):
    """GET a URL. Returns a dict, never raises for network errors.

    HTTPS is tried with certificate verification first, then retried with
    verification relaxed (many self-hosted VOD endpoints have bad certs).
    """
    hdrs = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "close",
    }
    if headers:
        hdrs.update(headers)

    is_https = url.lower().startswith("https://")
    contexts = [_CTX_STRICT, _CTX_RELAXED] if is_https else [None]

    last_err = "unknown"
    started = time.perf_counter()

    for ctx in contexts:
        for attempt in range(tries):
            req = urllib.request.Request(url, headers=hdrs, method="GET")
            try:
                if ctx is None:
                    resp = urllib.request.urlopen(req, timeout=timeout)
                else:
                    resp = urllib.request.urlopen(req, timeout=timeout, context=ctx)
                with resp:
                    raw = resp.read(max_bytes)
                    body = _decompress(raw, resp.headers.get("Content-Encoding"))
                    return {
                        "ok": True,
                        "status": getattr(resp, "status", 200),
                        "final_url": resp.geturl(),
                        "content_type": resp.headers.get("Content-Type", ""),
                        "body": body,
                        "latency_ms": int((time.perf_counter() - started) * 1000),
                        "error": None,
                    }
            except urllib.error.HTTPError as exc:
                try:
                    body = _decompress(exc.read(max_bytes), exc.headers.get("Content-Encoding") if exc.headers else None)
                except Exception:
                    body = b""
                return {
                    "ok": False,
                    "status": exc.code,
                    "final_url": url,
                    "content_type": (exc.headers.get("Content-Type", "") if exc.headers else ""),
                    "body": body,
                    "latency_ms": int((time.perf_counter() - started) * 1000),
                    "error": f"HTTP {exc.code}",
                }
            except Exception as exc:  # DNS, timeout, reset, SSL...
                last_err = f"{type(exc).__name__}: {exc}"
                if attempt + 1 < tries:
                    time.sleep(0.5)

    return {
        "ok": False,
        "status": 0,
        "final_url": url,
        "content_type": "",
        "body": b"",
        "latency_ms": int((time.perf_counter() - started) * 1000),
        "error": last_err,
    }


def add_params(url, params):
    """Append query params that are not already present."""
    parts = urllib.parse.urlsplit(url)
    q = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    have = {k for k, _ in q}
    for k, v in params.items():
        if k not in have:
            q.append((k, v))
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(q), parts.fragment)
    )


def is_url(value) -> bool:
    return isinstance(value, str) and value.lower().startswith(("http://", "https://"))
