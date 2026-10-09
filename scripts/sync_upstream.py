#!/usr/bin/env python3
"""按 config/upstreams.json 把上游接口收养进本地候选池。

    python scripts/sync_upstream.py [--dry-run] [--only NAME]

设计要点：

* **只负责搬运，不做取舍** —— 拉进来的东西当次流水线就会被 autobuild.py
  深度体检，死源连续 3 次失败后自动剔除。
* **每个被收养的条目打上 `_from` 标记**。下划线前缀字段在生成
  dist/tvbox.json 时会被剥掉，所以既能查血缘，又不污染输出。
* **单个上游挂掉只告警不中断**，绝不让一个坏 URL 拖垮每日流水线。
* 格式自动识别：TVBox 的 `sites` / `api_site` 清单（及 LunaTV 的 txt 版）都能吃。
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import CONFIG_PATH, STATE_DIR, decode_text, http_get, is_url, load_config, load_json, save_json  # noqa: E402
from import_sources import SECTIONS, merge, parse_loose  # noqa: E402

UPSTREAMS_PATH = CONFIG_PATH.parent / "upstreams.json"
SYNC_STATE = STATE_DIR / "sync.json"


# --------------------------------------------------------------------- fetching


def fetch_text(url: str, timeout: int = 20):
    """Returns `(text, error)`; never raises for network errors."""
    resp = http_get(url, timeout=timeout)
    if not resp["ok"]:
        return None, resp["error"] or f"HTTP {resp['status']}"
    return decode_text(resp["body"], resp.get("content_type", "")), None


def _unwrap(text: str) -> str:
    """GitHub `/contents/` API wraps the file as base64 JSON - unwrap it."""
    stripped = text.lstrip("\ufeff").strip()
    if not stripped.startswith("{"):
        return text
    try:
        data = json.loads(stripped)
    except Exception:
        return text
    if isinstance(data, dict) and data.get("encoding") == "base64" and data.get("content"):
        try:
            return base64.b64decode(data["content"]).decode("utf-8", "ignore")
        except Exception:
            return text
    return text


# -------------------------------------------------------------------- adapters


def detect_format(data) -> str:
    if isinstance(data, dict):
        if isinstance(data.get("api_site"), (dict, list)):
            return "lunatv"
        if isinstance(data.get("sites"), list):
            return "tvbox"
    return "unknown"


def _slug(label: str, index: int) -> str:
    slug = re.sub(r"[^0-9A-Za-z_]+", "_", str(label)).strip("_")[:40]
    return slug or f"up_{index}"


def from_lunatv(data: dict, name: str) -> dict:
    """`{"cache_time": ..., "api_site": {label: {name, api, detail}}}` -> sites.

    LunaTV-style packs its catalogue as a map keyed by label rather than a
    TVBox `sites` array, and carries no `type` field. These are all 采集
    接口, so they are emitted as `type: 0` - probe classification is by URL
    path anyway, so a `provide/vod` path gets deep-verified regardless.
    """
    raw = data.get("api_site")
    entries = list(raw.values()) if isinstance(raw, dict) else list(raw or [])

    sites = []
    for index, item in enumerate(entries):
        if not isinstance(item, dict):
            continue
        api = item.get("api")
        if not isinstance(api, str) or not api.strip():
            continue
        label = str(item.get("name") or "").strip()
        site = {
            "key": _slug(label, index),
            "name": label or f"上游站点{index + 1}",
            "type": 0,
            "api": api.strip(),
            "searchable": 1,
            "quickSearch": 1,
            "filterable": 1,
            "changeable": 1,
            "_from": name,
        }
        detail = str(item.get("detail") or "").strip()
        if detail:
            site["_note"] = detail[:120]
        sites.append(site)
    return {"sites": sites}


ADAPTERS = {"lunatv": from_lunatv}


def to_sections(data, fmt: str, name: str) -> dict:
    if fmt in ADAPTERS:
        return ADAPTERS[fmt](data, name)
    if fmt == "tvbox":
        return data
    raise ValueError(f"unknown format: {fmt}")


# ---------------------------------------------------------------------- merging


def _tag(incoming: dict, name: str) -> dict:
    for section in SECTIONS:
        for item in incoming.get(section, []) or []:
            if isinstance(item, dict):
                item.setdefault("_from", name)
    return incoming


def _drop_previous(cfg: dict, name: str) -> int:
    """`mode: replace` — forget what we adopted from this upstream last run."""
    removed = 0
    for section in SECTIONS:
        before = cfg.get(section) or []
        after = [i for i in before if not (isinstance(i, dict) and i.get("_from") == name)]
        removed += len(before) - len(after)
        cfg[section] = after
    return removed


# ------------------------------------------------------------------------- main


def load_upstreams() -> list:
    data = load_json(UPSTREAMS_PATH, None)
    if not isinstance(data, dict):
        return []
    ups = data.get("upstreams")
    if not isinstance(ups, list):
        return []
    return [u for u in ups if isinstance(u, dict)]


def sync_one(cfg: dict, up: dict) -> dict:
    """Pull one upstream into `cfg`. Returns a status record, never raises."""
    name = str(up.get("name") or up.get("url") or "?")
    url = up.get("url")
    record = {"name": name, "url": url if is_url(url) else "", "ok": False,
              "format": str(up.get("format") or "auto"), "error": None, "stats": None}

    if not record["url"]:
        record["error"] = "missing url"
        return record

    text, error = fetch_text(record["url"], int(up.get("timeout", 20) or 20))
    if error:
        record["error"] = error
        return record

    try:
        data = parse_loose(_unwrap(text))
    except SystemExit as exc:
        record["error"] = str(exc)
        return record

    fmt = record["format"].lower()
    if fmt == "auto":
        fmt = detect_format(data)
        record["format"] = fmt
    if fmt == "unknown":
        record["error"] = "not a TVBox sites list nor an api_site list"
        return record

    try:
        incoming = to_sections(data, fmt, name)
    except ValueError as exc:
        record["error"] = str(exc)
        return record

    if not any(incoming.get(s) for s in SECTIONS):
        record["error"] = "no adoptable entries"
        return record

    if str(up.get("mode", "merge") or "merge").lower() == "replace":
        _drop_previous(cfg, name)

    _tag(incoming, name)
    stats = merge(cfg, incoming, bool(up.get("spider")))
    record["ok"] = True
    record["stats"] = stats
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description="Pull upstream 接口 into the local candidate pool.")
    parser.add_argument("--dry-run", action="store_true", help="只打印统计，不写入")
    parser.add_argument("--only", default="", help="只同步指定 name 的上游")
    parser.add_argument("--strict", action="store_true", help="全部失败时返回非 0")
    args = parser.parse_args(argv)

    upstreams = load_upstreams()
    if not upstreams:
        print(f"no upstreams configured ({UPSTREAMS_PATH}) - nothing to do")
        return 0

    cfg = load_config()
    records, ran, failed = [], 0, 0

    for up in upstreams:
        name = str(up.get("name") or up.get("url") or "?")
        if not up.get("enabled", True):
            print(f"[skip] {name}: disabled")
            continue
        if args.only and args.only != name:
            continue
        ran += 1
        rec = sync_one(cfg, up)
        records.append(rec)

        if rec["ok"]:
            s = rec["stats"]
            parts = "  ".join(
                f"{sec} +{s[sec]['added']} dup {s[sec]['kept']} skip {s[sec]['skipped']}"
                for sec in SECTIONS
            )
            print(f"[ok  ] {name} ({rec['format']})  {parts}")
        else:
            failed += 1
            print(f"[fail] {name}: {rec['error']}")

    if not ran:
        print("nothing selected")
        return 0

    stamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

    if args.dry_run:
        print("dry-run: config not modified")
        return 1 if (args.strict and failed == ran) else 0

    save_json(CONFIG_PATH, cfg)
    save_json(SYNC_STATE, {"generated_at": stamp, "failed": failed, "upstreams": records})
    print(f"updated {CONFIG_PATH}   ({ran - failed}/{ran} upstreams ok)")
    return 1 if (args.strict and failed == ran) else 0


if __name__ == "__main__":
    raise SystemExit(main())
