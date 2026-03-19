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


def is_http_404_error(exc: Exception) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return int(exc.code) == 404
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        return isinstance(reason, urllib.error.HTTPError) and int(reason.code) == 404
    return False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync tracked device firmware from official vendor pages")
    parser.add_argument("--dry-run", action="store_true", help="Show changes without writing file")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose source diagnostics")
    parser.add_argument(
        "--debug-device",
        action="append",
        default=[],
        help="Device ID to emit detailed diagnostics for (can be repeated)",
    )
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


def get_latest_active_release(releases: list[dict[str, Any]]) -> dict[str, Any] | None:
    active = [r for r in releases if isinstance(r, dict) and bool(r.get("active"))]
    if not active:
        return None
    active.sort(key=lambda item: str(item.get("released_time") or ""), reverse=True)
    return active[0]


def parse_iso_date(date_text: str) -> datetime | None:
    clean = date_text.strip()
    if clean.endswith("Z"):
        clean = f"{clean[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(clean)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def should_accept_release_update(
    current_releases: list[dict[str, Any]],
    new_releases: list[dict[str, Any]],
    source: dict[str, Any] | None,
) -> tuple[bool, str]:
    if not current_releases:
        return True, ""
    if not new_releases:
        return False, "guardrail: new parser output has no releases"
    if isinstance(source, dict) and bool(source.get("allow_regression")):
        return True, ""

    current_latest = get_latest_active_release(current_releases)
    new_latest = get_latest_active_release(new_releases)
    if not current_latest or not new_latest:
        return True, ""

    current_version = str(current_latest.get("version") or "").strip()
    new_version = str(new_latest.get("version") or "").strip()
    current_date = parse_iso_date(str(current_latest.get("released_time") or ""))
    new_date = parse_iso_date(str(new_latest.get("released_time") or ""))
    if (
        isinstance(source, dict)
        and str(source.get("type") or "") == "apple_support"
        and current_version
        and new_version
        and current_version == new_version
        and current_date
        and new_date
        and new_date > current_date
    ):
        return False, "guardrail: apple latest version unchanged but release date moved later"
    if current_date and new_date and new_date < current_date:
        return (
            False,
            "guardrail: parser returned an older latest release date than current stored data",
        )
    return True, ""


