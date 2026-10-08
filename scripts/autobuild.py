#!/usr/bin/env python3
"""Daily pipeline: deep-check every source, update history, emit the 接口 JSON.

Usage:
    python scripts/autobuild.py            # check + build
    python scripts/autobuild.py --check    # check only
    python scripts/autobuild.py --build    # build only (reuse last report)

Outputs:
    state/history.json   rolling health stats per source
    state/report.json    raw results of this run
    dist/tvbox.json      the 接口 file the APK reads
    dist/report.md       human-readable summary (also used as job summary)
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import datetime as dt
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import DIST_DIR, STATE_DIR, load_config, load_json, save_json  # noqa: E402
import probe  # noqa: E402

HISTORY_PATH = STATE_DIR / "history.json"
REPORT_PATH = STATE_DIR / "report.json"
OUT_JSON = DIST_DIR / "tvbox.json"
OUT_MD = DIST_DIR / "report.md"

HISTORY_WINDOW = 10  # last N runs kept for reliability scoring


def entry_name(item: dict, field: str) -> str:
    """Stable identifier used both as the job key and as the merge key."""
    return str(item.get(field) or item.get("name") or item.get("key") or "")


# --------------------------------------------------------------------- history


def _blank():
    return {
        "ok": 0,
        "fail": 0,
        "consecutive_fail": 0,
        "last_ok": None,
        "last_fail": None,
        "last_latency_ms": 0,
        "last_items": 0,
        "recent": [],
    }


def _update(entry, result, now_iso):
    entry.setdefault("ok", 0)
    entry.setdefault("fail", 0)
    entry.setdefault("consecutive_fail", 0)
    entry.setdefault("recent", [])

    if result["kind"] == "none":
        # Unverifiable: never counts against it.
        entry["unverified"] = True
        return entry

    entry["unverified"] = False
    if result["ok"]:
        entry["ok"] += 1
        entry["consecutive_fail"] = 0
        entry["last_ok"] = now_iso
        entry["last_latency_ms"] = result["latency_ms"]
        entry["last_items"] = result["items"]
    else:
        entry["fail"] += 1
        entry["consecutive_fail"] = entry.get("consecutive_fail", 0) + 1
        entry["last_fail"] = now_iso

    entry["recent"].append(1 if result["ok"] else 0)
    entry["recent"] = entry["recent"][-HISTORY_WINDOW:]
    return entry


def _score(entry) -> float:
    """0-100+; higher = better. Used for ordering inside the output file."""
    if entry.get("unverified"):
        return 50.0

    latency = entry.get("last_latency_ms", 0)
    items = entry.get("last_items", 0)
    recent = entry.get("recent") or [0]

    score = 100.0
    score -= min(latency / 100.0, 30.0)          # slow endpoints lose up to 30
    score += min(items, 20) * 0.5                # richer catalogues gain up to 10
    score -= (recent.count(0) / len(recent)) * 25  # flaky endpoints lose up to 25
    score -= entry.get("consecutive_fail", 0) * 10
    return round(max(score, 0.0), 2)


def _status(entry, result, drop_after):
    if result["kind"] == "none":
        return "unverified"
    if result["ok"]:
        return "alive"
    if entry.get("consecutive_fail", 0) >= drop_after:
        return "dead"
    return "grace"


# ---------------------------------------------------------------------- checks


def run_checks(cfg):
    settings = cfg.get("settings", {})
    timeout = int(settings.get("timeout", 10))
    workers = int(settings.get("workers", 8))
    keyword = settings.get("keyword", "")
    parse_test_url = settings.get("parse_test_url", "")

    jobs = []

    for site in cfg.get("sites", []):
        key = "site:" + entry_name(site, "key")
        jobs.append((key, lambda s=site: probe.probe_site(s, {"keyword": keyword}, timeout)))

    for entry in cfg.get("parses", []):
        key = "parse:" + entry_name(entry, "name")
        jobs.append((key, lambda e=entry: probe.probe_parse(e, timeout, parse_test_url)))

    for entry in cfg.get("lives", []):
        key = "live:" + entry_name(entry, "name")
        jobs.append((key, lambda e=entry: probe.probe_live(e, timeout)))

    app = cfg.get("app", {})
    if app.get("spider"):
        jobs.append(("misc:spider", lambda: probe.probe_http(app["spider"], timeout, min_bytes=1024)))
    if app.get("wallpaper"):
        jobs.append(("misc:wallpaper", lambda: probe.probe_http(app["wallpaper"], timeout, min_bytes=100)))

    results = {}
    with futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(fn): key for key, fn in jobs}
        for fut in futures.as_completed(futs):
            key = futs[fut]
            try:
                results[key] = fut.result()
            except Exception as exc:
                results[key] = {
                    "ok": False, "kind": "error", "url": "",
                    "latency_ms": 0, "items": 0,
                    "detail": f"{type(exc).__name__}: {exc}",
                }
    return results


# ----------------------------------------------------------------------- build


def build_output(cfg, results, history, drop_after):
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

    buckets = {"site": [], "parse": [], "live": []}

    for key, result in results.items():
        kind, _, name = key.partition(":")
        if kind not in buckets:
            continue
        entry = history.setdefault(key, _blank())
        _update(entry, result, now)
        buckets[kind].append(
            {
                "key": key,
                "name": name,
                "result": result,
                "entry": entry,
                "status": _status(entry, result, drop_after),
                "score": _score(entry),
            }
        )

    def order(rows):
        rank = {"alive": 0, "unverified": 1, "grace": 2, "dead": 3}
        return sorted(rows, key=lambda r: (rank[r["status"]], -r["score"]))

    # Re-order the original config arrays so only surviving entries remain,
    # preserving each entry's original object shape (no extra fields leaked).
    def filter_keep(items, key_field, rows):
        """Emit config entries in ranked order, dropping dead ones."""
        by_name = {}
        for item in items:
            name = entry_name(item, key_field)
            if name and name not in by_name:
                by_name[name] = item

        out, seen = [], set()
        for row in rows:
            name = row["name"]
            if row["status"] == "dead" or not name or name in seen or name not in by_name:
                continue
            seen.add(name)
            # strip candidate-only annotations (keys starting with "_")
            item = by_name[name]
            out.append({k: v for k, v in item.items() if not k.startswith("_")})
        return out

    site_rows = order(buckets["site"])
    parse_rows = order(buckets["parse"])
    live_rows = order(buckets["live"])

    app = dict(cfg.get("app", {}))
    out = {
        key: value
        for key, value in app.items()
        if not key.startswith("_") and value not in (None, "", [], {})
    }
    out["sites"] = filter_keep(cfg.get("sites", []), "key", site_rows)
    out["parses"] = filter_keep(cfg.get("parses", []), "name", parse_rows)
    out["lives"] = filter_keep(cfg.get("lives", []), "name", live_rows)
    total = len(cfg.get("sites", []))
    stamp = now.replace("T", " ").replace("+00:00", " UTC")
    out["warning"] = f"源已自动更新 · {stamp} · 存活 {len(out['sites'])}/{total}"

    save_json(OUT_JSON, out)

    all_rows = site_rows + parse_rows + live_rows
    return out, all_rows, now


def write_report(out, rows, now, results):
    save_json(
        REPORT_PATH,
        {
            "generated_at": now,
            "counts": _counts(rows),
            "results": results,
        },
    )

    counts = _counts(rows)
    lines = [
        "# 影视源每日检测报告",
        "",
        f"- 生成时间：`{now}`",
        f"- 存活：**{counts['alive']}** / 暂存：**{counts['grace']}** / "
        f"已剔除：**{counts['dead']}** / 无法验证：**{counts['unverified']}**",
        f"- 输出文件：`dist/tvbox.json`（{len(out['sites'])} 站点，"
        f"{len(out['parses'])} 解析，{len(out['lives'])} 直播）",
        "",
        "| 状态 | 名称 | 得分 | 延迟 | 条目 | 说明 |",
        "| --- | --- | ---: | ---: | ---: | --- |",
    ]
    icon = {"alive": "✅", "grace": "🟡", "dead": "❌", "unverified": "❔"}
    for row in sorted(rows, key=lambda r: ({"alive": 0, "unverified": 1, "grace": 2, "dead": 3}[r["status"]], -r["score"])):
        r = row["result"]
        lines.append(
            f"| {icon[row['status']]} {row['status']} | {row['name']} | {row['score']} "
            f"| {r['latency_ms']}ms | {r['items']} | {r['detail'][:90]} |"
        )

    misc = sorted((k, v) for k, v in results.items() if k.startswith("misc:"))
    if misc:
        lines += ["", "### 公共资源（spider / 壁纸）", "", "| 状态 | 资源 | 说明 |", "| --- | --- | --- |"]
        for key, value in misc:
            lines.append(f"| {'✅' if value['ok'] else '❌'} | {key.split(':', 1)[1]} | {value['detail'][:90]} |")

    lines.append("")

    DIST_DIR.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")

    # job summary for GitHub Actions
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        Path(summary_path).write_text("\n".join(lines), encoding="utf-8")


def _counts(rows):
    counts = {"alive": 0, "grace": 0, "dead": 0, "unverified": 0}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return counts


# ------------------------------------------------------------------------ main


def main(argv=None):
    parser = argparse.ArgumentParser(description="Check sources and rebuild the 接口 file.")
    parser.add_argument("--check", action="store_true", help="only run probes")
    parser.add_argument("--build", action="store_true", help="only rebuild output from last report")
    args = parser.parse_args(argv)

    cfg = load_config()
    settings = cfg.get("settings", {})
    drop_after = int(settings.get("drop_after", 3))

    do_check = not args.build or args.check
    do_build = not args.check or args.build

    results = {}
    if do_check:
        print(f"probing {len(cfg.get('sites', []))} sites, "
              f"{len(cfg.get('parses', []))} parses, {len(cfg.get('lives', []))} lives ...")
        results = run_checks(cfg)
        save_json(REPORT_PATH, {"generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "results": results})

    if do_build:
        if not results:
            saved = load_json(REPORT_PATH, {})
            results = saved.get("results", {})
            if not results:
                raise SystemExit("no report available; run --check first")

        history = load_json(HISTORY_PATH, {}) or {}
        out, rows, now = build_output(cfg, results, history, drop_after)
        save_json(HISTORY_PATH, history)
        write_report(out, rows, now, results)

        counts = _counts(rows)
        print(f"alive={counts['alive']} grace={counts['grace']} "
              f"dead={counts['dead']} unverified={counts['unverified']}")
        print(f"wrote {OUT_JSON}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
