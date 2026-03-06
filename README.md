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
3. Optional: set `sources.refresh_workflow_url` so the UI "Refresh Now" button opens your workflow run page.
4. Run:
   - `python scripts/fetch_firmware_details.py`
   - `python scripts/generate_index.py`
5. Commit and push.

## GitHub Pages setup

1. Push this repo to GitHub.
2. In GitHub repository settings:
   - Pages -> Source: `GitHub Actions`
3. Run workflow `Update and Deploy Firmware Tracker` once (manual dispatch).
4. The site deploys and refreshes daily at 06:15 UTC.
5. CI runs parser/schema tests before sync.

## Automated Testing (No Local Setup)

- `CI` workflow (`.github/workflows/ci.yml`) runs on every pull request and push to `main`:
  - installs dependencies
  - runs unit/parser/schema tests
  - runs `scripts/generate_index.py` as a build smoke test
- `Update and Deploy Firmware Tracker` workflow (`.github/workflows/update-and-deploy.yml`) runs daily + manual:
  - re-runs tests
  - fetches official firmware data
  - regenerates browser assets + markdown summary
  - commits changes (scheduled/manual runs) and deploys to GitHub Pages

Recommended GitHub branch protection:

- Require pull request before merging to `main`
- Require status check: `CI / test`
- Disable direct pushes to `main` (except admins if you want an emergency path)

## Notes

- Includes DJI, Sony, and lighting devices by default.
- `Godox V860II (Sony)` is mapped to `V860IIS` firmware feed.
- `Amaran 300c` is set as app-managed (`Sidus Link`) via static/manual entry.
- `Dell U4025QW` remains static/manual due anti-bot protections on official support pages.
- Sync runs in parallel with retry/backoff and records source health each run.
  - Default: app continues to serve last known good data even if a source fails.
  - Transient network failures (for example temporary DNS outages in CI) are tracked separately and do not raise persistent UI source-issue banners.
  - Per-device `allow_empty: true` can be used for feeds where no firmware is currently published; this avoids false-positive source alerts.
  - Optional strict mode: `python scripts/fetch_firmware_details.py --fail-on-regression`
- UI is single-table for all devices and includes:
  - `Refresh Now` (opens `sources.refresh_workflow_url`)
  - `Reload Data` (reloads latest deployed assets)
  - Source issue banner when a vendor feed is failing (for example DJI parsing errors)
  - New-firmware banner and row highlights based on what your browser has previously seen (`Mark as Seen` to clear)
  - `View All` cards include an `Open official download page` link (landing page, not direct binary URL)
- Devices without configured sources are skipped.
