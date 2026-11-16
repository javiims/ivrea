import email.message
import io
import os
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
import xml.etree.ElementTree as ET
from unittest import mock

import scraper
from scraper import extract, update, write_feed, load_snapshot, split_segments, SOURCE

HTML = '''<li id="comment-10"><article class="comment-body"><span class="comment-author"><cite class="fn">Ana</cite></span>
<div class="comment-meta"><a class="comment-date" href="https://www.editorialivrea.com/ESP/preguntas/comment-page-8/#comment-10"><time datetime="2026-10-20T21:15:10+02:00">21:15</time></a></div>
<div class="comment-content"><p>Test question<br/>
second line<br/>
<strong><br/>
(((Test answer -DANIEL)))</strong></p></div></article>
<ul class="children"><li id="comment-11"><article class="comment-body"><div class="comment-content">Unanswered test question</div></article></li></ul></li>
<a class="prev page-numbers" href="/ESP/preguntas/comment-page-7/#comments">Anterior</a>
<a href="/ESP/preguntas/comment-page-7/?replytocom=10#respond">Reply</a>'''

API_URL = 'https://www.editorialivrea.com/ESP/preguntas/wp-json/wp/v2/comments'
T0, T1, T2 = '2026-01-01T00:00:00+00:00', '2026-01-02T00:00:00+00:00', '2026-01-03T00:00:00+00:00'


def records(html=HTML):
    return extract(html, SOURCE)[0]


