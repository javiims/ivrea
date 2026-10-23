"""Link-only RSS: no third-party question or answer text is republished."""
import argparse
from email import policy
from email.parser import BytesParser
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse, urldefrag, urlunparse
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup

SOURCE = "https://www.editorialivrea.com/ESP/preguntas/"
MAX_PAGES = int(os.environ.get("MAX_PAGES", "100"))
FEED_LIMIT = int(os.environ.get("FEED_LIMIT", "200"))


def load_snapshot(path):
    raw = Path(path).read_bytes()
    message = BytesParser(policy=policy.default).parsebytes(raw)
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_type() == 'text/html':
                return part.get_content()
        raise ValueError('MHTML contains no HTML part')
    return raw.decode('utf-8-sig')


def extract(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    records = {}
    for node in soup.select('li[id^="comment-"], article[id^="comment-"], div[id^="div-comment-"]'):
        match = re.fullmatch(r'(?:div-)?comment-(\d+)', node.get('id', ''))
        if not match:
            continue
        cid = match.group(1)
        body = node.select_one('.comment-content, .comment-text')
        if body is None:
            continue
        body = BeautifulSoup(str(body), 'html.parser')
        for child in body.select('.children, script, style, .reply'):
            child.decompose()
        # Emoji images are text content, not decorative avatars.
        for image in body.select('img.emoji'):
            image.replace_with(image.get('alt', ''))
        text = ' '.join(body.get_text(' ', strip=True).split())
        if not text:
            continue
        answers = []
        for strong in list(body.select('strong')):
            value = strong.get_text(' ', strip=True)
            if '(((' in value and ')))' in value:
                answers.append(' '.join(value.split()))
                strong.decompose()
        question = ' '.join(body.get_text(' ', strip=True).split())
        answer = '\n'.join(answers)
        def digest(value):
            return hashlib.sha256(value.encode('utf-8')).hexdigest()
        permalink = next((urljoin(url, a['href']) for a in node.select('a.comment-date[href], a[href]')
                          if urlparse(urljoin(url, a['href'])).fragment == f'comment-{cid}'),
                         urldefrag(url)[0] + f'#comment-{cid}')
        records[cid] = {'id': cid, 'hash': digest(text), 'question_hash': digest(question),
                        'answer_hash': digest(answer) if answer else None,
                        'answered': bool(answer), 'url': permalink}
    pages = set()
    base = urlparse(url)
    for a in soup.select('a[href]'):
        parsed = urlparse(urljoin(url, a['href']))
        if parsed.netloc == base.netloc and re.fullmatch(r'/ESP/preguntas/comment-page-\d+/?', parsed.path):
            # Reply links contain replytocom and would otherwise cause redundant fetches.
            pages.add(urlunparse((parsed.scheme, parsed.netloc, parsed.path, '', '', '')))
    return records, pages


def crawl():
    from playwright.sync_api import sync_playwright
    all_records = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context(user_agent="IvreaRSSMonitor/1.0 (hourly link-only RSS)")
        page = context.new_page()
        pending, visited = [SOURCE], set()
        scanned_pages = 0
        while pending:
            url = pending.pop(0)
            if url in visited:
                continue
            if scanned_pages >= MAX_PAGES:
                raise RuntimeError("Pagination limit reached. Increase MAX_PAGES; state has not been changed.")
            response = page.goto(url, wait_until="domcontentloaded", timeout=60000)
            if response is None or response.status >= 400:
                raise RuntimeError(f"HTTP failure fetching {url}: {response.status if response else 'no response'}")
            canonical = urldefrag(page.url)[0]
            # A short wait permits comments inserted by JavaScript to render.
            try:
                page.wait_for_selector('.comment-content, .comment-text', timeout=15000)
            except Exception:
                pass
            records, links = extract(page.content(), canonical)
            if not records:
                raise RuntimeError(
                    f"No supported comments found at {canonical}. The page may use a different markup, "
                    "an external discussion widget, or a bot challenge. No state or feed was overwritten. "
                    "Inspect the live page and adapt extract() before enabling the schedule."
                )
            scanned_pages += 1
            for cid, record in records.items():
                if cid in all_records and all_records[cid]["hash"] != record["hash"]:
                    raise RuntimeError("A comment changed during pagination; retry for a consistent scan.")
                all_records[cid] = record
            visited.update([url, canonical])
            pending.extend(sorted(links - visited - set(pending)))
            page.wait_for_timeout(1000)
        browser.close()
    return all_records


def update(state, records, now):
    initial = not state.get('initialised', False)
    seen = state.setdefault('seen', {})
    events = state.setdefault('events', [])
    count = 0
    for cid, record in sorted(records.items(), key=lambda pair: int(pair[0])):
        old = seen.get(cid)
        kind = None
        # Every published comment is a Q&A entry; answer markers never gate events.
        if not initial:
            if old is None:
                kind = 'new'
            elif isinstance(old, str):
                if old != record['hash']:
                    kind = 'updated'
            elif old.get('hash') != record['hash']:
                kind = 'updated'
        if kind:
            # Detection time prevents duplicate GUIDs if content is changed then reverted.
            events.append({'guid': f'ivrea:{cid}:{record["hash"]}:{now}', 'id': cid,
                           'kind': kind, 'url': record['url'], 'detected': now})
            count += 1
        seen[cid] = {key: record[key] for key in ('hash', 'question_hash', 'answer_hash')}
    state['events'] = events[-FEED_LIMIT:]
    state['initialised'] = True
    state['version'] = 3
    return count, initial


def write_feed(state, directory):
    directory.mkdir(parents=True, exist_ok=True)
    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")
    def add(parent, tag, text):
        ET.SubElement(parent, tag).text = text
    add(channel, "title", "Ivrea España: preguntas y respuestas")
    add(channel, "link", SOURCE)
    add(channel, "description", "Avisos de preguntas, respuestas y cambios detectados. Consulta el contenido en la web original.")
    add(channel, "language", "es")
    add(channel, "ttl", "60")
    for event in reversed(state['events']):
        item = ET.SubElement(channel, "item")
        label = {'answered': 'Nueva pregunta con respuesta', 'question': 'Nueva pregunta',
                 'answer_updated': 'Respuesta actualizada', 'answer_removed': 'Respuesta retirada',
                 'question_updated': 'Pregunta actualizada', 'new': 'Nueva pregunta o respuesta',
                 'updated': 'Pregunta o respuesta actualizada'}.get(event['kind'], 'Pregunta o respuesta actualizada')
        add(item, "title", f"{label} #{event['id']}")
        add(item, "link", event['url'])
        guid = ET.SubElement(item, "guid", isPermaLink="false")
        guid.text = event['guid']
        add(item, "pubDate", format_datetime(datetime.fromisoformat(event['detected'])))
        add(item, "description", "Se ha detectado una publicación o cambio. Lee la pregunta y la respuesta en la página original. La fecha indica cuándo se detectó, no cuándo se publicó.")
    ET.indent(rss)
    ET.ElementTree(rss).write(directory / "feed.xml", encoding="utf-8", xml_declaration=True)
    (directory / 'index.html').write_text(
        '<!doctype html><html lang="en"><meta charset="utf-8"><title>Ivrea RSS</title>'
        '<link rel="alternate" type="application/rss+xml" href="feed.xml">'
        '<h1>Ivrea Q&amp;A RSS</h1><p><a href="feed.xml">Subscribe to the RSS feed</a></p>'
        '<p>Link-only notifications. First successful run establishes a baseline.</p></html>', encoding='utf-8')
    (directory / '.nojekyll').touch()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--snapshot', help='Validate a saved HTML/MHTML file without fetching or changing state')
    args = parser.parse_args()
    if args.snapshot:
        records, pages = extract(load_snapshot(args.snapshot), SOURCE)
        if not records:
            raise RuntimeError('No comments detected in snapshot')
        print(json.dumps({'comments': len(records), 'answered': sum(r['answered'] for r in records.values()),
                          'pagination_urls': sorted(pages)}, indent=2))
        return
    state_path = Path('state.json')
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    records = crawl()  # Fetch everything successfully before modifying any files.
    now = datetime.now(timezone.utc).isoformat()
    count, initial = update(state, records, now)
    write_feed(state, Path('public'))
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f"Scanned {len(records)} comments; {count} new/updated events; baseline={initial}.")


if __name__ == '__main__':
    main()
