"""Repeated-digit regression checks and preservation of saved roster data."""
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from all_staff.demo import DemoApp
from all_staff.lines import DUTY_VERSION, LineOCR
from all_staff.schema import Box
from all_staff.segment import Line


class NumericRecheckTests(unittest.TestCase):
    def read(self, original, glyphs):
        engine = LineOCR.__new__(LineOCR)
        engine.torch, engine.model = mock.MagicMock(), mock.MagicMock()
        engine.size, engine.context, engine.device = (128, 32), {'fx': 0, 'fy': 0}, 'cpu'
        boxes = [Box(10 + i * 25, 10, 24 + i * 25, 40) for i in range(len(glyphs))]
        line = Line(Box(0, 0, 100, 50), tuple(boxes))
        with mock.patch('all_staff.lines.segment_lines', return_value=[line]), \
             mock.patch('all_staff.lines.line_tensor'), \
             mock.patch('all_staff.lines.decode', side_effect=[[(original, .97)], glyphs]):
            return engine.read_lines(Image.new('L', (100, 50), 255))[0]

    def test_repeated_digits_do_not_collapse_to_one_digit(self):
        for digit in ('1', '2', '3'):
            with self.subTest(digit=digit):
                line, text, confidence = self.read(digit, [(digit, .99), (digit, .98)])
                self.assertEqual(text, digit * 2)
                self.assertEqual(line.original_text, digit)
                self.assertAlmostEqual(confidence, .98)

    def test_single_digit_is_not_duplicated(self):
        for digit in ('1', '2', '3'):
            with self.subTest(digit=digit):
                line, text, _ = self.read(digit, [(digit, .99)])
                self.assertEqual(text, digit)
                self.assertFalse(line.original_text)

    def test_low_confidence_or_letter_glyph_does_not_reassign_person(self):
        for glyphs in ([('2', .99), ('2', .6)], [('2', .99), ('B', .99)]):
            with self.subTest(glyphs=glyphs):
                line, text, _ = self.read('2', glyphs)
                self.assertEqual(text, '2')
                self.assertFalse(line.original_text)

    def test_high_confidence_non_code_suffix_can_be_blank(self):
        _, text, _ = self.read('2', [('2', .99), ('2', .99), ('', .99)])
        self.assertEqual(text, '22')


class StoredDutyRecheckTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.app = DemoApp.__new__(DemoApp)
        self.app.runs, self.app.lock = Path(self.folder.name), threading.Lock()
        self.app.engine, self.app.context = object(), {'fx': 0, 'fy': 0}
        run = self.app.runs / 'run-1'
        (run / 'cells/images').mkdir(parents=True)
        Image.new('L', (80, 80), 255).save(run / 'cells/images/cell.png')
        cell = {'id': 'cell', 'r': 4, 'c': 2, 'img': '/runs/run-1/cells/images/cell.png',
                'lines': [{'text': '2'}], 'pred': [{'code': '2', 'conf': .97}],
                'truth': [{'code': '22', 'role': 'personnel_code'}]}
        self.before = {'run': {'id': 'run-1', 'codes': 1, 'roster_date': '2026-10-03'},
                       'names': {'35': ['沈姻姻']}, 'names_version': 3,
                       'leave_table': {'available': True, 'rest': ['35']}, 'leave_table_version': 1,
                       'allowed': ['2', '22'], 'roster': {}, 'time_rows': {'4': '05-06'}, 'column_names': {'2': '國際線'},
                       'people': [{'code': '2', 'slots': []}], 'sheets': [{'cells': [cell]}]}
        self.path = run / 'result.json'
        self.path.write_text(json.dumps(self.before))

    def tearDown(self):
        self.folder.cleanup()

    def test_recheck_is_cached_and_keeps_dates_names_leave_and_truth(self):
        cell = {**self.before['sheets'][0]['cells'][0], 'pred': [{'code': '22', 'conf': .99}],
                'lines': [{'text': '22', 'original_text': '2'}]}
        with mock.patch('all_staff.viewer.cell_payload', return_value=cell) as read:
            after = self.app.ensure_duties('run-1')
            self.assertEqual(self.app.ensure_duties('run-1'), after)
        self.assertEqual(read.call_count, 1)
        for key in ('names', 'names_version', 'leave_table', 'leave_table_version'):
            self.assertEqual(after[key], self.before[key])
        self.assertEqual(after['run'], self.before['run'])
        self.assertEqual(after['people'][0]['code'], '22')
        self.assertEqual(after['duty_version'], DUTY_VERSION)
        self.assertEqual(after['sheets'][0]['cells'][0]['truth'], cell['truth'])
        self.assertEqual(list(self.path.parent.glob('*.tmp')), [])

    def test_cells_under_a_note_are_not_rechecked_and_stay_empty(self):
        data = json.loads(self.path.read_text())
        data['sheets'][0]['cells'].append({
            'id': 'note', 'r': 19, 'c': 6, 'img': '/runs/run-1/cells/images/cell.png', 'lines': [{'text': '2'}],
            'pred': [], 'ignored': [{'code': '2', 'conf': .97}], 'overlay': 'box'})
        self.path.write_text(json.dumps(data))
        cell = {**data['sheets'][0]['cells'][0], 'pred': [{'code': '22', 'conf': .99}],
                'lines': [{'text': '22', 'original_text': '2'}]}
        with mock.patch('all_staff.viewer.cell_payload', return_value=cell) as read:
            after = self.app.ensure_duties('run-1')
        self.assertEqual(read.call_count, 1)   # only the ordinary cell is read again
        stored = {c['id']: c for c in after['sheets'][0]['cells']}
        self.assertEqual(stored['note']['pred'], [])
        self.assertEqual(stored['note']['overlay'], 'box')
        self.assertEqual([p['code'] for person in after['people'] for p in [person]], ['22'])

    def test_missing_image_does_not_save_partial_result(self):
        (self.path.parent / 'cells/images/cell.png').unlink()
        with self.assertRaises(FileNotFoundError):
            self.app.ensure_duties('run-1')
        self.assertEqual(json.loads(self.path.read_text()), self.before)


class RealDigitPhotoTests(unittest.TestCase):
    def test_production_checkpoint_on_reported_cells(self):
        root = Path(__file__).parent
        checkpoint = Path(os.environ.get('ALL_STAFF_TEST_RECOGNIZER', root / 'models/all_staff/line-v4/recognizer.pt'))
        if not checkpoint.is_file():
            self.skipTest('production checkpoint unavailable; set ALL_STAFF_TEST_RECOGNIZER')
        import torch
        torch.set_num_threads(1)
        engine = LineOCR(checkpoint)
        folder = root / 'assets/test_fixtures/digit-cells-2026-10-03'
        for item in json.loads((folder / 'expected.json').read_text()):
            with self.subTest(image=item['image']), Image.open(folder / item['image']) as image:
                self.assertEqual(sorted(p.code for p in engine.predict_cell(image)), sorted(item['expected']))


if __name__ == '__main__':
    unittest.main()