def process_device(
    device_id: str,
    device_name: str,
    source: dict[str, Any] | None,
    timeout: int,
    verbose: bool = False,
    debug_devices: set[str] | None = None,
) -> dict[str, Any]:
    debug_enabled = verbose or (bool(debug_devices) and device_id in (debug_devices or set()))
    debug_prefix = f"[debug {device_id}]"
    if not isinstance(source, dict):
        if debug_enabled:
            print(f"{debug_prefix} no source configured")
        return {
            "device_id": device_id,
            "status": "missing_source",
            "reason": "no source configured",
            "releases": [],
            "source_type": "",
            "vendor": "unknown",
        }

    attempts: list[dict[str, Any]] = [source]
    fallback_sources = source.get("fallback_sources")
    if isinstance(fallback_sources, list):
        for item in fallback_sources:
            if isinstance(item, dict):
                attempts.append(item)
    else:
        fallback_source = source.get("fallback_source")
        if isinstance(fallback_source, dict):
            attempts.append(fallback_source)

    last_result: dict[str, Any] | None = None
    for idx, candidate in enumerate(attempts):
        source_type = str(candidate.get("type") or "")
        vendor = SOURCE_VENDOR.get(source_type, "unknown")
        used_fallback = idx > 0
        candidate_url = str(candidate.get("url") or candidate.get("page_url") or "")
        candidate_for_run = dict(candidate)
        if debug_enabled:
            candidate_for_run["_debug"] = True
            candidate_for_run["_debug_prefix"] = debug_prefix
            print(
                f"{debug_prefix} attempt={idx + 1}/{len(attempts)} "
                f"type={source_type} fallback={used_fallback} url={candidate_url}"
            )
        try:
            releases = normalize_releases(sync_device(device_name, candidate_for_run, timeout))
        except Exception as exc:  # noqa: BLE001
            if debug_enabled:
                print(f"{debug_prefix} attempt failed: {exc}")
            if is_http_404_error(exc) and bool(candidate.get("treat_404_as_empty")):
                status = "ok_empty" if bool(candidate.get("allow_empty")) else "no_entries"
                reason = "404 treated as empty result by source policy"
                last_result = {
                    "device_id": device_id,
                    "status": status,
                    "reason": reason,
                    "releases": [],
                    "source_type": source_type,
                    "vendor": vendor,
                    "used_fallback": used_fallback,
                    "used_source": candidate,
                    "used_source_index": idx,
                }
                if status == "ok_empty" and idx >= len(attempts) - 1:
                    if debug_enabled:
                        print(f"{debug_prefix} final status={status} reason={reason}")
                    return last_result
                continue
            status = "transient_error" if is_transient_network_error(exc) else "error"
            last_result = {
                "device_id": device_id,
                "status": status,
                "reason": str(exc),
                "releases": [],
                "source_type": source_type,
                "vendor": vendor,
                "used_fallback": used_fallback,
                "used_source": candidate,
                "used_source_index": idx,
            }
            continue

        if not releases:
            status = "ok_empty" if bool(candidate.get("allow_empty")) else "no_entries"
            reason = "no firmware entries published yet" if status == "ok_empty" else "no firmware entries parsed"
            last_result = {
                "device_id": device_id,
                "status": status,
                "reason": reason,
                "releases": [],
                "source_type": source_type,
                "vendor": vendor,
                "used_fallback": used_fallback,
                "used_source": candidate,
                "used_source_index": idx,
            }
            # If a fallback source exists, do not stop on an empty primary result.
            if status == "ok_empty" and idx >= len(attempts) - 1:
                if debug_enabled:
                    print(f"{debug_prefix} final status={status} reason={reason}")
                return last_result
            continue

        if debug_enabled:
            latest = releases[0] if releases else {}
            print(
                f"{debug_prefix} parsed releases={len(releases)} "
                f"latest={latest.get('version')}@{latest.get('released_time')}"
            )
        return {
            "device_id": device_id,
            "status": "ok",
            "reason": "fallback source used" if used_fallback else "",
            "releases": releases,
            "source_type": source_type,
            "vendor": vendor,
            "used_fallback": used_fallback,
            "used_source": candidate,
            "used_source_index": idx,
        }

    if last_result:
        if debug_enabled:
            print(f"{debug_prefix} final status={last_result.get('status')} reason={last_result.get('reason')}")
        return last_result
    return {
        "device_id": device_id,
        "status": "error",
        "reason": "unknown source processing error",
        "releases": [],
        "source_type": "",
        "vendor": "unknown",
        "used_fallback": False,
        "used_source": source,
        "used_source_index": 0,
    }


