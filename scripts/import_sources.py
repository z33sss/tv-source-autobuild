#!/usr/bin/env python3
"""自动适配：把任意 TVBox / FongMi 接口“收养”进本地候选池。

    python scripts/import_sources.py <url-or-file> [--spider] [--dry-run]

The imported entries become candidates. The daily pipeline then deep-checks
them and only keeps the ones that still work.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import CONFIG_PATH, http_get, decode_text, is_url, load_config, save_json  # noqa: E402

SECTIONS = ("sites", "parses", "lives")
KEY_FIELDS = {"sites": "key", "parses": "name", "lives": "name"}


def read_source(src: str) -> dict:
    if is_url(src):
        resp = http_get(src, timeout=20)
        if not resp["ok"]:
            raise SystemExit(f"failed to fetch {src}: {resp['error']}")
        text = decode_text(resp["body"], resp.get("content_type", ""))
    else:
        path = Path(src)
        if not path.exists():
            raise SystemExit(f"not found: {path}")
        text = path.read_text(encoding="utf-8", errors="ignore")

    return parse_loose(text)


def parse_loose(text: str) -> dict:
    text = text.lstrip("\ufeff").strip()
    for attempt in (text, _tidy(text)):
        try:
            data = json.loads(attempt)
        except Exception:
            continue
        if isinstance(data, dict):
            return data
    raise SystemExit("source is not a JSON object (only TVBox-style 接口 are supported)")


def _tidy(text: str) -> str:
    """Best-effort cleanup for common formatter damage."""
    text = re.sub(r",\s*([}\]])", r"\1", text)          # trailing commas
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)    # block comments
    text = re.sub(r"(?m)^\s*//.*$", "", text)            # line comments
    return text


def merge(cfg: dict, incoming: dict, take_spider: bool):
    stats = {section: {"added": 0, "kept": 0, "skipped": 0} for section in SECTIONS}

    for section in SECTIONS:
        field = KEY_FIELDS[section]
        existing = cfg.setdefault(section, [])
        seen = {str(item.get(field) or item.get("name") or "") for item in existing}

        for item in incoming.get(section, []) or []:
            if not isinstance(item, dict):
                stats[section]["skipped"] += 1
                continue
            name = str(item.get(field) or "")
            if not name or not _has_target(section, item):
                stats[section]["skipped"] += 1
                continue
            if name in seen:
                stats[section]["kept"] += 1
                continue
            seen.add(name)
            existing.append(item)
            stats[section]["added"] += 1

    if take_spider and incoming.get("spider"):
        cfg.setdefault("app", {})["spider"] = incoming["spider"]

    return stats


def _has_target(section: str, item: dict) -> bool:
    if section == "sites":
        return any(is_url(item.get(f)) for f in ("api", "ext", "jar", "url")) or bool(
            isinstance(item.get("ext"), dict) and is_url((item["ext"] or {}).get("api"))
        )
    return is_url(item.get("url"))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Adopt sites from an existing 接口.")
    parser.add_argument("source", help="接口 URL 或本地 JSON 文件")
    parser.add_argument("--spider", action="store_true", help="同时采用源里的 spider jar 地址")
    parser.add_argument("--dry-run", action="store_true", help="只打印统计，不写入")
    args = parser.parse_args(argv)

    incoming = read_source(args.source)
    if not any(incoming.get(s) for s in SECTIONS):
        raise SystemExit("source contains no sites/parses/lives")

    cfg = load_config()
    stats = merge(cfg, incoming, args.spider)

    print(f"from: {args.source}")
    for section in SECTIONS:
        s = stats[section]
        print(f"  {section:8s} +{s['added']}  duplicate {s['kept']}  invalid {s['skipped']}")

    if args.dry_run:
        print("dry-run: config not modified")
        return 0

    save_json(CONFIG_PATH, cfg)
    print(f"updated {CONFIG_PATH}")
    print("run `python scripts/autobuild.py` to verify and rebuild dist/tvbox.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
