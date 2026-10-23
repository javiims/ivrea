# Ivrea Q&A RSS monitor

GitHub Actions project that turns https://www.editorialivrea.com/ESP/preguntas/ into an RSS
feed. Each RSS item contains **the reader's question and Ivrea's answer** (text inside
`(((…)))`), so you can read everything directly in your RSS reader.

## What changed in v7
- **Single source: the REST API** `https://editorialivrea.com/ESP/preguntas/wp-json/wp/v2/comments`.
  It always holds the newest comments, so page numbers (22, 23, …) no longer matter and the
  page probing was removed. One request per run (`per_page=100`, newest first).
- Older comments disappearing from the API is expected and harmless: `state.json` remembers
  every comment already seen, so nothing is re-sent or lost.
- Playwright removed from `requirements.txt` (no browser needed).
- State version 7: the state is rebuilt once (feed restarts with the latest `BASELINE_ITEMS`).
- If the API is blocked or returns no JSON, the run fails and the feed/state are left untouched.

## What changed in v6
- **Always reads the newest comment pages.** Each run finds the highest existing
  `comment-page-N` automatically: it starts from the largest of `START_PAGE`, the page saved in
  `state.json` (`last_page`) and the page numbers linked on the site, then tests N+1, N+2, N+4…
  and narrows down to the last page that exists (a missing page is a 404, a redirect, or a page
  without new comments). It then reads that page and the previous one (`SCAN_PAGES`).
  When the site moves from page 22 to 23, 24…, the next run follows it with no changes needed.
  Normally this costs only one or two extra requests per run.
- The log shows each probe and `Newest comment page: N`.
- The REST API is now only an extra safety net; the comment pages are the main source.
- State version 6: the state is rebuilt once (feed restarts with the latest `BASELINE_ITEMS`).

## What changed in v5
- **Recent questions are detected.** The site serves an old cached copy of the page to
  scripts (it showed March 2026). The scraper now reads the WordPress REST API
  (`/ESP/preguntas/wp-json/wp/v2/comments?post=22`), which is not cached and returns the
  newest 100 comments. If the API is unavailable it falls back to the HTML pages, with
  cache-busting, and always jumps to the highest `comment-page-N` it can find instead of
  trusting the base page. The log prints `Newest comment found: …` so you can check it.
- **Old state is rebuilt automatically** on the first v5 run (it was based on stale data):
  the feed is reset to the latest `BASELINE_ITEMS` entries, with no flood of old items.
- **No page preview in the entries.** Items no longer have a `<link>` and the
  "Ver en la web de Ivrea" link was removed, so readers stop showing the general Q&A page
  preview; each entry only contains the question and the answer.

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
| `API_URL` | `https://editorialivrea.com/ESP/preguntas/wp-json/wp/v2/comments` | Comments API |
| `API_COMMENTS` | 100 | Newest comments read per run (max 100) |
| `FEED_LIMIT` | 200 | Items kept in the feed |
| `BASELINE_ITEMS` | 10 | Items shown after the first run (0 = empty feed) |
| `HTTP_TIMEOUT` | 30 | Seconds per HTTP request (3 retries) |

## Behaviour notes
- New entries use the detection time as `pubDate` (comments are published when Ivrea approves and
  answers them, which can be much later than the question date).
- If an existing entry changes (e.g. answer edited) an item marked with ✏️ is added.
  Edits are only detected while the comment is still among the newest 100 in the API.
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
