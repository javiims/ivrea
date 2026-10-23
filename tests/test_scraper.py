import tempfile
import unittest
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

    def test_migration_from_v3_state(self):
        recs = records()
        state = {'initialised': True, 'seen': {cid: r['hash'] for cid, r in recs.items()},
                 'events': [{'guid': 'x', 'id': '1', 'kind': 'new', 'url': 'u', 'detected': T0}]}
        self.assertEqual(update(state, recs, T1), (0, False))
        self.assertEqual(state['version'], 4)
        self.assertEqual(state['events'], [])  # old link-only events are dropped

    def test_mhtml_utf8(self):
        data = ('MIME-Version: 1.0\nContent-Type: multipart/related; boundary=x\n\n--x\n'
                'Content-Type: text/html\nContent-Transfer-Encoding: 8bit\n\n'
                + HTML.replace('Test question', 'Pregunta reimpresión') + '\n--x--\n')
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'test.mhtml'
            p.write_bytes(data.encode('utf-8'))
            recs = records(load_snapshot(p))
            self.assertEqual(len(recs), 2)
            self.assertIn('reimpresión', recs['10']['segments'][0][1])


if __name__ == '__main__':
    unittest.main()
