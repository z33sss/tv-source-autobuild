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


def _probe(url, timeout, headers=None, max_bytes=3_000_000):
    resp = http_get(url, timeout=timeout, headers=headers, max_bytes=max_bytes)
    detail = resp["error"] or f"HTTP {resp['status']}"
    return resp, detail


def _get_play(url, timeout, headers=None, max_bytes=131_072):
    """Fetch a playback asset.

    Many CDNs hotlink-protect their playlists: the first attempt is made
    without a Referer (that is what "直链" means), and only on an explicit
    401/403 is it retried once with a same-origin Referer, the way a browser
    would when it lands on the video host.
    """
    resp, detail = _probe(url, timeout, headers=headers, max_bytes=max_bytes)
    if resp["ok"] or resp.get("status") not in (401, 403):
        return resp, detail
    parts = urllib.parse.urlsplit(url)
    origin = {"Referer": f"{parts.scheme}://{parts.netloc}/",
              "Origin": f"{parts.scheme}://{parts.netloc}"}
    return _probe(url, timeout, headers=origin, max_bytes=max_bytes)


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


def probe_cms(url, timeout=10, keyword=None, settings=None):
    """Apple CMS / 苹果CMS 采集接口深度验证.

    Stage 1  目录接口返回真实的 VOD 行（不是 HTML、不是空列表）
    Stage 2  关键词搜索，证明目录可查询
    Stage 3  播放链路 —— 取一条真实播放地址，确认它指向的直链真能下到分片
    """
    settings = settings or {}
    play_mode = str(settings.get("play_check", "auto") or "auto").lower()
    play_tries = max(1, int(settings.get("play_check_urls", 2) or 2))

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

    # body that is most likely to carry `vod_play_url` for stage 3
    detail_text = text

    # Stage 2: keyword search proves the catalogue is queryable.
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
            items = sitems
            detail_text = _text(sresp)
            detail = f"list+search ok, {sitems} hit(s) for '{keyword}'"
        else:
            detail = f"list ok ({items} items), search unreachable ({sdetail})"

    # Stage 3: 播放链路
    note = ""
    if play_mode != "off":
        note, error = _playback_gate(url, detail_text, play_tries, timeout,
                                     play_mode == "strict")
        if error:
            return _result(False, "cms", url, resp, items,
                           f"{detail} · 播放链路失败: {error}")

    return _result(True, "cms", url, resp, items, detail + note, note)


# ------------------------------------------------- 播放链路 (闸门 3)

MEDIA_EXTS = (".mp4", ".m4v", ".flv", ".mkv", ".ts", ".rmvb", ".avi", ".mov")


def _classify_play_url(url: str):
    """'hls' for an HLS playlist, 'media' for a direct file, otherwise None."""
    parts = urllib.parse.urlsplit(str(url))
    target = (urllib.parse.unquote(parts.path) + "?" + parts.query).lower()
    if "m3u8" in target:
        return "hls"
    if urllib.parse.unquote(parts.path).lower().endswith(MEDIA_EXTS):
        return "media"
    return None


def _extract_play_urls(text: str, limit: int = 8) -> list:
    """Pull candidate play URLs out of an Apple CMS payload.

    Apple CMS packs episodes as `标题$链接#标题$链接`, with several playback
    lines (线路) separated by `$$$`. Plain JSON bodies that embed an m3u8 URL
    anywhere in a string value are picked up too.
    """
    stripped = text.lstrip("\ufeff").lstrip()
    if stripped[:1] not in ("{", "["):
        return []
    try:
        data = json.loads(stripped)
    except Exception:
        return []

    found, seen = [], set()

    def add(value):
        value = str(value).strip()
        if is_url(value) and value not in seen:
            seen.add(value)
            found.append(value)

    def walk(node, depth=0):
        if depth > 5 or len(found) >= limit * 4:
            return
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "vod_play_url" and isinstance(value, str):
                    for line in value.split("$$$"):
                        for episode in line.split("#"):
                            pieces = episode.split("$", 1)
                            add(pieces[1] if len(pieces) == 2 else pieces[0])
                elif isinstance(value, str):
                    if "m3u8" in value.lower() and "http" in value:
                        for m in re.finditer(r'https?://[^\s"\'<>#$]+', value):
                            add(m.group(0))
                else:
                    walk(value, depth + 1)
        elif isinstance(node, list):
            for item in node[:80]:
                walk(item, depth + 1)

    walk(data)
    return found[:limit]


