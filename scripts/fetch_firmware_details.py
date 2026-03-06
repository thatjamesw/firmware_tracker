#!/usr/bin/env python3
"""Sync tracked firmware releases from official vendor sources into data/devices.json."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import socket
import sys
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jsonschema import ValidationError, validate as jsonschema_validate

from sources import SOURCE_VENDOR, SYNC_HANDLERS
from sources import apple as apple_source
from sources import atomos as atomos_source
from sources import bambu as bambu_source
from sources.common import configure_fetch, normalize_releases

ROOT = Path(__file__).resolve().parents[1]
DATA_FILE = ROOT / "data" / "devices.json"
SCHEMA_FILE = ROOT / "data" / "devices.schema.json"


def is_transient_network_error(exc: Exception) -> bool:
    """Detect temporary connectivity failures that should not be treated as persistent source issues."""
    text = str(exc).lower()
    transient_markers = (
        "temporary failure in name resolution",
        "name or service not known",
        "nodename nor servname provided",
        "connection timed out",
        "timed out",
        "temporary failure",
    )
    if any(marker in text for marker in transient_markers):
        return True
    if isinstance(exc, TimeoutError):
        return True
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        if isinstance(reason, TimeoutError | socket.gaierror):
            return True
        reason_text = str(reason).lower()
        return any(marker in reason_text for marker in transient_markers)
    return False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync tracked device firmware from official vendor pages")
    parser.add_argument("--dry-run", action="store_true", help="Show changes without writing file")
    parser.add_argument("--timeout", type=int, default=30, help="HTTP timeout seconds")
    parser.add_argument("--max-workers", type=int, default=8, help="Max parallel device workers")
    parser.add_argument("--retries", type=int, default=3, help="HTTP retries per request")
    parser.add_argument("--retry-backoff", type=float, default=1.5, help="Exponential backoff base seconds")
    parser.add_argument(
        "--fail-on-regression",
        action="store_true",
        help="Fail when a previously healthy dynamic source stops parsing",
    )
    return parser.parse_args()


def validate_payload_schema(payload: dict[str, Any]) -> None:
    schema = json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))
    jsonschema_validate(instance=payload, schema=schema)


def list_tracked_devices(payload: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for category in payload.get("categories", {}).values():
        for device_id, device_name in category.get("devices", {}).items():
            out[device_id] = device_name
    return out


def sync_device(device_name: str, source: dict[str, Any], timeout: int) -> list[dict[str, Any]]:
    source_type = str(source.get("type") or "")
    handler = SYNC_HANDLERS.get(source_type)
    if not handler:
        return []
    return handler(device_name, source, timeout) if source_type == "dji_downloads" else handler(source, timeout)


def process_device(
    device_id: str,
    device_name: str,
    source: dict[str, Any] | None,
    timeout: int,
) -> dict[str, Any]:
    if not isinstance(source, dict):
        return {
            "device_id": device_id,
            "status": "missing_source",
            "reason": "no source configured",
            "releases": [],
            "source_type": "",
            "vendor": "unknown",
        }

    source_type = str(source.get("type") or "")
    vendor = SOURCE_VENDOR.get(source_type, "unknown")
    try:
        releases = normalize_releases(sync_device(device_name, source, timeout))
    except Exception as exc:  # noqa: BLE001
        status = "transient_error" if is_transient_network_error(exc) else "error"
        return {
            "device_id": device_id,
            "status": status,
            "reason": str(exc),
            "releases": [],
            "source_type": source_type,
            "vendor": vendor,
        }

    if not releases:
        if bool(source.get("allow_empty")):
            return {
                "device_id": device_id,
                "status": "ok_empty",
                "reason": "no firmware entries published yet",
                "releases": [],
                "source_type": source_type,
                "vendor": vendor,
            }
        return {
            "device_id": device_id,
            "status": "no_entries",
            "reason": "no firmware entries parsed",
            "releases": [],
            "source_type": source_type,
            "vendor": vendor,
        }

    return {
        "device_id": device_id,
        "status": "ok",
        "reason": "",
        "releases": releases,
        "source_type": source_type,
        "vendor": vendor,
    }


def build_sync_status(results: list[dict[str, Any]], prior_sync_status: dict[str, Any] | None = None) -> dict[str, Any]:
    issues = []
    transient_issues = []
    prior_streaks = prior_sync_status.get("issue_streaks", {}) if isinstance(prior_sync_status, dict) else {}
    issue_streaks: dict[str, int] = {}
    health_counts = {
        "ok": 0,
        "ok_empty": 0,
        "no_entries": 0,
        "error": 0,
        "transient_error": 0,
        "missing_source": 0,
    }
    by_vendor: dict[str, dict[str, Any]] = {}

    for result in results:
        status = str(result.get("status") or "")
        vendor = str(result.get("vendor") or "unknown")
        device_id = str(result.get("device_id") or "")
        reason = str(result.get("reason") or "")

        if status in health_counts:
            health_counts[status] += 1

        vendor_entry = by_vendor.setdefault(vendor, {"ok": 0, "issues": []})
        if status in {"ok", "ok_empty"}:
            vendor_entry["ok"] += 1
        elif status == "transient_error":
            transient_issues.append({"vendor": vendor, "device_id": device_id, "status": status, "reason": reason})
        elif vendor != "static":
            vendor_entry["issues"].append({"device_id": device_id, "status": status, "reason": reason})
            streak_key = f"{vendor}:{device_id}"
            prev_streak = prior_streaks.get(streak_key, 0)
            try:
                prev_streak_num = int(prev_streak)
            except (TypeError, ValueError):
                prev_streak_num = 0
            streak_days = max(1, prev_streak_num + 1)
            issue_streaks[streak_key] = streak_days
            issues.append(
                {
                    "vendor": vendor,
                    "device_id": device_id,
                    "status": status,
                    "reason": reason,
                    "streak_days": streak_days,
                }
            )

    vendor_health = {}
    for vendor, entry in by_vendor.items():
        if vendor == "static":
            continue
        vendor_health[vendor] = {
            "status": "issue" if entry["issues"] else "ok",
            "ok_count": entry["ok"],
            "issues": entry["issues"],
        }

    max_issue_streak_days = max(issue_streaks.values(), default=0)

    return {
        "last_run_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "health_counts": health_counts,
        "vendor_health": vendor_health,
        "issues": issues,
        "transient_issues": transient_issues,
        "issue_streaks": issue_streaks,
        "max_issue_streak_days": max_issue_streak_days,
    }


def main() -> int:
    args = parse_args()
    payload = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    try:
        validate_payload_schema(payload)
    except ValidationError as exc:
        print(f"Schema validation failed: {exc.message}", file=sys.stderr)
        return 1

    configure_fetch(retries=args.retries, retry_backoff=args.retry_backoff)

    tracked = list_tracked_devices(payload)
    device_sources = payload.get("sources", {}).get("device_sources", {})
    firmware_index = payload.setdefault("firmware_index", {})
    sources_block = payload.setdefault("sources", {})

    updated_devices: list[str] = []
    skipped_devices: list[str] = []
    regressions: list[str] = []

    futures: list[concurrent.futures.Future] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.max_workers)) as pool:
        for device_id, device_name in tracked.items():
            futures.append(
                pool.submit(
                    process_device,
                    device_id,
                    device_name,
                    device_sources.get(device_id),
                    args.timeout,
                )
            )

        results = [future.result() for future in concurrent.futures.as_completed(futures)]

    for result in sorted(results, key=lambda item: str(item.get("device_id", ""))):
        device_id = str(result["device_id"])
        status = str(result["status"])
        source_type = str(result.get("source_type") or "")
        reason = str(result.get("reason") or "")
        releases = result.get("releases") or []

        current = normalize_releases(
            firmware_index.get(device_id, {}).get("releases", [])
            if isinstance(firmware_index.get(device_id, {}), dict)
            else []
        )

        if status in {"ok", "ok_empty"}:
            if releases != current:
                firmware_index[device_id] = {"releases": releases}
                updated_devices.append(device_id)
            continue

        skipped_devices.append(f"{device_id} ({reason})")
        if source_type != "static" and current and status in {"no_entries", "error", "missing_source"}:
            regressions.append(f"{device_id} ({status}: {reason})")

    prior_sync_status = sources_block.get("sync_status")
    sync_status = build_sync_status(results, prior_sync_status if isinstance(prior_sync_status, dict) else None)
    sources_block["sync_status"] = sync_status

    if updated_devices:
        print("Updated devices:", ", ".join(updated_devices))
    else:
        print("No firmware updates found from configured official sources.")

    if skipped_devices:
        print("Skipped devices:")
        for item in skipped_devices:
            print(f"- {item}")

    counts = sync_status.get("health_counts", {})
    print(
        "Source health summary: "
        + ", ".join(
            f"{key}={counts.get(key, 0)}"
            for key in ["ok", "ok_empty", "no_entries", "error", "transient_error", "missing_source"]
        )
    )

    if sync_status.get("issues"):
        print("Source issues detected (app will continue using last known good data):")
        for issue in sync_status["issues"]:
            print(
                f"- {issue['vendor']}: {issue['device_id']} ({issue['status']}: {issue['reason']})"
            )
    if sync_status.get("transient_issues"):
        print("Transient source fetch issues detected (not surfaced as persistent UI source issues):")
        for issue in sync_status["transient_issues"]:
            print(
                f"- {issue['vendor']}: {issue['device_id']} ({issue['status']}: {issue['reason']})"
            )

    should_write = bool(updated_devices)
    if prior_sync_status != sync_status:
        should_write = True

    if should_write and not args.dry_run:
        DATA_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
        print(f"Wrote updates to {DATA_FILE}")

    if regressions and args.fail_on_regression:
        print("Regressions detected and --fail-on-regression is set.", file=sys.stderr)
        return 2

    return 0


# Backward-compatible exports used by tests.
sync_bambu_wiki = bambu_source.sync_bambu_wiki
sync_atomos_support = atomos_source.sync_atomos_support
sync_apple_support = apple_source.sync_apple_support


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"Sync failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
