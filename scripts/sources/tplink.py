from __future__ import annotations

import re
from typing import Any

from .common import (
    fetch_bytes,
    html_to_text,
    make_release_candidate,
    normalize_space,
    parse_human_date_to_iso,
    resolve_release_candidates,
)


def sync_tplink_downloads(source: dict[str, Any], timeout: int) -> list[dict[str, Any]]:
    url = source["url"]
    model = str(source.get("model") or "").strip()
    hardware_version = str(source.get("hardware_version") or "").strip()

    html = fetch_bytes(url, timeout=timeout).decode("utf-8", errors="replace")
    text = html_to_text(html)
    escaped_model = re.escape(model) if model else r"[A-Za-z0-9 ]+"
    escaped_hw = re.escape(hardware_version) if hardware_version else r"V[0-9][0-9A-Za-z.]*"
    firmware_heading = re.compile(
        rf"({escaped_model}\([^)]+\)_{escaped_hw}_[0-9][0-9A-Za-z.\-]*\s+Build\s+[0-9]{{8}})",
        re.I,
    )
    candidates: list[dict[str, Any]] = []

    matches = list(firmware_heading.finditer(text))
    for idx, match in enumerate(matches):
        heading = normalize_space(match.group(1))
        next_start = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        block = text[match.end() : next_start]
        version_match = re.search(
            rf"_{escaped_hw}_([0-9][0-9A-Za-z.\-]*)\s+Build\s+([0-9]{{8}})",
            heading,
            re.I,
        )
        if not version_match:
            continue

        version = version_match.group(1)
        build = version_match.group(2)
        date_match = re.search(r"Published Date:\s*([0-9]{4}-[0-9]{2}-[0-9]{2})", block, re.I)
        release_date = parse_human_date_to_iso(date_match.group(1)) if date_match else ""

        note_parts: list[str] = []
        for section_name in (
            "New Features/Enhancements",
            "Bug Fixes",
            "Modifications and Bug Fixes",
            "Note",
        ):
            section_match = re.search(
                rf"{re.escape(section_name)}:\s*(.*?)(?=\s+(?:New Features/Enhancements|Bug Fixes|Modifications and Bug Fixes|Note|{escaped_model}\([^)]+\)_{escaped_hw}_[0-9]|##|\Z))",
                block,
                re.I | re.S,
            )
            if section_match:
                note_parts.append(f"{section_name}: {normalize_space(section_match.group(1))}")

        note = " ".join(note_parts).strip()
        if not note:
            note = "Official TP-Link firmware listing"
        note = f"{note} Build: {build}"

        candidates.append(
            make_release_candidate(
                version=version,
                released_time=release_date,
                note=note,
                evidence_type="tplink_download_item",
                evidence_text=heading,
                source_url=url,
                confidence=0.9 if release_date else 0.78,
                rank=86,
            )
        )

    return resolve_release_candidates(candidates, source)