def _first_variant(text: str, base: str):
    """URL of the first `#EXT-X-STREAM-INF` variant, resolved against `base`."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF"):
            continue
        for nxt in lines[i + 1:]:
            nxt = nxt.strip()
            if not nxt or nxt.startswith("#"):
                continue
            if nxt.startswith("data:"):
                return None
            return urllib.parse.urljoin(base, nxt)
        return None
    return None


def _segments(text: str, base: str) -> list:
    """Media segment URIs of a playlist, resolved against `base`."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-MAP:"):
            m = re.search(r'URI="([^"]+)"', line)
            if m:
                out.append(urllib.parse.urljoin(base, m.group(1)))
            continue
        if line.startswith("#") or line.startswith("data:"):
            continue
        out.append(urllib.parse.urljoin(base, line))
    return out


def probe_playback(url, timeout=10, headers=None, max_hops=3):
    """播放链路深度验证 —— "接口活着" ≠ "真的能看".

    HLS:   播放列表 → (主播放列表 → 变体) → 媒体播放列表 → 首个分片真能下载
    media: 直链直接 GET，要求返回非空且不是 HTML
    """
    if _classify_play_url(url) == "media":
        resp, detail = _get_play(url, timeout, headers, max_bytes=65_536)
        if not resp["ok"]:
            return _result(False, "hls", url, resp, 0, f"直链不可用: {detail}")
        if not resp["body"]:
            return _result(False, "hls", url, resp, 0, "直链返回空内容")
        if _looks_like_html(_text(resp)):
            return _result(False, "hls", url, resp, 0, "直链返回的是 HTML")
        return _result(True, "hls", url, resp, 1, f"直链 ok ({len(resp['body'])}B)")

    resp = {"ok": False, "status": 0, "body": b"", "content_type": "",
            "final_url": url, "latency_ms": 0, "error": "not started"}
    seen, current, hops, text = set(), url, 0, ""

    while True:
        if current in seen or hops >= max_hops:
            return _result(False, "hls", url, resp, 0, "播放列表嵌套过深或成环")
        seen.add(current)
        hops += 1
        resp, detail = _get_play(current, timeout, headers)
        if not resp["ok"]:
            return _result(False, "hls", url, resp, 0, f"播放列表不可用: {detail}")
        text = _text(resp)
        if "#EXTM3U" not in text[:200]:
            return _result(False, "hls", url, resp, 0, "不是 M3U 播放列表")
        variant = _first_variant(text, resp.get("final_url") or current)
        if variant:
            current = variant
            continue
        break

    base = resp.get("final_url") or current
    segments = _segments(text, base)
    if not segments:
        return _result(False, "hls", url, resp, 0, "播放列表里没有分片")

    last = ""
    for segment in segments[:2]:
        sresp, sdetail = _get_play(segment, timeout, headers, max_bytes=65_536)
        if not sresp["ok"]:
            last = f"分片下载失败: {sdetail}"
            continue
        if not sresp["body"]:
            last = "分片为空"
            continue
        if _looks_like_html(_text(sresp)):
            last = "分片返回的是 HTML"
            continue
        return _result(True, "hls", url, resp, len(segments),
                       f"播放链路 ok（{hops}级列表 · {len(segments)}分片）")
    return _result(False, "hls", url, resp, 0, last or "分片下载失败")


def _playback_gate(api_url, detail_text, tries, timeout, strict):
    """Returns `(note, error)`; a non-empty `error` means the gate failed."""
    play_urls = _extract_play_urls(detail_text)

    if not play_urls:
        # Apple CMS convention: the first catalogue entry's detail carries
        # `vod_play_url`.
        dresp, _ = _probe(add_params(api_url, {"ac": "detail", "ids": "1"}), timeout)
        if dresp["ok"]:
            play_urls = _extract_play_urls(_text(dresp))

    direct = [u for u in play_urls if _classify_play_url(u)]
    direct.sort(key=lambda u: 0 if _classify_play_url(u) == "hls" else 1)

    if not direct:
        if strict:
            return "", "目录里没有任何可直连播放的地址（全部依赖解析接口）"
        return " · 播放链路未验证（无直链）", None

    errors = []
    for candidate in direct[:tries]:
        res = probe_playback(candidate, timeout)
        if res["ok"]:
            return " · " + res["detail"], None
        errors.append(res["detail"])
    return "", errors[-1]


TARGET_LABEL = {"cms": "采集", "json": "配置", "play": "播放", "http": "地址"}


