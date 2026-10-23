"""Ivrea España Q&A -> RSS.

Fetches the public Q&A page with plain HTTP (no browser, no apt packages), detects
newly published entries and writes an RSS feed whose items contain the full
question and Ivrea's answer.
"""
import argparse
import hashlib
import html
import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlparse, urlunparse
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup, NavigableString

SOURCE = "https://www.editorialivrea.com/ESP/preguntas/"
# The base URL always shows the newest comment page (newest first). Scanning it plus
# the previous page is enough to catch new answers, even right after a page rollover.
SCAN_PAGES = int(os.environ.get("SCAN_PAGES", "2"))
FULL_SCAN = os.environ.get("FULL_SCAN", "").lower() in {"1", "true", "yes"}
MAX_PAGES = int(os.environ.get("MAX_PAGES", "100"))
FEED_LIMIT = int(os.environ.get("FEED_LIMIT", "200"))
# On the very first run, put the latest N entries in the feed so it is not empty.
BASELINE_ITEMS = int(os.environ.get("BASELINE_ITEMS", "10"))
TIMEOUT = int(os.environ.get("HTTP_TIMEOUT", "30"))
USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/130.0 Safari/537.36 IvreaRSS/4.0")
ANSWER_RE = re.compile(r"\(\(\((.*?)\)\)\)", re.S)


# --------------------------------------------------------------------------- input

def load_snapshot(path):
    raw = Path(path).read_bytes()
    message = BytesParser(policy=policy.default).parsebytes(raw)
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True)
                charset = part.get_content_charset() or "utf-8"
                return payload.decode(charset, errors="replace")
        raise ValueError("MHTML contains no HTML part")
    return raw.decode("utf-8-sig")


def fetch_http(url, retries=3):
    last = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "es-ES,es;q=0.9",
            })
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                charset = resp.headers.get_content_charset() or "utf-8"
                return resp.read().decode(charset, errors="replace"), urldefrag(resp.geturl())[0]
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last = exc
            print(f"HTTP attempt {attempt}/{retries} failed for {url}: {exc}")
            time.sleep(5 * attempt)
    raise RuntimeError(f"Could not fetch {url}: {last}")


def fetch_browser(url):
    """Fallback only: uses the Google Chrome already installed on GitHub runners.

    No `playwright install --with-deps` (that apt step is what hung the old workflow).
    """
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome")
        try:
            page = browser.new_page(user_agent=USER_AGENT)
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            try:
                page.wait_for_selector(".comment-content", timeout=15000)
            except Exception:
                pass
            return page.content(), urldefrag(page.url)[0]
        finally:
            browser.close()


def fetch(url):
    try:
        text, final = fetch_http(url)
        if 'class="comment-content' in text or "class='comment-content" in text:
            return text, final
        print(f"No comments in plain HTTP response for {url}; trying browser fallback.")
    except RuntimeError as exc:
        print(f"{exc}; trying browser fallback.")
    try:
        return fetch_browser(url)
    except ImportError:
        raise RuntimeError("Plain HTTP failed and Playwright is not installed for the fallback.")


# ------------------------------------------------------------------------- parsing

