import tempfile
import unittest
from pathlib import Path
import xml.etree.ElementTree as ET
from scraper import extract, update, write_feed, load_snapshot, SOURCE

HTML = '''<li id="comment-10"><article class="comment-body"><a class="comment-date" href="https://www.editorialivrea.com/ESP/preguntas/comment-page-8/#comment-10">Date</a><div class="comment-content"><p>Test question</p><p><strong>(((Test answer)))</strong></p></div></article><ul class="children"><li id="comment-11"><article class="comment-body"><div class="comment-content">Unanswered test question</div></article></li></ul></li><a href="/ESP/preguntas/comment-page-7/#comments">Previous</a><a href="/ESP/preguntas/comment-page-7/?replytocom=10#respond">Reply</a>'''

class Tests(unittest.TestCase):
    def records(self, html=HTML):
        return extract(html, SOURCE)[0]

    def test_structure_and_pagination(self):
        records, pages = extract(HTML, SOURCE)
        self.assertEqual(set(records), {'10', '11'})
        self.assertTrue(records['10']['answered'])
        self.assertFalse(records['11']['answered'])
        self.assertEqual(pages, {'https://www.editorialivrea.com/ESP/preguntas/comment-page-7/', 'https://www.editorialivrea.com/ESP/preguntas/comment-page-8/'})
        changed = self.records(HTML.replace('Unanswered test question', 'Changed child'))
        self.assertEqual(records['10']['hash'], changed['10']['hash'])

    def test_baseline_and_answer_edit(self):
        state = {}
        self.assertEqual(update(state, self.records(), '2026-01-01T00:00:00+00:00'), (0, True))
        changed = self.records(HTML.replace('Test answer', 'Changed answer'))
        self.assertEqual(update(state, changed, '2026-01-02T00:00:00+00:00'), (1, False))
        self.assertEqual(state['events'][-1]['kind'], 'updated')
        self.assertEqual(update(state, changed, '2026-01-03T00:00:00+00:00'), (0, False))
        with tempfile.TemporaryDirectory() as d:
            write_feed(state, Path(d))
            self.assertEqual(len(ET.parse(Path(d)/'feed.xml').findall('./channel/item')), 1)
            self.assertNotIn('Changed answer', (Path(d)/'feed.xml').read_text())

    def test_later_answer(self):
        state = {}
        update(state, self.records(), '2026-01-01T00:00:00+00:00')
        changed = self.records(HTML.replace('Unanswered test question', 'Unanswered test question <strong>(((Later answer)))</strong>'))
        self.assertEqual(update(state, changed, '2026-01-02T00:00:00+00:00'), (1, False))
        self.assertEqual(state['events'][-1]['kind'], 'updated')

    def test_new_answered_comment(self):
        state = {}
        update(state, self.records(), '2026-01-01T00:00:00+00:00')
        records = self.records(HTML.replace('comment-10', 'comment-12'))
        self.assertEqual(update(state, records, '2026-01-02T00:00:00+00:00'), (1, False))

    def test_migration(self):
        records = self.records()
        state = {'initialised': True, 'seen': {cid:r['hash'] for cid,r in records.items()}, 'events': []}
        self.assertEqual(update(state, records, '2026-01-01T00:00:00+00:00'), (0, False))
        self.assertEqual(state['version'], 3)

    def test_new_entry_without_marker(self):
        state = {}
        update(state, self.records(), '2026-01-01T00:00:00+00:00')
        records = self.records(HTML.replace('comment-11', 'comment-13'))
        self.assertEqual(update(state, records, '2026-01-02T00:00:00+00:00'), (1, False))
        self.assertEqual(state['events'][-1]['kind'], 'new')

    def test_mhtml(self):
        data = 'MIME-Version: 1.0\nContent-Type: multipart/related; boundary=x\n\n--x\nContent-Type: text/html; charset=utf-8\n\n' + HTML + '\n--x--\n'
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/'test.mhtml'
            p.write_text(data)
            self.assertEqual(len(self.records(load_snapshot(p))), 2)

if __name__ == '__main__':
    unittest.main()
