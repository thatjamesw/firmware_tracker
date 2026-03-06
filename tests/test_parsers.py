import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import fetch_firmware_details as ffd  # noqa: E402
from sources import apple as apple_source  # noqa: E402
from sources import atomos as atomos_source  # noqa: E402
from sources import bambu as bambu_source  # noqa: E402
from sources import dji as dji_source  # noqa: E402


class ParserTests(unittest.TestCase):
    def test_bambu_wiki_parser_extracts_latest(self) -> None:
        html = (FIXTURES_DIR / "bambu_wiki.html").read_text(encoding="utf-8")
        original_fetch = bambu_source.fetch_bytes
        try:
            bambu_source.fetch_bytes = lambda _url, timeout: html.encode("utf-8")
            releases = ffd.sync_bambu_wiki(
                {"url": "https://wiki.bambulab.com/en/p1/manual/p1p-firmware-release-history", "series": "P1"},
                timeout=5,
            )
        finally:
            bambu_source.fetch_bytes = original_fetch

        self.assertEqual(len(releases), 1)
        self.assertEqual(releases[0]["version"], "01.09.01.00")
        self.assertEqual(releases[0]["released_time"], "2026-01-14")

    def test_atomos_parser_extracts_current(self) -> None:
        html = (FIXTURES_DIR / "atomos_ninjav.html").read_text(encoding="utf-8")
        original_fetch = atomos_source.fetch_bytes
        try:
            atomos_source.fetch_bytes = lambda _url, timeout: html.encode("utf-8")
            releases = ffd.sync_atomos_support(
                {"url": "https://www.atomos.com/product-support/", "article_id": "NinjaVArticle"},
                timeout=5,
            )
        finally:
            atomos_source.fetch_bytes = original_fetch

        self.assertEqual(len(releases), 1)
        self.assertEqual(releases[0]["version"], "11.18.00")
        self.assertEqual(releases[0]["released_time"], "2025-11-01")

    def test_apple_ios_parser_extracts_latest_and_release_date(self) -> None:
        html = (FIXTURES_DIR / "apple_100100.html").read_text(encoding="utf-8")
        original_fetch = apple_source.fetch_bytes
        try:
            apple_source.fetch_bytes = lambda _url, timeout: html.encode("utf-8")
            releases = ffd.sync_apple_support(
                {"url": "https://support.apple.com/en-us/100100", "kind": "ios"},
                timeout=5,
            )
        finally:
            apple_source.fetch_bytes = original_fetch

        self.assertEqual(len(releases), 1)
        self.assertEqual(releases[0]["version"], "26.3.1")
        self.assertEqual(releases[0]["released_time"], "2026-03-04")

    def test_apple_airpods_parser_extracts_latest_and_published_date(self) -> None:
        html = (FIXTURES_DIR / "apple_106340.html").read_text(encoding="utf-8")
        original_fetch = apple_source.fetch_bytes
        try:
            apple_source.fetch_bytes = lambda _url, timeout: html.encode("utf-8")
            releases = ffd.sync_apple_support(
                {"url": "https://support.apple.com/106340", "kind": "airpods", "model": "AirPods Pro 3"},
                timeout=5,
            )
        finally:
            apple_source.fetch_bytes = original_fetch

        self.assertEqual(len(releases), 1)
        self.assertEqual(releases[0]["version"], "8B34")
        self.assertEqual(releases[0]["released_time"], "2026-01-13")

    def test_devices_json_conforms_to_schema(self) -> None:
        payload = json.loads((ROOT / "data" / "devices.json").read_text(encoding="utf-8"))
        # Raises on validation failure.
        ffd.validate_payload_schema(payload)

    def test_sync_status_tracks_issue_streak_days(self) -> None:
        prior = {"issue_streaks": {"dji:wa150": 2}}
        results = [
            {"device_id": "wa150", "status": "error", "reason": "HTTP Error 404", "vendor": "dji"},
            {"device_id": "wa520", "status": "ok", "reason": "", "vendor": "dji"},
        ]

        status = ffd.build_sync_status(results, prior)
        self.assertEqual(status["max_issue_streak_days"], 3)
        self.assertEqual(status["issue_streaks"]["dji:wa150"], 3)
        self.assertEqual(status["issues"][0]["streak_days"], 3)

    def test_dji_parser_falls_back_when_first_pdf_404s(self) -> None:
        html = """
        <li class="groups-download-item">
          <div class="groups-item-name">DJI Mini 5 Pro - Release Notes</div>
          <a href="https://example.com/RN/rn-404.pdf" class="download-file">Download</a>
        </li>
        <li class="groups-download-item">
          <div class="groups-item-name">DJI Mini 5 Pro - Release Notes</div>
          <a href="https://example.com/RN/rn-good.pdf" class="download-file">Download</a>
        </li>
        """

        calls: list[str] = []
        original_fetch = dji_source.fetch_bytes
        original_parse_pdf = dji_source.parse_dji_release_pdf
        try:
            def fake_fetch(url: str, timeout: int) -> bytes:
                calls.append(url)
                if url == "https://www.dji.com/mini-5-pro/downloads":
                    return html.encode("utf-8")
                if url == "https://example.com/RN/rn-404.pdf":
                    raise RuntimeError("HTTP Error 404: Not Found")
                if url == "https://example.com/RN/rn-good.pdf":
                    return b"%PDF-test%"
                raise RuntimeError(f"Unexpected URL: {url}")

            dji_source.fetch_bytes = fake_fetch
            dji_source.parse_dji_release_pdf = lambda _pdf, _name: [  # type: ignore[assignment]
                {
                    "version": "01.00.0500",
                    "released_time": "2026-03-01",
                    "release_note": {"en": "Test"},
                    "arb": None,
                    "active": True,
                }
            ]

            releases = dji_source.sync_dji_downloads(
                "Mini 5 Pro",
                {"url": "https://www.dji.com/mini-5-pro/downloads"},
                timeout=5,
            )
        finally:
            dji_source.fetch_bytes = original_fetch
            dji_source.parse_dji_release_pdf = original_parse_pdf

        self.assertEqual(len(releases), 1)
        self.assertEqual(releases[0]["version"], "01.00.0500")
        self.assertIn("https://example.com/RN/rn-404.pdf", calls)
        self.assertIn("https://example.com/RN/rn-good.pdf", calls)


if __name__ == "__main__":
    unittest.main()
