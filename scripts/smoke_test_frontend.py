#!/usr/bin/env python3
"""Lightweight frontend smoke test for docs/index.html and generated device assets."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS_DIR = ROOT / "docs"
INDEX_HTML = DOCS_DIR / "index.html"
CATEGORIES_JS = DOCS_DIR / "devices" / "categories.js"
FIRMWARE_JS = DOCS_DIR / "devices" / "index.js"
CONFIG_JS = DOCS_DIR / "devices" / "config.js"


def fail(message: str) -> None:
    print(f"Frontend smoke test failed: {message}", file=sys.stderr)
    raise SystemExit(1)


def read_text(path: Path) -> str:
    if not path.exists():
        fail(f"missing file: {path}")
    return path.read_text(encoding="utf-8")


def extract_const_json(js_text: str, const_name: str):
    marker = f"const {const_name} = "
    idx = js_text.find(marker)
    if idx < 0:
        fail(f"{const_name} declaration not found")

    rest = js_text[idx + len(marker):].strip()
    if not rest.endswith(";"):
        fail(f"{const_name} declaration is missing trailing semicolon")

    payload = rest[:-1].strip()
    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        fail(f"{const_name} payload is not valid JSON: {exc}")


def check_index_script_refs(html: str) -> None:
    script_refs = set(re.findall(r'<script\s+src="([^"]+)"', html))
    required = {
        "devices/categories.js",
        "devices/index.js",
        "devices/config.js",
    }
    missing = required - script_refs
    if missing:
        fail(f"docs/index.html missing script refs: {', '.join(sorted(missing))}")


def main() -> int:
    html = read_text(INDEX_HTML)
    categories_js = read_text(CATEGORIES_JS)
    firmware_js = read_text(FIRMWARE_JS)
    config_js = read_text(CONFIG_JS)

    check_index_script_refs(html)

    categories = extract_const_json(categories_js, "CATEGORIES")
    firmware_index = extract_const_json(firmware_js, "FIRMWARE_INDEX")
    tracker_config = extract_const_json(config_js, "TRACKER_CONFIG")

    if not isinstance(categories, dict) or not categories:
        fail("CATEGORIES must be a non-empty object")
    if not isinstance(firmware_index, dict):
        fail("FIRMWARE_INDEX must be an object")
    if not isinstance(tracker_config, dict):
        fail("TRACKER_CONFIG must be an object")

    required_config_keys = {
        "generated_at_utc",
        "refresh_workflow_url",
        "device_download_pages",
        "device_source_types",
        "source_sync_status",
    }
    missing_config = required_config_keys - set(tracker_config.keys())
    if missing_config:
        fail(f"TRACKER_CONFIG missing keys: {', '.join(sorted(missing_config))}")

    tracked_devices: set[str] = set()
    for category_id, category in categories.items():
        if not isinstance(category, dict):
            fail(f"category '{category_id}' is not an object")
        if "title" not in category or "devices" not in category:
            fail(f"category '{category_id}' missing title/devices")
        devices = category.get("devices")
        if not isinstance(devices, dict):
            fail(f"category '{category_id}' devices must be an object")
        tracked_devices.update(str(device_id) for device_id in devices.keys())

    if not tracked_devices:
        fail("no tracked devices found in CATEGORIES")

    download_pages = tracker_config.get("device_download_pages")
    source_types = tracker_config.get("device_source_types")
    if not isinstance(download_pages, dict) or not isinstance(source_types, dict):
        fail("TRACKER_CONFIG device maps must be objects")

    for device_id in sorted(tracked_devices):
        if device_id in firmware_index:
            entry = firmware_index.get(device_id)
            if not isinstance(entry, dict):
                fail(f"device '{device_id}' firmware entry must be an object")
            releases = entry.get("releases")
            if not isinstance(releases, list):
                fail(f"device '{device_id}' firmware entry missing releases list")
        if device_id not in download_pages:
            fail(f"device '{device_id}' missing in TRACKER_CONFIG.device_download_pages")
        if device_id not in source_types:
            fail(f"device '{device_id}' missing in TRACKER_CONFIG.device_source_types")

    print("Frontend smoke test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