def build_sync_status(results: list[dict[str, Any]], prior_sync_status: dict[str, Any] | None = None) -> dict[str, Any]:
    issues = []
    transient_issues = []
    prior_streaks = prior_sync_status.get("issue_streaks", {}) if isinstance(prior_sync_status, dict) else {}
    prior_device_health = prior_sync_status.get("device_health", {}) if isinstance(prior_sync_status, dict) else {}
    prior_vendor_health = prior_sync_status.get("vendor_health", {}) if isinstance(prior_sync_status, dict) else {}
    issue_streaks: dict[str, int] = {}
    device_health: dict[str, dict[str, Any]] = {}
    health_counts = {
        "ok": 0,
        "ok_empty": 0,
        "no_entries": 0,
        "error": 0,
        "transient_error": 0,
        "guardrail_rejected": 0,
        "missing_source": 0,
    }
    by_vendor: dict[str, dict[str, Any]] = {}
    run_now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    for result in results:
        status = str(result.get("status") or "")
        vendor = str(result.get("vendor") or "unknown")
        device_id = str(result.get("device_id") or "")
        reason = str(result.get("reason") or "")

        if status in health_counts:
            health_counts[status] += 1

        vendor_entry = by_vendor.setdefault(vendor, {"ok": 0, "issues": [], "transient_count": 0})
        prev_device = prior_device_health.get(device_id, {}) if isinstance(prior_device_health, dict) else {}
        if status in {"ok", "ok_empty"}:
            vendor_entry["ok"] += 1
            device_health[device_id] = {
                "vendor": vendor,
                "status": status,
                "last_success_utc": run_now,
                "consecutive_failures": 0,
                "last_error_type": "",
                "last_error_reason": "",
            }
        elif status == "transient_error":
            vendor_entry["transient_count"] += 1
            transient_issues.append({"vendor": vendor, "device_id": device_id, "status": status, "reason": reason})
            prev_fail = int(prev_device.get("consecutive_failures", 0) or 0)
            device_health[device_id] = {
                "vendor": vendor,
                "status": status,
                "last_success_utc": str(prev_device.get("last_success_utc") or ""),
                "consecutive_failures": prev_fail + 1,
                "last_error_type": status,
                "last_error_reason": reason,
            }
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
            prev_fail = int(prev_device.get("consecutive_failures", 0) or 0)
            device_health[device_id] = {
                "vendor": vendor,
                "status": status,
                "last_success_utc": str(prev_device.get("last_success_utc") or ""),
                "consecutive_failures": prev_fail + 1,
                "last_error_type": status,
                "last_error_reason": reason,
            }
        else:
            prev_fail = int(prev_device.get("consecutive_failures", 0) or 0)
            device_health[device_id] = {
                "vendor": vendor,
                "status": status,
                "last_success_utc": str(prev_device.get("last_success_utc") or ""),
                "consecutive_failures": prev_fail + 1,
                "last_error_type": status,
                "last_error_reason": reason,
            }

    vendor_health = {}
    for vendor, entry in by_vendor.items():
        if vendor == "static":
            continue
        prev_vendor = prior_vendor_health.get(vendor, {}) if isinstance(prior_vendor_health, dict) else {}
        issue_count = len(entry["issues"])
        transient_count = int(entry.get("transient_count", 0))
        had_failure = issue_count > 0 or transient_count > 0
        prev_vendor_failures = int(prev_vendor.get("consecutive_failures", 0) or 0)
        vendor_health[vendor] = {
            "status": "issue" if issue_count else ("transient_issue" if transient_count else "ok"),
            "ok_count": entry["ok"],
            "issues": entry["issues"],
            "transient_count": transient_count,
            "last_success_utc": str(prev_vendor.get("last_success_utc") or "") if had_failure else run_now,
            "consecutive_failures": (prev_vendor_failures + 1) if had_failure else 0,
            "last_error_type": (
                entry["issues"][0]["status"] if issue_count else ("transient_error" if transient_count else "")
            ),
        }

    max_issue_streak_days = max(issue_streaks.values(), default=0)

    return {
        "last_run_utc": run_now,
        "health_counts": health_counts,
        "vendor_health": vendor_health,
        "device_health": device_health,
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
    debug_devices = set(str(device_id).strip() for device_id in (args.debug_device or []) if str(device_id).strip())

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
                    args.verbose,
                    debug_devices,
                )
            )

        results = [future.result() for future in concurrent.futures.as_completed(futures)]

    processed_results: list[dict[str, Any]] = []
    for result in sorted(results, key=lambda item: str(item.get("device_id", ""))):
        device_id = str(result["device_id"])
        status = str(result["status"])
        source_type = str(result.get("source_type") or "")
        reason = str(result.get("reason") or "")
        releases = result.get("releases") or []
        source = device_sources.get(device_id)
        effective_source = result.get("used_source")
        if not isinstance(effective_source, dict):
            effective_source = source if isinstance(source, dict) else None

        current = normalize_releases(
            firmware_index.get(device_id, {}).get("releases", [])
            if isinstance(firmware_index.get(device_id, {}), dict)
            else []
        )

        if status in {"ok", "ok_empty"}:
            if releases != current:
                accepted, guard_reason = should_accept_release_update(
                    current,
                    releases,
                    effective_source,
                )
                if accepted:
                    firmware_index[device_id] = {"releases": releases}
                    updated_devices.append(device_id)
                else:
                    result["status"] = "guardrail_rejected"
                    result["reason"] = guard_reason
                    status = "guardrail_rejected"
                    reason = guard_reason
            else:
                processed_results.append(result)
                continue

        processed_results.append(result)
        if status not in {"ok", "ok_empty"}:
            skipped_devices.append(f"{device_id} ({reason})")
        if source_type != "static" and current and status in {"no_entries", "error", "missing_source", "guardrail_rejected"}:
            regressions.append(f"{device_id} ({status}: {reason})")

    prior_sync_status = sources_block.get("sync_status")
    sync_status = build_sync_status(processed_results, prior_sync_status if isinstance(prior_sync_status, dict) else None)
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
            for key in ["ok", "ok_empty", "no_entries", "error", "transient_error", "guardrail_rejected", "missing_source"]
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
