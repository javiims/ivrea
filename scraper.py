"""Ivrea España Q&A -> RSS.

Reads the newest comments from the WordPress REST API of the Q&A section, detects newly
published or edited entries and writes an RSS feed whose items contain the full question
and Ivrea's answer.
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
# Only source: the WordPress REST API of the Q&A section. It always contains the newest
# comments (older ones drop out of it, which is fine: state.json remembers what was seen).
API = os.environ.get("API_URL", "https://editorialivrea.com/ESP/preguntas/wp-json/wp/v2/comments")
API_COMMENTS = min(int(os.environ.get("API_COMMENTS", "100")), 100)  # WordPress maximum is 100
STATE_VERSION = 7
FEED_LIMIT = int(os.environ.get("FEED_LIMIT", "200"))
# On the very first run, put the latest N entries in the feed so it is not empty.
BASELINE_ITEMS = int(os.environ.get("BASELINE_ITEMS", "10"))
TIMEOUT = int(os.environ.get("HTTP_TIMEOUT", "30"))
USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/130.0 Safari/537.36 IvreaRSS/7.0")
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


def no_cache(url):
    """Add a unique query parameter so page caches/CDNs cannot return an old copy."""
    return url + ("&" if "?" in url else "?") + f"nocache={int(time.time() * 1000)}"


def fetch_http(url, retries=3, accept="text/html,application/xhtml+xml"):
    last = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(no_cache(url), headers={
                "User-Agent": USER_AGENT,
                "Accept": accept,
                "Accept-Language": "es-ES,es;q=0.9",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            })
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                charset = resp.headers.get_content_charset() or "utf-8"
                final = urlunparse(urlparse(resp.geturl())._replace(query="", fragment=""))
                return resp.read().decode(charset, errors="replace"), final
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last = exc
            print(f"HTTP attempt {attempt}/{retries} failed for {url}: {exc}")
            time.sleep(5 * attempt)
    raise RuntimeError(f"Could not fetch {url}: {last}")


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

def api_to_html(items):
    """Render REST API comments with the same markup as the page, so one parser serves both."""
    parts = []
    for c in items:
        if c.get("status", "approved") != "approved":
            continue
        date = c.get("date_gmt")
        date = f"{date}+00:00" if date else c.get("date", "")
        parts.append(
            f'<li id="comment-{int(c["id"])}"><span class="comment-author"><cite class="fn">'
            f'{html.escape(c.get("author_name", ""))}</cite></span>'
            f'<a class="comment-date" href="{html.escape(c.get("link", ""))}">'
            f'<time datetime="{html.escape(date)}"></time></a>'
            f'<div class="comment-content">{(c.get("content") or {}).get("rendered", "")}</div></li>')
    return "".join(parts)


def crawl():
    url = f"{API}?per_page={API_COMMENTS}&orderby=date&order=desc"
    text, _ = fetch_http(url, accept="application/json")
    try:
        items = json.loads(text)
    except json.JSONDecodeError:
        raise RuntimeError(f"The API did not return JSON (blocked?): {text[:200]!r}. "
                           "No state or feed was overwritten.")
    if not isinstance(items, list) or not items:
        raise RuntimeError(f"The API returned no comments: {text[:200]!r}. "
                           "No state or feed was overwritten.")
    records = extract(api_to_html(items), SOURCE)[0]
    if not records:
        raise RuntimeError("API comments could not be parsed. No state or feed was overwritten.")
    print(f"REST API: {len(records)} comments (ids {min(map(int, records))}-{max(map(int, records))})")
    return records


def newest(records):
    dates = [r["published"] for r in records.values() if r.get("published")]
    return max(dates, key=lambda d: datetime.fromisoformat(d)) if dates else None


# --------------------------------------------------------------------------- state

def _event(record, cid, kind, now, date=None):
    return {"guid": f"ivrea:{cid}:{record['hash']}:{now}", "id": cid, "kind": kind,
            "url": record["url"], "detected": now, "date": date or now,
            "author": record.get("author", ""), "published": record.get("published"),
            "segments": record.get("segments", [])}


def update(state, records, now):
    # Older versions could read a stale cached page, so their state is rebuilt once.
    initial = not state.get("initialised", False) or state.get("version", 0) < STATE_VERSION
    if initial:
        state["seen"], state["events"] = {}, []
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
    state["version"] = STATE_VERSION
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
        print(f"Newest comment: {newest(records)}")
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
    print(f"Newest comment found: {newest(records)}")
    print(f"Scanned {len(records)} comments; {count} new/updated entries; baseline={initial}.")


if __name__ == "__main__":
    main()