def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _structured_text(body):
    """Text keeping line breaks from <br> and paragraphs."""
    body = BeautifulSoup(str(body), "html.parser")
    for node in body.find_all(string=True):
        node.replace_with(NavigableString(re.sub(r"\s+", " ", str(node))))
    for br in body.find_all("br"):
        br.replace_with("\n")
    for block in body.find_all(["p", "div", "li", "blockquote"]):
        block.insert_before("\n\n")
        block.insert_after("\n\n")
    text = body.get_text("")
    lines = [line.strip() for line in text.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def split_segments(text):
    """Return [(kind, text)] in reading order; kind is 'q' (reader) or 'a' (Ivrea)."""
    segments, pos = [], 0
    for match in ANSWER_RE.finditer(text):
        before = text[pos:match.start()].strip()
        if before:
            segments.append(("q", before))
        answer = match.group(1).strip()
        if answer:
            segments.append(("a", answer))
        pos = match.end()
    rest = text[pos:].strip()
    if rest:
        segments.append(("q", rest))
    return segments


def extract(page_html, url):
    soup = BeautifulSoup(page_html, "html.parser")
    records = {}
    for node in soup.select('li[id^="comment-"], article[id^="comment-"], div[id^="div-comment-"]'):
        match = re.fullmatch(r"(?:div-)?comment-(\d+)", node.get("id", ""))
        if not match:
            continue
        cid = match.group(1)
        if cid in records:
            continue
        body = node.select_one(".comment-content, .comment-text")
        if body is None:
            continue
        body = BeautifulSoup(str(body), "html.parser")
        for child in body.select(".children, script, style, .reply"):
            child.decompose()
        for image in body.select("img.emoji"):  # emoji images are text
            image.replace_with(image.get("alt", ""))
        # Hash kept identical to v2/v3 so an existing state.json does not re-trigger.
        flat = " ".join(body.get_text(" ", strip=True).split())
        if not flat:
            continue
        text = _structured_text(body)
        segments = split_segments(text)
        answers = [s for k, s in segments if k == "a"]
        questions = [s for k, s in segments if k == "q"]
        author_node = node.select_one(".comment-author .fn, cite.fn, .comment-author")
        author = " ".join(author_node.get_text(" ", strip=True).split()) if author_node else ""
        time_node = node.select_one("time[datetime]")
        published = time_node["datetime"] if time_node else None
        permalink = next((urljoin(url, a["href"]) for a in node.select("a.comment-date[href], a[href]")
                          if urlparse(urljoin(url, a["href"])).fragment == f"comment-{cid}"),
                         urldefrag(url)[0] + f"#comment-{cid}")
        records[cid] = {
            "id": cid, "hash": _digest(flat),
            "question_hash": _digest("\n".join(questions)),
            "answer_hash": _digest("\n".join(answers)) if answers else None,
            "answered": bool(answers), "url": permalink, "author": author,
            "published": published, "segments": segments,
        }
    pages, prev_page = set(), None
    base = urlparse(url)
    for a in soup.select("a[href]"):
        parsed = urlparse(urljoin(url, a["href"]))
        if parsed.netloc == base.netloc and re.fullmatch(r"/ESP/preguntas/comment-page-\d+/?", parsed.path):
            clean = urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))
            pages.add(clean)
            if "prev" in (a.get("class") or []) and prev_page is None:
                prev_page = clean
    return records, pages, prev_page


# --------------------------------------------------------------------------- crawl

def crawl():
    all_records, visited = {}, set()
    pending = [SOURCE]
    limit = MAX_PAGES if FULL_SCAN else SCAN_PAGES
    while pending and len(visited) < limit:
        url = pending.pop(0)
        if url in visited:
            continue
        page_html, final = fetch(url)
        records, links, prev_page = extract(page_html, final)
        if not records:
            raise RuntimeError(
                f"No comments found at {final}. The site may have changed markup or blocked the "
                "request. No state or feed was overwritten.")
        visited.update([url, final])
        print(f"{final}: {len(records)} comments")
        for cid, record in records.items():
            all_records.setdefault(cid, record)
        if FULL_SCAN:
            pending.extend(sorted(links - visited - set(pending)))
        elif prev_page and prev_page not in visited:
            pending.append(prev_page)
        if pending:
            time.sleep(1)
    if FULL_SCAN and pending:
        raise RuntimeError("Pagination limit reached. Increase MAX_PAGES; state has not been changed.")
    return all_records


# --------------------------------------------------------------------------- state

def _event(record, cid, kind, now, date=None):
    return {"guid": f"ivrea:{cid}:{record['hash']}:{now}", "id": cid, "kind": kind,
            "url": record["url"], "detected": now, "date": date or now,
            "author": record.get("author", ""), "published": record.get("published"),
            "segments": record.get("segments", [])}


def update(state, records, now):
    initial = not state.get("initialised", False)
    seen = state.setdefault("seen", {})
    events = state.setdefault("events", [])
    count = 0
    ordered = sorted(records.items(), key=lambda pair: int(pair[0]))
    for cid, record in ordered:
        old = seen.get(cid)
        kind = None
        if not initial:
            old_hash = old if isinstance(old, str) else (old or {}).get("hash")
            if old is None:
                kind = "new"
            elif old_hash != record["hash"]:
                kind = "updated"
        if kind:
            events.append(_event(record, cid, kind, now))
            count += 1
        seen[cid] = {key: record[key] for key in ("hash", "question_hash", "answer_hash")}
    if initial and BASELINE_ITEMS > 0:
        # Seed the feed with the latest entries, dated with their own comment date.
        for cid, record in ordered[-BASELINE_ITEMS:]:
            events.append(_event(record, cid, "new", now, record.get("published")))
    # Old link-only events have no text; drop them so the feed only shows full entries.
    state["events"] = [e for e in events if e.get("segments")][-FEED_LIMIT:]
    state["initialised"] = True
    state["version"] = 4
    return count, initial


# ---------------------------------------------------------------------------- feed

