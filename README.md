# Ivrea Q&A RSS monitor

A GitHub Actions project for https://editorialivrea.com/ESP/preguntas.

## Snapshot-verified extraction (v2)
The parser has been tested against the supplied saved MHTML snapshot: 50 comments, all 50 with an inline answer. The page uses li#comment-ID > article.comment-body > .comment-content, and answers appear inside strong elements containing (((...))). Pagination links include replytocom links; these are normalised to avoid duplicate requests. Live HTTP access and GitHub deployment remain unverified.

## Publication model (v3)
The user confirmed that unanswered questions cannot be published. Every published comment is therefore treated as one Q&A entry. All new comment IDs generate an RSS item; changes to an existing entry generate an update item. Inline answer markers are parsed for diagnostics only and never suppress a notification. ANSWERED_ONLY has been removed.

Validate a saved file without network access or state changes:

```sh
python scraper.py --snapshot "saved-page.mhtml.txt"
```

The supplied snapshot is not bundled and is not used as the production baseline. Existing v1 hash-only and v2 structured state are compatible. Keep state.json when upgrading; changed content is detected using the full-entry hash.

## Setup
1. Create a public GitHub repository with its default branch (normally main).
2. Extract this archive and upload ALL files, including .github/workflows/rss.yml. Preserve the directory structure. Using git is the most reliable way to include hidden directories.
3. Under Settings > Pages, set Source to GitHub Actions.
4. Ensure Actions is enabled. The workflow requests contents: write, pages: write and id-token: write. Repository/organisation policy must permit those permissions and commits by github-actions[bot] to the default branch. Protected branches may require a policy change or an alternative state store.
5. Open Actions > Update Ivrea RSS > Run workflow on the default branch.
6. Check that the Detect new entries step reports a non-zero number of scanned comments and baseline=True. A successful first run intentionally creates an empty feed and records existing comments, avoiding a historical notification flood.
7. Open the deployed site from Settings > Pages or the workflow deployment link. Click Subscribe to the RSS feed and copy that link into your RSS reader.

## Behaviour
- Polls hourly at minute 17 UTC; manual runs are also supported.
- Uses Chromium to allow JavaScript-rendered comments, then extracts WordPress-style comment IDs and comment bodies.
- Follows same-host comment-page-N pagination links, including older pages where a new answer could appear. Stops with an error if MAX_PAGES (default 100) is exceeded or a page contains no supported comments.
- Detects every newly published Q&A entry and changes to its content, regardless of answer-marker formatting.
- Emits link-only Spanish RSS entries, not copyrighted question/answer text. No third-party comment text is stored; state contains IDs and content hashes.
- Each changed body gets a new RSS GUID. Dates represent detection time, not publication time. Repeated unchanged scans do not produce duplicate items.
- Keeps all seen IDs in state.json, and the latest 200 events in public/feed.xml. State is committed to the default branch, not kept in an expiring Actions cache.
- Treats each published comment as a Q&A entry, without identifying individual commenters.
- It cannot guarantee detection of content added and removed between polls, content hidden from the browser, or comments not reachable through pagination. Static FAQ sections are not monitored.
- A failure leaves the previous deployed feed intact. Enable workflow failure notifications in your GitHub notification settings if desired.

## Configuration
Set MAX_PAGES or FEED_LIMIT in the Detect new entries step's env mapping. Increase the job timeout if you intentionally scan a large archive. Avoid unnecessarily frequent polling.

## Local testing
Use Python 3.12. Install requirements, then run:

```sh
python -m pip install -r requirements.txt
python -m playwright install chromium
python -m unittest discover -s tests -v
python scraper.py
```

The included unit tests use artificial markup matching the supplied snapshot structure; the parser was also checked against the supplied snapshot. If live extraction fails, inspect the comment DOM in your browser and adapt extract() selectors. Do not delete state.json to troubleshoot: deleting it resets the baseline.

## GitHub caveats and references
Scheduled workflows run on the default branch, may be delayed or dropped during high load, and public-repository schedules can be disabled after 60 days without repository activity. Check the Actions tab periodically; this is polling, not a real-time webhook.

- https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows
- https://docs.github.com/en/actions/how-tos/manage-workflow-runs/disable-and-enable-workflows
- https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages
