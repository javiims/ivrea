# Ivrea Q&A RSS monitor

GitHub Actions project that turns https://www.editorialivrea.com/ESP/preguntas/ into an RSS
feed. Each RSS item contains **the reader's question and Ivrea's answer** (text inside
`(((…)))`), so you can read everything directly in your RSS reader.

## What changed in v4
- **Fixed the hanging/cancelled runs.** The old workflow ran
  `python -m playwright install --with-deps chromium`, which calls `apt-get`. On the runner the
  Ubuntu mirror (`azure.archive.ubuntu.com`) did not respond, so the job hung ~25 min until the
  timeout cancelled it. The page is plain server-rendered WordPress, so the scraper now uses a
  normal HTTP request (no browser, no apt). A browser fallback remains, using the Google Chrome
  already installed on GitHub runners (`channel="chrome"`), so it never needs `playwright install`.
- **Faster scans.** The base URL always shows the newest page (newest first). Only that page plus
  the previous one are fetched (2 requests) instead of crawling every page each hour.
  Set `FULL_SCAN=1` to crawl all pages if you ever need to.
- **Full content in the feed.** Item title = start of the question; description and
  `content:encoded` = question(s) + Ivrea's answer(s), in reading order (some entries have
  several question/answer pairs). Author is in `dc:creator`. A link to the original remains.
- Job timeout reduced to 10 minutes, with per-step timeouts. `actions/checkout@v5` and
  `actions/setup-python@v6` remove the Node.js 20 deprecation warning.
- MHTML snapshots are now decoded as UTF-8 (accents were garbled before).

## Setup / upgrade
1. Replace the files in the repository with the contents of this archive, keeping the structure
   (`.github/workflows/rss.yml`, `tests/test_scraper.py`, `scraper.py`, `requirements.txt`).
   If a `state.json` already exists, keep it: hashes are compatible and no duplicates are created.
2. Settings > Pages > Source: **GitHub Actions**.
3. Actions > Update Ivrea RSS > **Run workflow**.
4. The *Detect new entries* step should print about 50 comments per page and finish in seconds.
   The first run (no state) creates a baseline and puts the latest `BASELINE_ITEMS` (default 10)
   entries in the feed so you can see the format; afterwards only new or changed entries appear.
5. Subscribe to `https://<user>.github.io/<repo>/feed.xml`.

## Configuration (env of *Detect new entries*)
| Variable | Default | Meaning |
|---|---|---|
| `SCAN_PAGES` | 2 | Pages scanned per run (newest first) |
| `FULL_SCAN` | off | `1` = crawl every comment page (up to `MAX_PAGES`) |
| `FEED_LIMIT` | 200 | Items kept in the feed |
| `BASELINE_ITEMS` | 10 | Items shown after the first run (0 = empty feed) |
| `HTTP_TIMEOUT` | 30 | Seconds per HTTP request (3 retries) |

## Behaviour notes
- New entries use the detection time as `pubDate` (comments are published when Ivrea approves and
  answers them, which can be much later than the question date).
- If an existing entry changes (e.g. answer edited) an item marked with ✏️ is added.
  Edits on pages older than the scanned ones are only detected with `FULL_SCAN=1`.
- If the site blocks the request or the markup changes, the run fails and the previous feed and
  state are left untouched.
- The feed published on GitHub Pages is public and contains text written by readers and Ivrea.
  Use it for personal reading; consider a private alternative if you do not want to republish it.

## Local testing
```sh
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python scraper.py --snapshot "saved-page.mhtml.txt" --preview preview   # writes preview/feed.xml
python scraper.py                                                       # live run
```
Do not delete `state.json` in the repository: it resets the baseline.
