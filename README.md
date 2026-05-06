# Firmware Tracker

Static firmware-tracking site that deploys on GitHub Pages and syncs from official vendor sources.

## How it works

- Source of truth: `data/devices.json`
- Schema: `data/devices.schema.json` (validated before sync)
- Official sync: `scripts/fetch_firmware_details.py`
  - DJI: downloads pages + release-notes PDFs
  - Sony: official `support.d-imaging.sony.co.jp` firmware pages (model-code based)
  - Godox: official firmware listing pages
  - Apple: official support pages for iOS/macOS/watchOS/AirPods
  - Atomos: official product-support firmware sections (device article based)
  - Bambu: official wiki release-history pages
  - Static/manual entries for devices without stable public firmware feeds
- Generator: `scripts/generate_index.py`
- Generated browser assets:
  - `docs/devices/categories.js`
  - `docs/devices/index.js`
  - `docs/devices/config.js`
  - `docs/FIRMWARE_SUMMARY.md` (repo-friendly markdown snapshot)
- Frontend: `docs/index.html`

## Local usage

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -p 'test_*.py' -v
python scripts/fetch_firmware_details.py
python scripts/generate_index.py
```

Then open `docs/index.html` in a browser.

## Add devices

1. Add your device under `categories` in `data/devices.json`.
2. Add a source entry under `sources.device_sources`:
   - `dji_downloads`
   - `sony_cscs`
   - `godox_listing`
   - `apple_support`
   - `atomos_support`
   - `bambu_wiki`
   - `static`
   - Optional resilience fields:
     - `fallback_source` (same structure as a primary source)
     - `allow_regression` (`true` to bypass release-date guardrail for that source)
     - `treat_404_as_empty` (`true` to interpret HTTP 404 as empty source data and continue fallback)
3. Run:
   - `python scripts/fetch_firmware_details.py`
   - `python scripts/generate_index.py`
4. Commit and push.

## GitHub Pages setup

1. Push this repo to GitHub.
2. In GitHub repository settings:
   - Pages -> Source: `GitHub Actions`
3. Run workflow `Update and Deploy Firmware Tracker` once (manual dispatch).
4. The site deploys and refreshes daily at 07:00 Europe/Helsinki (DST-safe scheduler).
5. CI runs parser/schema tests before sync.

## Automated Testing (No Local Setup)

- `CI` workflow (`.github/workflows/ci.yml`) runs on every pull request and push to `main`:
  - installs dependencies
  - runs unit/parser/schema tests
  - runs `scripts/generate_index.py` as a build smoke test
- `Update and Deploy Firmware Tracker` workflow (`.github/workflows/update-and-deploy.yml`) runs daily + manual:
  - re-runs tests
  - fetches official firmware data with regression guardrails enabled
  - regenerates browser assets + markdown summary
  - deploys generated `docs/` as a Pages artifact (no push back to protected `main`)
  - opens/updates an automation PR for generated file changes and enables auto-merge

### Automated Sync PR Setup (One-time)

To run fully hands-off daily:

1. Add these actions to your repository Actions allowlist:
   - `peter-evans/create-pull-request@v8`
   - `peter-evans/enable-pull-request-automerge@v3`
2. Add repository secret `FW_BOT_TOKEN` (fine-grained PAT):
   - Repository permissions: `Contents: Read and write`, `Pull requests: Read and write`
3. Branch protection for `main`:
   - Keep PR required and status checks required.
   - Set required approvals to `0` for full automation.

Recommended GitHub branch protection:

- Require pull request before merging to `main`
- Require status check: `CI / test`
- Disable direct pushes to `main` (except admins if you want an emergency path)

## Notes

- Includes DJI, Sony, and lighting devices by default.
- DJI Mini 5 Pro currently uses:
  - primary: `https://www.dji.com/fi/mini-5-pro/downloads`
  - fallback: `https://www.dji.com/fi/downloads/products/mini-5-pro#doc`
- `Godox V860II (Sony)` is mapped to `V860IIS` firmware feed.
- `Amaran 300c` is set as app-managed (`Sidus Link`) via static/manual entry.
- `Dell U4025QW` remains static/manual due anti-bot protections on official support pages.
- Sync runs in parallel with retry/backoff and records source health each run.
  - Default: app continues to serve last known good data even if a source fails.
  - Transient network failures (for example temporary DNS outages in CI) are tracked separately and do not raise persistent UI source-issue banners.
  - Per-device `allow_empty: true` can be used for feeds where no firmware is currently published; this avoids false-positive source alerts.
  - DJI stale `404` release-note PDF links are tolerated as empty results (last known good data is retained).
  - Release guardrail prevents replacing current data with an older "latest" release date unless `allow_regression: true` is set on that source.
  - Version-aware guardrails prioritize new firmware versions over stale vendor dates, reject apparent downgrades, and preserve previous release metadata when a parser can only see the latest row.
  - Parsers now extract ranked release candidates from official evidence (tables, visible text, links, filenames, and PDFs) before a shared resolver chooses the best firmware value.
  - Vendor HTML parsers use shared text/date/link helpers and tolerate common markup changes in Apple, Sony, Godox, Atomos, and Bambu pages.
  - Per-device and per-vendor health is tracked (`consecutive_failures`, `last_success_utc`, `last_error_type`) for better diagnostics.
  - Strict mode: `python scripts/fetch_firmware_details.py --fail-on-regression` is used by the scheduled deploy workflow.
- UI is single-table for all devices and includes:
  - A firmware status summary that changes when unseen firmware is detected
  - Last generated timestamp
  - Source issue banner when a vendor feed is failing (for example DJI parsing errors)
  - New-firmware status and row highlights based on what your browser has previously seen (`Mark as Seen` to clear)
  - `View All` cards include an `Open official download page` link (landing page, not direct binary URL)
- Devices without configured sources are skipped.
