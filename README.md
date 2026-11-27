# Kobo Daily Digest

Automatically builds daily e-ink-friendly EPUB digests (starting with **Hacker News**, ready to add **TLDR newsletters** next), keeps a rolling **4-day window**, and syncs over Wi-Fi directly into a folder on your Kobo (`/mnt/onboard/DailyReads`) with automatic 4-day local purging.

Works with **both** KOReader and Stock Kobo (Nickel) since digests are standard EPUBs saved in a normal Kobo folder.

## How It Works

1. **GitHub Actions (daily at 13:00 UTC / 5–6 AM PT)** runs `kobo-digest build --source hn`:
   - Fetches top Hacker News stories via the official HN Firebase API.
   - Extracts clean article text using `trafilatura` and sanitizes into strict XHTML.
   - Builds `YYYY-MM-DD-HN.epub` with a front-page table of contents and one chapter per article.
   - Purges any digest older than **4 days** and writes `manifest.json` + `index.html` to the `gh-pages` branch.
2. **KOReader Plugin (`dailydigest.koplugin`) on your Kobo**:
   - Whenever your Kobo connects to Wi-Fi in KOReader (or when you tap **Tools → Daily Digest → Sync now**), it checks `manifest.json`, downloads any new EPUBs into `/mnt/onboard/DailyReads/`, and deletes any local `YYYY-MM-DD-*.epub` (plus its `.sdr` sidecar folder) older than 4 days.
   - Because `/mnt/onboard/DailyReads/` is a normal Kobo folder, Stock Kobo (Nickel) can read the same EPUBs too.

## Local Quickstart

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e ".[dev]"

# Run unit tests
pytest

# Generate today's Hacker News EPUB locally
kobo-digest build --source hn --out dist/digests --max-articles 15 --retention-days 4
```

## Deploy to GitHub Pages (Free, No Laptop Needed)

1. Push this repo to GitHub (e.g., `github.com/<you>/kobo-daily-digest`).
2. Run the **Build & Publish Daily Kobo Digests** workflow once manually from the **Actions** tab (this creates the `gh-pages` branch).
3. In GitHub repo **Settings → Pages**, set **Source** to **Deploy from a branch** → `gh-pages` / `/ (root)`.
4. Your manifest URL will be:
   ```
   https://<you>.github.io/kobo-daily-digest/digests/manifest.json
   ```

## Install the KOReader Plugin on Your Kobo (One-Time)

1. Copy `koreader-plugin/dailydigest.koplugin/` onto your Kobo at:
   ```
   /mnt/onboard/.adds/koreader/plugins/dailydigest.koplugin/
   ```
2. Restart KOReader.
3. Open **Tools (wrench icon) → Daily Digest → Settings → Feed URL** and paste your `manifest.json` URL.
4. Tap **Sync now** once to test! From then on, every time your Kobo connects to Wi-Fi in KOReader, it will automatically pull new daily EPUBs into `DailyReads/` and purge anything older than 4 days.

> **Tip for Stock Kobo (Nickel):** When you exit KOReader back to Nickel (or trigger `rescan_books_full` via NickelMenu), Nickel automatically indexes the EPUBs in `DailyReads/` too.

## Next Increment (Phase 2)

- Add `fetcher_tldr.py` (`--source tldr`) to generate `YYYY-MM-DD-TLDR.epub` alongside `YYYY-MM-DD-HN.epub`.