def _run_target(kind, url, site, settings, timeout):
    if kind == "cms":
        return probe_cms(url, timeout, settings.get("keyword"), settings)
    if kind == "json":
        return probe_json(url, timeout)
    if kind == "play":
        # player gateways answer 200 even with an empty ?url=, so only
        # reachability is meaningful here.
        return probe_http(url, timeout, min_bytes=1)
    return probe_http(url, timeout, expect=site.get("_expect"))


def probe_site(site, settings, timeout=10):
    """Probe a `sites` entry. Returns the worst-case result across its URLs."""
    targets = list(_site_targets(site))

    if not targets:
        # e.g. `api: "csp_Xxx"` served from the shared spider jar - nothing
        # standalone to reach. Keep it, but flag it as unverifiable.
        return {
            "ok": True,
            "kind": "none",
            "url": "",
            "latency_ms": 0,
            "items": 0,
            "targets": [],
            "detail": "unverified (no probe target)",
        }

    results = [
        (label, kind, url, _run_target(kind, url, site, settings, timeout))
        for kind, url, label in targets
    ]
    targets_meta = [
        {"label": label, "kind": kind, "url": url, "ok": res["ok"]}
        for label, kind, url, res in results
    ]

    failed = [r for r in results if not r[3]["ok"]]
    worst_latency = max(r[3]["latency_ms"] for r in results)
    items = max(r[3]["items"] for r in results)
    key = site.get("key") or site.get("name") or "?"

    if failed:
        label, kind, url, res = failed[0]
        return {
            "ok": False,
            "kind": kind,
            "url": url,
            "latency_ms": worst_latency,
            "items": 0,
            "targets": targets_meta,
            "detail": f"[{key}] {label} {TARGET_LABEL.get(kind, kind)}失败: {res['detail']}",
        }

    parts = []
    for label, kind, url, res in results:
        if kind in ("cms", "json"):
            parts.append(f"{label} {res['items']}条" + (res.get("note") or ""))
        else:
            parts.append(f"{label} ok")
    return {
        "ok": True,
        "kind": results[0][1],
        "url": results[0][2],
        "latency_ms": worst_latency,
        "items": items,
        "targets": targets_meta,
        "detail": f"[{key}] " + " · ".join(parts),
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


def _is_vod_api(url) -> bool:
    """True when a URL looks like an Apple-CMS 采集接口 rather than a plain page."""
    path = urllib.parse.urlsplit(str(url)).path.lower()
    return "provide/vod" in path


def _site_targets(site):
    """Yield (kind, url, label) triples worth probing for a `sites` entry."""
    seen = set()

    def emit(kind, url, label):
        if is_url(url) and url not in seen:
            seen.add(url)
            return (kind, url, label)
        return None

    candidates = []

    # Explicit probe spec always wins.
    spec = site.get("probe") or {}
    if spec.get("url"):
        candidates.append((spec.get("kind", "http"), spec["url"], "probe"))

    ext = site.get("ext")
    if isinstance(ext, str) and is_url(ext):
        if _is_vod_api(ext):
            candidates.append(("cms", ext, "ext"))
        else:
            end = ext.lower().split("?")[0]
            candidates.append(("json" if end.endswith(".json") else "http", ext, "ext"))
    elif isinstance(ext, dict):
        if is_url(ext.get("api")):
            candidates.append(("cms", ext["api"], "ext.api"))
        if is_url(ext.get("url")):
            candidates.append(("http", ext["url"], "ext.url"))

    # Classify by URL shape first: `type: 1` with a 采集接口 path is still a CMS,
    # and a `type: 0` site pointing at a plain page is not.
    api = site.get("api")
    if is_url(api):
        kind = "cms" if (site.get("type") in (0, 3, 6) or _is_vod_api(api)) else "http"
        candidates.append((kind, api, "api"))

    jar = site.get("jar")
    if is_url(jar):
        candidates.append(("http", jar, "jar"))

    # Per-site player override (recognized by fongmi/TV Site.java).
    play = site.get("playUrl")
    if is_url(play):
        candidates.append(("play", play, "playUrl"))

    for kind, url, label in candidates:
        out = emit(kind, url, label)
        if out:
            yield out


def _result(ok, kind, url, resp, items, detail, note=""):
    return {
        "ok": ok,
        "kind": kind,
        "url": url,
        "latency_ms": resp.get("latency_ms", 0),
        "items": items,
        "detail": detail,
        "note": note,
    }
