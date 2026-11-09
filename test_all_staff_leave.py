"""Leave-table OCR and the private LINE commands/menu that present it."""
import copy
import json
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from unittest import mock

from PIL import Image

from all_staff.leave_table import LEAVE_VERSION, agreed_code, code_reading, kind_reading, read_leave_table, render_leave_table
from all_staff.bot import STAFF_UI
from test_all_staff_bot import BotCase, RESULT, cells_of, flex_texts, image_event, postback_event


def nodes(node):
    """Every dict in a Flex tree, outermost first."""
    if isinstance(node, list):
        return [inner for child in node for inner in nodes(child)]
    if isinstance(node, dict):
        return [node, *(inner for child in node.values() for inner in nodes(child))]
    return []


LEAVE = {"available": True, "rest": ["72", "26"], "unreadable_rest": 1,
         "leave": [{"code": "甲", "name": ["陳小華"], "kind": "補休"},
                   {"code": None, "name": [], "kind": None}]}


class LeaveCommandTests(BotCase):
    settings = {"public_base": "https://bot.example"}

    def setUp(self):
        super().setUp()
        self.analyzer.data.update(leave_table=copy.deepcopy(LEAVE), leave_table_version=LEAVE_VERSION)
        self.analyzer.leave_table_image = lambda run: (f"{run}/leave-table-v1.jpg", f"{run}/leave-table-v1-preview.jpg")
        self.bot.handle(image_event("U1", "Cgroup"))
        self.store.set_staff_name("U2", "林小明")
        self.store.set_staff_name("U3", "陳小華")

    def test_unregistered_user_gets_one_card_of_rest_and_leave_and_the_original(self):
        reply = self.ask("休假人員")
        messages = self.transport.raw[-1]
        self.assertEqual([m['type'] for m in messages], ['flex', 'image'])
        bubble = messages[0]['contents']
        self.assertEqual((bubble['type'], bubble['size']), ('bubble', 'giga'))
        texts = flex_texts(bubble)
        self.assertEqual(texts[0], '🌴 休假人員')
        self.assertIn('💤 輪休 2 人', texts)
        self.assertIn('📝 請假 1 人', texts)
        self.assertLess(texts.index('💤 輪休人員'), texts.index('📝 請假人員'))
        # 輪休 by code, three to a row; the name not read is a quiet 「（未辨識）」 after its code
        self.assertEqual([cell['text'] for cell in cells_of(bubble)], ['26（未辨識）', '72 林小明'])
        missing = cells_of(bubble)[0]['contents'][1]
        self.assertEqual((missing['size'], missing['color']), ('xs', STAFF_UI['faint']))
        # 請假: code, name and kind on one row
        rows = [line for line in nodes(bubble) if line.get('type') == 'box' and line.get('layout') == 'horizontal'
                and flex_texts(line) == ['甲', '陳小華', '補休']]
        self.assertEqual(len(rows), 1)
        # What could not be read: one line each, never a row of 「代號？」
        self.assertIn('🔍 另有 1 格看不清楚，請看下方原圖', texts)
        self.assertIn('🔍 另有 1 筆看不清楚，請看下方原圖', texts)
        for phrase in ('代號？', '代號未辨識', '姓名未辨識', '左欄', '左右滑動'):
            self.assertNotIn(phrase, reply)
        self.assertIn('📷 下方為休假區塊原圖，可點開放大', texts[-2].splitlines())
        self.assertEqual(messages[1]['originalContentUrl'], 'https://bot.example/runs/run-1/leave-table-v1.jpg')
        self.assertEqual(bubble['footer']['contents'][0]['action']['data'], 'roster|run-1')

    def test_rest_is_a_grid_by_code_three_to_a_row_not_the_printed_columns(self):
        self.analyzer.data['leave_table']['rest_rows'] = [
            ['72', '26', '甲'], ['3', '?', '戊'], [None, '81', '?']]
        self.analyzer.data['leave_table']['rest'] = ['72', '26', '甲', '3', '戊', '81']
        del self.analyzer.data['leave_table']['unreadable_rest']   # an older reading: count the 「?」 cells
        self.ask('輪休人員')
        bubble = self.transport.raw[-1][0]['contents']
        panel = bubble['body']['contents'][0]
        self.assertEqual([[cell['text'] for cell in row['contents'] if cell['type'] == 'text'] for row in panel['contents']],
                         [['3（未辨識）', '26（未辨識）', '72 林小明'], ['81（未辨識）', '甲 陳小華', '戊（未辨識）']])
        texts = flex_texts(bubble)
        self.assertIn('🔍 另有 2 格看不清楚，請看下方原圖', texts)
        self.assertNotIn('左欄', texts)
        self.assertEqual(texts.count('💤 輪休人員'), 1)   # alone, the band says what it is; no section title

    def test_leave_rows_keep_reasons_and_quietly_mark_what_was_not_read(self):
        self.analyzer.data['leave_table']['leave'] = [
            {'code': None, 'name': ['陳小華'], 'kind': '事假'},          # the code not read, the name was
            {'code': 'J', 'name': [], 'kind': None},                       # neither name nor kind
            {'code': '67', 'name': [], 'kind': '補休', 'reason': '10/3 加班'},
            {'code': None, 'name': [], 'kind': None}, {'code': None, 'name': ['某某'], 'kind': '病假'}]
        self.ask('請假人員')
        bubble = self.transport.raw[-1][0]['contents']
        panel = bubble['body']['contents'][0]
        rows = [flex_texts(line) for line in panel['contents'] if line['type'] == 'box']
        self.assertEqual(rows, [['67', '（未辨識）', '10/3 加班', '補休'], ['J', '（未辨識）', '假別未辨識'],
                                ['—', '陳小華', '事假']])
        texts = flex_texts(bubble)
        self.assertIn('🔍 另有 2 筆看不清楚，請看下方原圖', texts)
        self.assertIn('📝 請假 3 人', texts)
        self.assertNotIn('💤 輪休人員', texts)

    def test_subset_commands_and_date_keep_the_chosen_roster(self):
        self.store.add_roster('Cgroup', 'newer', 'U2', 5, '2026-10-02')
        self.ask('輪休人員 10/1')
        self.assertIn('輪休人員', self.reply())
        self.assertNotIn('請假人員', self.reply())
        self.assertIn('10/1（四）班表', self.reply())
        self.assertEqual(self.transport.raw[-1][0]['contents']['type'], 'bubble')
        self.ask('請假人員 10/1')
        self.assertNotIn('輪休人員', self.reply())
        self.assertEqual(self.transport.raw[-1][0]['contents']['type'], 'bubble')
        self.ask('休假人員 10/1')
        action = self.transport.raw[-1][-1]['quickReply']['items'][1]['action']
        self.assertEqual(action['data'], 'leave|run-1')
        self.bot.handle(postback_event('U1', action['data']))
        self.assertIn('10/1（四）班表', self.reply())
        self.assertIn('run-1/leave-table', self.reply())

    def test_missing_leave_table_does_not_infer_leave_from_missing_duties(self):
        self.analyzer.data['leave_table'] = {'available': False}
        self.ask('休假人員')
        bubble = self.transport.raw[-1][0]['contents']
        self.assertEqual(bubble['type'], 'bubble')
        texts = flex_texts(bubble)
        self.assertIn('休假區塊尚未辨識', texts)
        self.assertIn('這張班表的輪休、請假記載沒有讀出來，請看下方原圖。', texts)
        for heading in ('💤 輪休人員', '📝 請假人員'):
            self.assertNotIn(heading, texts)
        self.assertNotIn('輪休人員（', self.reply())
        self.assertEqual(self.transport.raw[-1][-1]['type'], 'image')

    def test_old_roster_gets_read_and_failure_still_has_original(self):
        self.analyzer.data.pop('leave_table_version')
        self.analyzer.ensure_leave = mock.Mock(return_value=LEAVE)
        self.ask('休假人員')
        self.analyzer.ensure_leave.assert_called_once_with('run-1')
        self.analyzer.ensure_leave.side_effect = OSError('bad photo')
        self.ask('休假人員')
        self.assertIn('休假區塊尚未辨識', self.reply())
        self.assertEqual(self.transport.raw[-1][-1]['type'], 'image')

    def test_private_postback_cannot_read_another_users_upload_and_groups_stay_silent(self):
        self.store.add_roster('U2', 'private-run', 'U2', 5, '2026-10-03')
        count = len(self.transport.replies)
        self.bot.handle(postback_event('U1', 'leave|private-run'))
        self.bot.handle(__import__('test_all_staff_bot').text_event('U1', '休假人員', 'Cgroup'))
        self.assertEqual(len(self.transport.replies), count)

    def test_missing_date_and_missing_crop_have_clear_fallback(self):
        self.assertIn('沒有 10/5', self.ask('休假人員 10/5'))
        self.analyzer.leave_table_image = lambda run: None
        self.ask('休假人員')
        self.assertEqual(len(self.transport.raw[-1]), 1)
        self.assertNotIn('下方', self.reply())
        self.assertIn('🔍 另有 1 格看不清楚，請看整張班表', self.reply())
        link = self.transport.raw[-1][0]['contents']['footer']['contents'][0]['action']
        self.assertEqual((link['data'], link['displayText']), ('roster|run-1', '查看整張班表'))

    def test_section_postbacks_show_that_part_alone(self):
        for data, shown, hidden in (('leave|run-1|rest', '💤 輪休人員', '📝'), ('leave|run-1|leave', '📝 請假人員', '💤'),
                                    ('leave|run-1', '🌴 休假人員', None)):
            with self.subTest(data=data):
                self.bot.handle(postback_event('U1', data))
                texts = flex_texts(self.transport.raw[-1][0]['contents'])
                self.assertEqual(texts[0], shown)
                if hidden:
                    self.assertFalse(any(text.startswith(hidden) for text in texts))
                else:
                    self.assertLess(texts.index('💤 輪休人員'), texts.index('📝 請假人員'))

    def test_a_full_leave_table_is_one_bubble_and_long_reasons_fall_back_to_two(self):
        from all_staff.bot import FLEX_BUBBLE_LIMIT, FLEX_CAROUSEL_LIMIT, flex_size, leave_card
        from all_staff.schema import ALLOWED_CODES
        names = {code: '歐陽小明' for code in ALLOWED_CODES}
        full = {'available': True, 'rest': list(ALLOWED_CODES[:42]), 'unreadable_rest': 0,   # 14 rows × 3
                'leave': [{'code': code, 'name': [], 'kind': '身心調適假'} for code in ALLOWED_CODES[42:64]]}
        roster = {'run_id': 'run-1', 'roster_date': '2026-10-01'}
        card = leave_card(roster, full, names, [], show_original=True, has_image=True)
        self.assertEqual(card['contents']['type'], 'bubble')
        self.assertLessEqual(flex_size(card['contents']), FLEX_BUBBLE_LIMIT)
        for entry in full['leave']:
            entry['reason'] = '支援桃園機場安檢勤務一週'
        card = leave_card(roster, full, names, [], show_original=True, has_image=True)
        bubbles = card['contents']['contents']
        self.assertEqual([flex_texts(bubble)[0] for bubble in bubbles], ['💤 輪休人員', '📝 請假人員'])
        self.assertTrue(all(flex_size(bubble) <= FLEX_BUBBLE_LIMIT for bubble in bubbles))
        self.assertLessEqual(flex_size(card['contents']), FLEX_CAROUSEL_LIMIT)
        self.assertLessEqual(len(card['altText']), 400)

    def test_a_typical_day_is_one_small_bubble(self):
        from all_staff.bot import flex_size, leave_card
        from all_staff.schema import ALLOWED_CODES
        names = {code: '林小明' for code in ALLOWED_CODES}
        day = {'available': True, 'rest': list(ALLOWED_CODES[:27]), 'unreadable_rest': 2,
               'leave': [{'code': code, 'name': [], 'kind': '休假'} for code in ALLOWED_CODES[60:68]]}
        card = leave_card({'run_id': 'run-1', 'roster_date': '2026-10-01'}, day, names, [], show_original=True,
                          has_image=True)
        self.assertEqual(card['contents']['type'], 'bubble')
        self.assertLess(flex_size(card['contents']), 25_000)

    def test_slow_first_read_sends_result_after_reply_token_deadline(self):
        self.analyzer.data.pop('leave_table_version')
        self.analyzer.ensure_leave = mock.Mock(return_value=LEAVE)
        count = len(self.transport.replies)
        with mock.patch('all_staff.bot.time.monotonic', side_effect=[0, 60]):
            self.bot.handle(__import__('test_all_staff_bot').text_event('U1', '休假人員'))
        self.assertEqual(len(self.transport.replies), count)
        self.assertEqual(self.transport.pushes[-1][0], 'U1')
        self.assertIn('輪休人員', '\n'.join(self.transport.pushes[-1][1]))
        self.assertIn('U1', self.transport.loadings)


