#!/usr/bin/env python3
"""Generate browser-ready firmware index assets from data/devices.json."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from sources.common import version_sort_key

ROOT = Path(__file__).resolve().parents[1]
DATA_FILE = ROOT / "data" / "devices.json"
OUTPUT_DIR = ROOT / "docs" / "devices"
SUMMARY_FILE = ROOT / "docs" / "FIRMWARE_SUMMARY.md"

def build_device_download_pages(device_sources: dict) -> dict:
    pages = {}
    for device_id, source in device_sources.items():
        if not isinstance(source, dict):
            continue
        source_type = source.get("type")
        if source_type in {"dji_downloads", "godox_listing", "tplink_downloads"}:
            pages[device_id] = source.get("url", "")
            continue
        if source_type == "sony_cscs":
            if source.get("page_url"):
                pages[device_id] = source.get("page_url", "")
                continue
            mdl = source.get("mdl", "")
            lang = source.get("lang", "en")
            area = source.get("area", "us")
            pages[device_id] = (
                f"https://support.d-imaging.sony.co.jp/www/cscs/firm/?mdl={mdl}&lang={lang}&area={area}"
                if mdl
                else ""
            )
            continue
        if source_type == "static":
            pages[device_id] = source.get("page_url", "")
            continue
        if source_type in {"apple_support", "atomos_support", "bambu_wiki"}:
            pages[device_id] = source.get("page_url", "") or source.get("url", "")
            continue
        pages[device_id] = ""
    return pages


def build_device_source_types(device_sources: dict) -> dict:
    out = {}
    for device_id, source in device_sources.items():
        if not isinstance(source, dict):
            continue
        out[device_id] = str(source.get("type") or "")
    return out


def get_latest_active_release(releases: list[dict]) -> dict | None:
    active = [r for r in releases if isinstance(r, dict) and r.get("active") is True]
    if not active:
        return None
    active.sort(
        key=lambda r: (version_sort_key(str(r.get("version") or "")), str(r.get("released_time") or "")),
        reverse=True,
    )
    return active[0]


def age_days(released_time: str) -> int | None:
    try:
        dt = datetime.fromisoformat(released_time)
    except ValueError:
        return None
    now = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    rel = dt.replace(tzinfo=timezone.utc)
    return max(0, (now - rel).days)


def format_age(days: int | None) -> str:
    if days is None:
        return "-"
    if days == 0:
        return "today"
    if days == 1:
        return "1 day"
    if days < 30:
        return f"{days} days"
    months = days // 30
    if months < 12:
        return f"{months} month{'s' if months != 1 else ''}"
    years = days // 365
    return f"{years} year{'s' if years != 1 else ''}"


def build_issue_map(sync_status: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for issue in sync_status.get("issues", []):
        if not isinstance(issue, dict):
            continue
        device_id = str(issue.get("device_id") or "")
        if not device_id:
            continue
        reason = str(issue.get("reason") or issue.get("status") or "issue")
        out[device_id] = reason
    return out


def generate_summary_markdown(payload: dict) -> str:
    categories = payload.get("categories", {})
    firmware_index = payload.get("firmware_index", {})
    sources = payload.get("sources", {})
    device_sources = sources.get("device_sources", {})
    sync_status = sources.get("sync_status", {})
    issue_map = build_issue_map(sync_status)

    rows: list[dict[str, str]] = []
    for category in categories.values():
        category_title = str(category.get("title") or "")
        devices = category.get("devices", {})
        if not isinstance(devices, dict):
            continue
        for device_id, device_name in devices.items():
            source = device_sources.get(device_id, {})
            source_type = str(source.get("type") or "")
            if source_type == "static":
                continue
            releases = (
                firmware_index.get(device_id, {}).get("releases", [])
                if isinstance(firmware_index.get(device_id, {}), dict)
                else []
            )
            latest = get_latest_active_release(releases if isinstance(releases, list) else [])
            if latest:
                version = str(latest.get("version") or "-")
                released = str(latest.get("released_time") or "-")
                age = format_age(age_days(released))
            else:
                version = "-"
                released = "-"
                age = "-"
            status = issue_map.get(device_id, "ok")
            rows.append(
                {
                    "category": category_title,
                    "device": str(device_name),
                    "version": version,
                    "released": released,
                    "age": age,
                    "source": source_type or "-",
                    "status": status,
                }
            )

    rows.sort(key=lambda r: (r["category"].lower(), r["device"].lower()))

    lines = [
        "# Firmware Summary",
        "",
        f"Generated (UTC): {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}",
        "",
        "| Category | Device | Latest | Released | Age | Source | Status |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        lines.append(
            f"| {row['category']} | {row['device']} | {row['version']} | {row['released']} | {row['age']} | {row['source']} | {row['status']} |"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    payload = json.loads(DATA_FILE.read_text(encoding="utf-8"))

    categories = payload.get("categories", {})
    firmware_index = payload.get("firmware_index", {})
    sources = payload.get("sources", {})
    refresh_workflow_url = sources.get("refresh_workflow_url", "")
    device_sources = sources.get("device_sources", {})
    sync_status = sources.get("sync_status", {})
    device_download_pages = build_device_download_pages(device_sources)
    device_source_types = build_device_source_types(device_sources)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    categories_js = "// Auto-generated categories index\n\nconst CATEGORIES = "
    categories_js += json.dumps(categories, ensure_ascii=True, separators=(",", ":"))
    categories_js += ";\n"

    index_js = "// Auto-generated firmware index\n\nconst FIRMWARE_INDEX = "
    index_js += json.dumps(firmware_index, ensure_ascii=True, separators=(",", ":"))
    index_js += ";\n"

    config_js = "// Auto-generated UI config\n\nconst TRACKER_CONFIG = "
    config_js += json.dumps(
        {
            "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "refresh_workflow_url": refresh_workflow_url,
            "device_download_pages": device_download_pages,
            "device_source_types": device_source_types,
            "source_sync_status": sync_status,
        },
        ensure_ascii=True,
        separators=(",", ":"),
    )
    config_js += ";\n"

    (OUTPUT_DIR / "categories.js").write_text(categories_js, encoding="utf-8")
    (OUTPUT_DIR / "index.js").write_text(index_js, encoding="utf-8")
    (OUTPUT_DIR / "config.js").write_text(config_js, encoding="utf-8")
    SUMMARY_FILE.write_text(generate_summary_markdown(payload), encoding="utf-8")

    print(
        f"Generated {(OUTPUT_DIR / 'categories.js')}, {(OUTPUT_DIR / 'index.js')}, {(OUTPUT_DIR / 'config.js')}, and {SUMMARY_FILE}"
    )


if __name__ == "__main__":
    main()