def _shorten(text, limit=90):
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1].rsplit(" ", 1)[0] + "…"


def _paragraphs(text):
    out = []
    for para in text.split("\n\n"):
        out.append("<p>" + "<br/>".join(html.escape(line) for line in para.split("\n")) + "</p>")
    return "".join(out)


def render_html(event):
    author = html.escape(event.get("author") or "Lector")
    parts = []
    if event.get("kind") == "updated":
        parts.append("<p><em>Entrada modificada (respuesta nueva o editada).</em></p>")
    for kind, text in event["segments"]:
        if kind == "q":
            parts.append(f"<p><strong>❓ {author} pregunta:</strong></p>{_paragraphs(text)}")
        else:
            parts.append("<blockquote style=\"border-left:3px solid #c00;padding-left:8px;margin-left:0\">"
                         f"<p><strong>💬 Respuesta de Ivrea:</strong></p>{_paragraphs(text)}</blockquote>")
    if not any(k == "a" for k, _ in event["segments"]):
        parts.append("<p><em>(Sin respuesta marcada con (((…))).)</em></p>")
    parts.append(f'<p><a href="{html.escape(event["url"])}">Ver en la web de Ivrea</a></p>')
    return "".join(parts)


def plain_text(event):
    out = []
    for kind, text in event["segments"]:
        out.append(("PREGUNTA: " if kind == "q" else "RESPUESTA: ") + text)
    return "\n\n".join(out)


def write_feed(state, directory):
    directory.mkdir(parents=True, exist_ok=True)
    content_ns = "http://purl.org/rss/1.0/modules/content/"
    ET.register_namespace("content", content_ns)
    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")

    def add(parent, tag, text):
        ET.SubElement(parent, tag).text = text

    add(channel, "title", "Ivrea España: preguntas y respuestas")
    add(channel, "link", SOURCE)
    add(channel, "description", "Preguntas de lectores y respuestas de Ivrea en su sección de preguntas.")
    add(channel, "language", "es")
    add(channel, "ttl", "60")
    for event in reversed(state["events"]):
        item = ET.SubElement(channel, "item")
        questions = [t for k, t in event["segments"] if k == "q"]
        prefix = "✏️ " if event.get("kind") == "updated" else ""
        title = _shorten(questions[0]) if questions else f"Entrada #{event['id']}"
        add(item, "title", prefix + title)
        add(item, "link", event["url"])
        if event.get("author"):
            add(item, "{http://purl.org/dc/elements/1.1/}creator", event["author"])
        guid = ET.SubElement(item, "guid", isPermaLink="false")
        guid.text = event["guid"]
        add(item, "pubDate", format_datetime(datetime.fromisoformat(event.get("date") or event["detected"])))
        body = render_html(event)
        add(item, "description", body)
        add(item, f"{{{content_ns}}}encoded", body)
    ET.register_namespace("dc", "http://purl.org/dc/elements/1.1/")
    ET.indent(rss)
    ET.ElementTree(rss).write(directory / "feed.xml", encoding="utf-8", xml_declaration=True)
    (directory / "index.html").write_text(
        '<!doctype html><html lang="es"><meta charset="utf-8"><title>Ivrea RSS</title>'
        '<link rel="alternate" type="application/rss+xml" href="feed.xml">'
        '<h1>Ivrea: preguntas y respuestas (RSS)</h1>'
        '<p><a href="feed.xml">Suscribirse al feed RSS</a></p></html>', encoding="utf-8")
    (directory / ".nojekyll").touch()


# ---------------------------------------------------------------------------- main

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", help="Validate a saved HTML/MHTML file without fetching or changing state")
    parser.add_argument("--preview", help="With --snapshot: write a preview feed to this folder")
    args = parser.parse_args()
    if args.snapshot:
        records, pages, prev_page = extract(load_snapshot(args.snapshot), SOURCE)
        if not records:
            raise RuntimeError("No comments detected in snapshot")
        print(json.dumps({"comments": len(records), "answered": sum(r["answered"] for r in records.values()),
                          "previous_page": prev_page, "pagination_urls": sorted(pages)}, indent=2))
        if args.preview:
            state = {}
            update(state, records, datetime.now(timezone.utc).isoformat())
            write_feed(state, Path(args.preview))
        return
    state_path = Path("state.json")
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    records = crawl()  # fetch everything before modifying any files
    now = datetime.now(timezone.utc).isoformat()
    count, initial = update(state, records, now)
    write_feed(state, Path("public"))
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Scanned {len(records)} comments; {count} new/updated entries; baseline={initial}.")


if __name__ == "__main__":
    main()
