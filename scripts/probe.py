"""Deep verification probes for TVBox / FongMi 接口 elements.

Each probe returns a dict:
    {"ok": bool, "kind": str, "url": str, "latency_ms": int,
     "items": int, "detail": str}
"""

from __future__ import annotations

import json
import re
import urllib.parse
import xml.etree.ElementTree as ET

from lib import add_params, decode_text, http_get, is_url

# Keys that commonly hold a list of results in a VOD API response.
LIST_KEYS = ("list", "data", "results", "result", "rows", "items", "vod", "records")


# ------------------------------------------------------------------ primitives


def _text(resp) -> str:
    return decode_text(resp["body"], resp.get("content_type", ""))


def _count_items(text: str) -> int:
    """Count entries in a JSON or XML VOD response."""
    stripped = text.lstrip()
    if stripped[:1] in ("{", "["):
        try:
            data = json.loads(stripped)
        except Exception:
            data = None
        if isinstance(data, list):
            return len(data)
        if isinstance(data, dict):
            for key in LIST_KEYS:
                value = data.get(key)
                if isinstance(value, list) and value:
                    return len(value)
            # one level deeper: {"data": {"list": [...]}}
            for value in data.values():
                if isinstance(value, dict):
                    for key in LIST_KEYS:
                        inner = value.get(key)
                        if isinstance(inner, list) and inner:
                            return len(inner)
        return 0

    if stripped[:1] == "<":
        try:
            root = ET.fromstring(text)
        except Exception:
            return 0
        videos = root.findall(".//video")
        if videos:
            return len(videos)
        items = root.findall(".//item")
        return len(items)

    return 0


def _looks_like_html(text: str) -> bool:
    head = text[:400].lower()
    return "<!doctype html" in head or "<html" in head or "<head" in head


def _probe(url, timeout, headers=None):
    resp = http_get(url, timeout=timeout, headers=headers)
    detail = resp["error"] or f"HTTP {resp['status']}"
    return resp, detail


# ---------------------------------------------------------------------- probes


def probe_http(url, timeout=10, expect=None, min_bytes=200):
    """Plain reachability + optional content markers."""
    resp, detail = _probe(url, timeout)
    if not resp["ok"]:
        return _result(False, "http", url, resp, 0, detail)

    body = resp["body"]
    if len(body) < min_bytes:
        return _result(False, "http", url, resp, 0, f"body too small ({len(body)}B)")

    if expect:
        text = _text(resp)
        missing = [m for m in expect if m not in text]
        if missing:
            return _result(False, "http", url, resp, 0, f"missing marker: {missing[0]}")

    return _result(True, "http", url, resp, 0, f"HTTP {resp['status']} {len(body)}B")


def probe_json(url, timeout=10, list_key_required=True):
    resp, detail = _probe(url, timeout)
    if not resp["ok"]:
        return _result(False, "json", url, resp, 0, detail)

    text = _text(resp)
    try:
        json.loads(text.lstrip())
    except Exception as exc:
        return _result(False, "json", url, resp, 0, f"invalid JSON: {exc}")

    items = _count_items(text) if list_key_required else 1
    if list_key_required and items == 0:
        return _result(False, "json", url, resp, 0, "JSON has no list payload")

    return _result(True, "json", url, resp, items, f"JSON ok, {items} item(s)")


def probe_m3u8(url, timeout=10):
    resp, detail = _probe(url, timeout)
    if not resp["ok"]:
        return _result(False, "m3u8", url, resp, 0, detail)

    text = _text(resp)
    if "#EXTM3U" not in text[:200]:
        return _result(False, "m3u8", url, resp, 0, "not an M3U playlist")

    channels = text.count("#EXTINF")
    if channels == 0:
        # a bare .m3u8 media playlist has EXTINF too; master playlists list variants
        channels = text.count("#EXT-X-STREAM-INF")
    return _result(True, "m3u8", url, resp, channels, f"{channels} channel(s)")


def probe_cms(url, timeout=10, keyword=None):
    """Apple CMS / 苹果CMS 采集接口深度验证.

    Tests the program-list endpoint first, then (optionally) a keyword search
    to make sure the backend actually returns real VOD data.
    """
    list_url = add_params(url, {"ac": "list", "pg": "1"})
    resp, detail = _probe(list_url, timeout)

    if not resp["ok"]:
        # some deployments ignore `ac` and only serve the plain base path
        resp, detail = _probe(url, timeout)
        if not resp["ok"]:
            return _result(False, "cms", url, resp, 0, detail)

    text = _text(resp)
    items = _count_items(text)
    if items == 0:
        if _looks_like_html(text):
            return _result(False, "cms", url, resp, 0, "returned HTML instead of VOD data")
        return _result(False, "cms", url, resp, 0, "empty list payload")

    latency = resp["latency_ms"]

    # Second stage: keyword search proves the catalogue is queryable.
    if keyword:
        search_url = add_params(url, {"ac": "detail", "wd": keyword})
        sresp, sdetail = _probe(search_url, timeout)
        if sresp["ok"]:
            sitems = _count_items(_text(sresp))
            if sitems == 0:
                return _result(
                    False, "cms", url, sresp, items,
                    f"list ok ({items}) but search returned 0 for '{keyword}'",
                )
            latency = max(latency, sresp["latency_ms"])
            items = sitems
            detail = f"list+search ok, {sitems} hit(s) for '{keyword}'"
        else:
            detail = f"list ok ({items} items), search unreachable ({sdetail})"

    return _result(True, "cms", url, resp, items, detail)


