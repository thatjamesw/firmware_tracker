from __future__ import annotations

import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

USER_AGENT = "firmware-tracker-sync/1.0 (+https://github.com/)"
FETCH_RETRIES = 3
FETCH_RETRY_BACKOFF = 1.5


def configure_fetch(retries: int, retry_backoff: float) -> None:
    global FETCH_RETRIES, FETCH_RETRY_BACKOFF
    FETCH_RETRIES = max(0, int(retries))
    FETCH_RETRY_BACKOFF = max(0.1, float(retry_backoff))


def fetch_bytes(url: str, timeout: int) -> bytes:
    host = ""
    try:
        host = urllib.parse.urlparse(url).netloc.lower()
    except Exception:  # noqa: BLE001
        host = ""

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/pdf,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    # DJI endpoints are region-routed; sending explicit region/lang cookies improves consistency in CI.
    if host.endswith("dji.com"):
        headers["Cookie"] = "region=GB; lang=en"

    req = urllib.request.Request(
        url,
        headers=headers,
    )
    last_exc: Exception | None = None
    for attempt in range(FETCH_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            # 404 means the resource is absent; retrying will not help.
            if int(exc.code) == 404:
                raise
            last_exc = exc
            if attempt >= FETCH_RETRIES:
                raise
            sleep_seconds = FETCH_RETRY_BACKOFF * (2**attempt)
            time.sleep(sleep_seconds)
        except (urllib.error.URLError, TimeoutError) as exc:
            last_exc = exc
            if attempt >= FETCH_RETRIES:
                raise
            sleep_seconds = FETCH_RETRY_BACKOFF * (2**attempt)
            time.sleep(sleep_seconds)
    if last_exc:
        raise last_exc
    raise RuntimeError(f"Failed to fetch URL: {url}")


def normalize_space(text: str) -> str:
    return " ".join(text.split())


def as_iso_date(date_text: str) -> str:
    clean = date_text.strip().replace(".", "-").replace("/", "-")
    match = re.search(r"(\d{4}-\d{2}-\d{2})", clean)
    return match.group(1) if match else ""


def parse_human_date_to_iso(date_text: str) -> str:
    clean = normalize_space(date_text).replace(",", "")
    for fmt in ("%d %b %Y", "%d %B %Y", "%B %d %Y", "%b %d %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(clean, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return as_iso_date(clean)


def normalize_release(raw: dict[str, Any]) -> dict[str, Any]:
    note = raw.get("release_note")
    if not isinstance(note, dict):
        note = {"en": ""}
    elif "en" not in note:
        note["en"] = ""

    return {
        "version": str(raw.get("version") or "").lstrip("Vv"),
        "released_time": str(raw.get("released_time") or ""),
        "release_note": {"en": str(note.get("en") or "")},
        "arb": raw.get("arb"),
        "active": bool(raw.get("active", False)),
    }


def normalize_releases(releases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = [normalize_release(r) for r in releases if isinstance(r, dict)]

    def sort_key(item: dict[str, Any]) -> tuple[int, str]:
        date_text = str(item.get("released_time") or "")
        try:
            dt = datetime.fromisoformat(date_text)
            ts = int(dt.replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            ts = -1
        return (ts, str(item.get("version") or ""))

    normalized.sort(key=sort_key, reverse=True)
    return normalized