class LeaveReaderTests(unittest.TestCase):
    def test_code_and_kind_ambiguity_are_not_forced(self):
        self.assertEqual(code_reading('７９'), '79')
        self.assertEqual(code_reading('T'), '丁')
        self.assertIsNone(code_reading('91'))
        self.assertIsNone(agreed_code(['79', '19']))
        self.assertIsNone(agreed_code(['79', '', '']))
        self.assertEqual(agreed_code(['79', '79', '19']), '79')
        self.assertIsNone(kind_reading(['假', '假']))
        self.assertEqual(kind_reading(['退佔', '退伍']), '退伍')
        self.assertEqual(kind_reading(['', '病假', '']), '病假')
        self.assertEqual(kind_reading(['身心調過人', '租心調點條', '出心調硬代']), '身心調適假')
        self.assertEqual(kind_reading(['性防假', '恬防假', '如防假']), '災防假')
        self.assertIsNone(kind_reading(['休假', '補休']))
        self.assertEqual(kind_reading(['補半', '補半']), '補半')
        self.assertIsNone(kind_reading(['補和', '補和']))
        self.assertEqual(kind_reading(['鶴休', '神和休', '窒休', '補和休']), '補休')
        self.assertEqual(kind_reading(['曝伍', '四伍', '選位']), '退伍')
        self.assertEqual(kind_reading(['榮譽假', '榮絃假']), '榮譽假')
        self.assertEqual(code_reading('成|'), '戊')

    def test_real_code_pixels_with_fictional_names_recover_all_three_rest_lanes(self):
        if not shutil.which('tesseract'):
            self.skipTest('tesseract not available')
        path = Path(__file__).resolve().parent / 'assets/test_fixtures/leave-table-sample.png'
        with Image.open(path) as source:
            page = source.copy()
        directory = ['王大同', '陳小華', '李小光', '周小玲', '張小安', '林小明', '黃小美']
        names = dict(zip(['7', '8', '70', 'H', 'B'], directory[:5]))
        with mock.patch('all_staff.leave_table.warp_page', return_value=page):
            data = read_leave_table(path, None, names=names, directory=directory, fixed_codes={'壬':'林小明','子':'黃小美'})
        expected = {'3','6','9','12','15','18','21','24','27','30','34','35','59','60','64','65','66','69',
                    '74','75','79','80','81','A','C','I','丁'}
        self.assertTrue(data['available'])
        self.assertEqual(set(data['rest']), expected)
        self.assertEqual(data['unreadable_rest'], 0)
        self.assertEqual([p['code'] for p in data['leave']], ['7','8','70','H','B','壬','子'])
        # A faint field may stay unreadable; it must never become a different kind.
        for entry, expected_kind in zip(data['leave'], ['休假'] * 4 + ['補休', '退伍', '退伍']):
            self.assertIn(entry['kind'], (expected_kind, None))
        self.assertEqual(data['leave'][0]['kind'], '休假')
        self.assertEqual(data['leave'][4]['kind'], '補休')

    def test_original_crop_uses_source_pixels_and_excludes_adjacent_sections(self):
        image = Image.new('RGB', (100,100), 'red')
        for x in range(60,95):
            for y in range(40,80): image.putpixel((x,y),(0,128,0))
        with mock.patch('all_staff.leave_table.warp_page', return_value=image), \
             mock.patch('all_staff.leave_table.locate_leave', return_value={'bounds':(60,40,95,80)}):
            crop = render_leave_table(Path('photo.jpg'), None)
        self.assertEqual(crop.size,(35,40))
        self.assertEqual(set(crop.getdata()),{(0,128,0)})

    def test_saved_roster_is_read_once_and_original_fields_are_preserved(self):
        from all_staff.demo import DemoApp
        with tempfile.TemporaryDirectory() as temp:
            app=DemoApp.__new__(DemoApp)
            app.root=Path(__file__).resolve().parent
            app.template=app.root/'configs/all_staff_roster_template.local.json'
            app.runs=Path(temp); app.lock=threading.Lock()
            folder=app.runs/'run-1'; (folder/'upload').mkdir(parents=True)
            (folder/'upload/photo.jpg').write_bytes(b'photo')
            original=copy.deepcopy(RESULT)
            (folder/'result.json').write_text(json.dumps(original,ensure_ascii=False))
            with mock.patch('all_staff.leave_table.read_leave_table',return_value=LEAVE) as read:
                self.assertEqual(app.ensure_leave('run-1'), LEAVE)
                self.assertEqual(app.ensure_leave('run-1'), LEAVE)
            read.assert_called_once()
            updated=app.result('run-1')
            self.assertEqual(updated.pop('leave_table'),LEAVE)
            self.assertEqual(updated.pop('leave_table_version'),LEAVE_VERSION)
            self.assertEqual(updated,original)


if __name__ == '__main__':
    unittest.main()