def probe_site(site, settings, timeout=10):
    """Probe a `sites` entry. Returns the worst-case result across its URLs."""
    probes = []
    for kind, url in _site_targets(site):
        if kind == "cms":
            probes.append(probe_cms(url, timeout, settings.get("keyword")))
        elif kind == "json":
            probes.append(probe_json(url, timeout))
        else:
            markers = site.get("_expect")
            probes.append(probe_http(url, timeout, expect=markers))

    if not probes:
        # e.g. `api: "csp_Xxx"` served from the shared spider jar - nothing
        # standalone to reach. Keep it, but flag it as unverifiable.
        return {
            "ok": True,
            "kind": "none",
            "url": "",
            "latency_ms": 0,
            "items": 0,
            "detail": "unverified (no probe target)",
        }

    failed = [p for p in probes if not p["ok"]]
    worst_latency = max(p["latency_ms"] for p in probes)
    items = max(p["items"] for p in probes)
    key = site.get("key", "?")

    if failed:
        return {
            "ok": False,
            "kind": failed[0]["kind"],
            "url": failed[0]["url"],
            "latency_ms": worst_latency,
            "items": 0,
            "detail": f"[{key}] {failed[0]['detail']}",
        }
    return {
        "ok": True,
        "kind": probes[0]["kind"],
        "url": probes[0]["url"],
        "latency_ms": worst_latency,
        "items": items,
        "detail": f"[{key}] " + "; ".join(p["detail"] for p in probes),
    }


def probe_parse(entry, timeout=10, test_url=""):
    """Probe a `parses` entry.

    With `test_url` set, the parse service is asked to resolve a real video
    URL (deep check). Without it, only reachability is verified.
    """
    url = entry.get("url") or ""
    if not is_url(url):
        return _result(False, "parse", url, {"latency_ms": 0}, 0, "missing url")

    if test_url:
        full = url + urllib.parse.quote(test_url, safe="")
        resp, detail = _probe(full, timeout)
        if not resp["ok"]:
            return _result(False, "parse", url, resp, 0, detail)
        text = _text(resp)
        try:
            data = json.loads(text)
        except Exception:
            return _result(False, "parse", url, resp, 0, "non-JSON parse response")

        code = data.get("code", 1)
        resolved = data.get("url") or data.get("data") or data.get("link")
        if str(code) not in ("0", "1", "200") or not resolved:
            return _result(False, "parse", url, resp, 0, f"no playable url (code={code})")
        host = urllib.parse.urlsplit(str(resolved)).netloc
        return _result(True, "parse", url, resp, 1, f"resolved -> {host}")

    return probe_http(url, timeout, min_bytes=1)


def probe_live(entry, timeout=10):
    url = entry.get("url") or ""
    if not is_url(url):
        return _result(False, "live", url, {"latency_ms": 0}, 0, "missing url")
    return probe_m3u8(url, timeout)


# --------------------------------------------------------------------- helpers


def _site_targets(site):
    """Yield (kind, url) pairs worth probing for a `sites` entry."""
    seen = set()

    def emit(kind, url):
        if is_url(url) and url not in seen:
            seen.add(url)
            return (kind, url)
        return None

    candidates = []

    # Explicit probe spec always wins.
    probe = site.get("probe") or {}
    if probe.get("url"):
        candidates.append((probe.get("kind", "http"), probe["url"], None))

    ext = site.get("ext")
    if isinstance(ext, str) and is_url(ext):
        kind = "json" if ext.lower().split("?")[0].endswith(".json") else "http"
        candidates.append((kind, ext, None))
    elif isinstance(ext, dict):
        if is_url(ext.get("api")):
            candidates.append(("cms", ext["api"], None))
        if is_url(ext.get("url")):
            candidates.append(("http", ext["url"], None))

    api = site.get("api")
    if is_url(api):
        kind = "cms" if site.get("type") in (0, 3, 6) else "http"
        candidates.append((kind, api, None))

    jar = site.get("jar")
    if is_url(jar):
        candidates.append(("http", jar, None))

    for kind, url, expect in candidates:
        out = emit(kind, url)
        if out:
            yield out


def _result(ok, kind, url, resp, items, detail):
    return {
        "ok": ok,
        "kind": kind,
        "url": url,
        "latency_ms": resp.get("latency_ms", 0),
        "items": items,
        "detail": detail,
    }