class Tests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(scraper, 'BASELINE_ITEMS', 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_structure_and_pagination(self):
        recs, pages, prev_page = extract(HTML, SOURCE)
        self.assertEqual(set(recs), {'10', '11'})
        self.assertTrue(recs['10']['answered'])
        self.assertFalse(recs['11']['answered'])
        self.assertEqual(recs['10']['author'], 'Ana')
        self.assertEqual(recs['10']['segments'], [('q', 'Test question\nsecond line'), ('a', 'Test answer -DANIEL')])
        self.assertEqual(prev_page, 'https://www.editorialivrea.com/ESP/preguntas/comment-page-7/')
        self.assertIn(prev_page, pages)
        changed = records(HTML.replace('Unanswered test question', 'Changed child'))
        self.assertEqual(recs['10']['hash'], changed['10']['hash'])

    def test_interleaved_segments(self):
        segs = split_segments('Q1\n(((A1)))\n\nQ2\n(((A2)))')
        self.assertEqual(segs, [('q', 'Q1'), ('a', 'A1'), ('q', 'Q2'), ('a', 'A2')])

    def test_feed_contains_question_and_answer(self):
        state = {}
        self.assertEqual(update(state, records(), T0), (0, True))
        new = records(HTML.replace('comment-10', 'comment-12'))
        self.assertEqual(update(state, new, T1), (1, False))
        with tempfile.TemporaryDirectory() as d:
            write_feed(state, Path(d))
            items = ET.parse(Path(d) / 'feed.xml').findall('./channel/item')
            self.assertEqual(len(items), 1)
            self.assertTrue(items[0].find('title').text.startswith('Test question'))
            desc = items[0].find('description').text
            self.assertIn('Test question', desc)
            self.assertIn('Test answer -DANIEL', desc)
            self.assertNotIn('(((', desc)

    def test_answer_edit_and_no_duplicates(self):
        state = {}
        update(state, records(), T0)
        changed = records(HTML.replace('Test answer', 'Changed answer'))
        self.assertEqual(update(state, changed, T1), (1, False))
        self.assertEqual(state['events'][-1]['kind'], 'updated')
        self.assertEqual(update(state, changed, T2), (0, False))

    def test_baseline_seed(self):
        state = {}
        with mock.patch.object(scraper, 'BASELINE_ITEMS', 1):
            self.assertEqual(update(state, records(), T0), (0, True))
        self.assertEqual([e['id'] for e in state['events']], ['11'])

    def test_old_state_is_rebaselined(self):
        recs = records()
        state = {'initialised': True, 'version': 4, 'seen': {'1': 'x'},
                 'events': [{'guid': 'x', 'id': '1', 'kind': 'new', 'url': 'u', 'detected': T0}]}
        self.assertEqual(update(state, recs, T1), (0, True))
        self.assertEqual(state['version'], scraper.STATE_VERSION)
        self.assertEqual(state['events'], [])
        self.assertEqual(set(state['seen']), {'10', '11'})

    def test_feed_has_no_item_link(self):
        state = {}
        update(state, records(), T0)
        update(state, records(HTML.replace('comment-10', 'comment-12')), T1)
        with tempfile.TemporaryDirectory() as d:
            write_feed(state, Path(d))
            item = ET.parse(Path(d) / 'feed.xml').find('./channel/item')
            self.assertIsNone(item.find('link'))
            self.assertNotIn('href', item.find('description').text)

    @staticmethod
    def _api(*ids):
        return scraper.json.dumps([{
            'id': i, 'status': 'approved', 'author_name': f'Lector {i}',
            'date_gmt': '2026-10-23T10:00:00',
            'link': f'https://www.editorialivrea.com/ESP/preguntas/comment-page-22/#comment-{i}',
            'content': {'rendered': f'<p>Pregunta {i}<br />\n<strong><br />\n(((Respuesta {i} -DANIEL)))</strong></p>\n'}}
            for i in ids])

    def _crawl(self, payload):
        calls = []
        def fake(url, **kw):
            calls.append(url)
            return payload, API_URL
        with mock.patch.object(scraper, 'fetch_http', side_effect=fake):
            return scraper.crawl(), calls

    def test_api_source(self):
        recs, calls = self._crawl(self._api(600))
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0].startswith('https://editorialivrea.com/ESP/preguntas/wp-json/wp/v2/comments?'))
        self.assertIn('per_page=100', calls[0])
        self.assertEqual(recs['600']['segments'], [('q', 'Pregunta 600'), ('a', 'Respuesta 600 -DANIEL')])
        self.assertEqual(recs['600']['author'], 'Lector 600')
        self.assertEqual(recs['600']['published'], '2026-10-23T10:00:00+00:00')

    def test_older_comments_leaving_the_api_are_not_a_problem(self):
        state = {}
        update(state, self._crawl(self._api(3, 2, 1))[0], T0)
        # 1 dropped out of the API, 4 is new
        self.assertEqual(update(state, self._crawl(self._api(4, 3, 2))[0], T1), (1, False))
        self.assertEqual(state['events'][-1]['id'], '4')
        self.assertIn('1', state['seen'])
        # 1 dropped earlier and now 2 as well: still nothing new
        self.assertEqual(update(state, self._crawl(self._api(4, 3))[0], T2), (0, False))

    def test_api_errors_do_not_touch_state(self):
        for payload in ('<html>blocked</html>', '[]', '{"code": "rest_disabled"}'):
            with self.subTest(payload=payload):
                with self.assertRaises(RuntimeError):
                    self._crawl(payload)

    @staticmethod
    def _feed(*ids):
        items = ''.join(
            '<item><title>Por: Lector {0}</title>'
            '<link>https://www.editorialivrea.com/ESP/preguntas/comment-page-22/#comment-{0}</link>'
            '<dc:creator><![CDATA[Lector {0}]]></dc:creator>'
            '<pubDate>Fri, 23 Oct 2026 10:00:00 +0000</pubDate>'
            '<guid isPermaLink="false">https://www.editorialivrea.com/ESP/preguntas/#comment-{0}</guid>'
            '<description><![CDATA[Pregunta {0}]]></description>'
            '<content:encoded><![CDATA[<p>Pregunta {0}<br />\n<strong><br />\n'
            '(((Respuesta {0} -DANIEL)))</strong></p>\n]]></content:encoded></item>'.format(i) for i in ids)
        return ('<?xml version="1.0" encoding="UTF-8"?><rss version="2.0" '
                'xmlns:content="http://purl.org/rss/1.0/modules/content/" '
                'xmlns:dc="http://purl.org/dc/elements/1.1/"><channel><title>Comentarios</title>'
                + items + '</channel></rss>')

    def _crawl_blocked(self, answers):
        """Every address answers 403 Forbidden, except those starting with a key of `answers`."""
        calls = []
        def fake(url, **kw):
            calls.append(url)
            for prefix, text in answers.items():
                if url.startswith(prefix):
                    return text, url
            raise scraper.SourceError('HTTP Error 403: Forbidden')
        with mock.patch.object(scraper, 'fetch_http', side_effect=fake):
            return scraper.crawl(), calls

    def test_blocked_api_falls_back_to_other_addresses(self):
        rest_route = 'https://www.editorialivrea.com/ESP/preguntas/?rest_route=/wp/v2/comments'
        recs, calls = self._crawl_blocked({rest_route: self._api(600)})
        self.assertEqual(len(calls), 3)
        self.assertTrue(calls[1].startswith('https://www.editorialivrea.com/ESP/preguntas/wp-json/wp/v2/comments?'))
        self.assertTrue(calls[2].startswith(rest_route + '&per_page=100'))
        self.assertEqual(set(recs), {'600'})

    def test_comments_feed_gives_the_same_entries_as_the_api(self):
        from_api, _ = self._crawl(self._api(600, 601))
        from_feed, calls = self._crawl_blocked({scraper.COMMENTS_FEED: self._feed(600, 601)})
        self.assertEqual(len(calls), 4)
        self.assertEqual(calls[-1], scraper.COMMENTS_FEED)
        self.assertEqual(from_feed, from_api)
        # so switching between both sources reports nothing as new or edited
        state = {}
        update(state, from_api, T0)
        self.assertEqual(update(state, from_feed, T1), (0, False))

    def test_every_source_blocked(self):
        with self.assertRaises(scraper.SourcesUnavailable):
            self._crawl_blocked({})

    def test_forbidden_is_not_retried(self):
        error = urllib.error.HTTPError('https://x/api', 403, 'Forbidden', email.message.Message(),
                                       io.BytesIO(b'<html>Request blocked</html>'))
        with mock.patch.object(scraper.urllib.request, 'urlopen', side_effect=error) as urlopen, \
                mock.patch.object(scraper.time, 'sleep') as sleep:
            with self.assertRaises(scraper.SourceError):
                scraper.fetch_http('https://x/api')
        self.assertEqual(urlopen.call_count, 1)
        sleep.assert_not_called()

    def test_network_errors_are_retried(self):
        with mock.patch.object(scraper.urllib.request, 'urlopen',
                               side_effect=urllib.error.URLError('timed out')) as urlopen, \
                mock.patch.object(scraper.time, 'sleep') as sleep:
            with self.assertRaises(scraper.SourceError):
                scraper.fetch_http('https://x/api')
        self.assertEqual(urlopen.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def _run_main(self, state, **crawl):
        """Run main() in an empty folder; return the saved state.json text and whether a feed was written."""
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'state.json'
            if state is not None:
                path.write_text(scraper.json.dumps(state), encoding='utf-8')
            os.chdir(d)
            try:
                with mock.patch.object(scraper, 'crawl', **crawl), mock.patch('sys.argv', ['scraper.py']):
                    scraper.main()
            finally:
                os.chdir(cwd)
            return (path.read_text(encoding='utf-8') if path.exists() else None,
                    (Path(d) / 'public' / 'feed.xml').exists())

    def test_successful_run_saves_state_and_feed(self):
        text, feed = self._run_main(None, return_value=records())
        state = scraper.json.loads(text)
        self.assertIn('last_success', state)
        self.assertEqual(set(state['seen']), {'10', '11'})
        self.assertTrue(feed)

    def test_blocked_run_is_skipped_until_it_lasts_too_long(self):
        def ago(hours):
            return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        blocked = scraper.SourcesUnavailable('blocked')
        state = {'last_success': ago(5), 'events': []}
        self.assertEqual(self._run_main(state, side_effect=blocked), (scraper.json.dumps(state), False))
        # a recently detected entry also counts as a successful read
        self._run_main({'events': [{'detected': ago(5)}]}, side_effect=blocked)
        for state in ({'last_success': ago(scraper.MAX_BLOCKED_HOURS + 1)}, {}):
            with self.subTest(state=state), self.assertRaises(RuntimeError):
                self._run_main(state, side_effect=blocked)

    def test_last_success_is_saved_at_most_daily(self):
        state = {}
        scraper.note_success(state, T0)
        scraper.note_success(state, '2026-01-01T12:00:00+00:00')
        self.assertEqual(state['last_success'], T0)
        scraper.note_success(state, T1)
        self.assertEqual(state['last_success'], T1)


if __name__ == '__main__':
    unittest.main()
