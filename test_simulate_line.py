import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from simulate_line import discover, simulate_one, write_index


class SimulationTests(unittest.IsolatedAsyncioTestCase):
    def test_discovery_checks_full_range_and_duplicate_dates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for day in (10,11): (root/f'9:{day}.jpg').touch()
            self.assertEqual(len(discover(root,10,11)),2)
            with self.assertRaises(ValueError): discover(root,10,12)
            (root/'9_10.png').touch()
            with self.assertRaises(ValueError): discover(root,10,11)

    async def test_conversation_saves_actual_text_images_and_sequence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'9:18.jpg';Image.new('RGB',(20,20),'white').save(source)
            async def analyze(payload,**kwargs):
                self.assertTrue(kwargs['all_employees'])
                self.assertTrue(kwargs['use_final_markdown'])
                kwargs['artifacts_dir'].mkdir()
                return {'employees':[{'employee_code':'戊','employee_name':'測試','is_day_off':False,
                    'assignments':[{'time':'10-11','shift':'國際線','needs_work':True,'confidence':.99}],
                    'crop_image':{'filename':'proof.jpg','content':source.read_bytes()}}],
                    'usage':{'api_calls':4,'estimated_usd':.01}}
            corrected = {'filename':'perspective-corrected.jpg','content':source.read_bytes()}
            with patch('simulate_line.create_corrected_schedule_preview',return_value=corrected), \
                 patch('simulate_line.analyze_schedule_image',side_effect=analyze):
                result=await simulate_one(source,root/'request')
            self.assertEqual(result['status'],'completed')
            messages=json.loads((root/'request/conversation.json').read_text())
            self.assertEqual([x['type'] for x in messages],['image','text','image','text','image','text'])
            self.assertIn('摘要｜上班1h｜休息0h',messages[3]['text'])
            self.assertEqual((root/'request/perspective-corrected.jpg').read_bytes(),source.read_bytes())
            self.assertEqual((root/'request/戊.jpg').read_bytes(),source.read_bytes())
            self.assertTrue((root/'request/result.json').exists())
            write_index(root,[result])
            index = (root/'index.html').read_text()
            self.assertIn('conversation.html', index)
            self.assertIn('總時間', index)
            self.assertNotIn('API 次數', index)
            self.assertNotIn('估算費用', index)

    async def test_failure_preserves_input_and_error_reply(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'input.jpg';source.write_bytes(b'photo')
            corrected = {'filename':'perspective-corrected.jpg','content':b'corrected'}
            with patch('simulate_line.create_corrected_schedule_preview',return_value=corrected), \
                 patch('simulate_line.analyze_schedule_image',side_effect=RuntimeError('test failure')):
                result=await simulate_one(source,root/'failed')
            self.assertEqual(result['status'],'failed')
            self.assertTrue((root/'failed/error.txt').exists())
            self.assertTrue((root/'failed/input.jpg').exists())
            self.assertTrue((root/'failed/conversation.html').exists())
            messages=json.loads((root/'failed/conversation.json').read_text())
            self.assertEqual([x['type'] for x in messages],['image','text','image','text'])
            self.assertEqual((root/'failed/perspective-corrected.jpg').read_bytes(),b'corrected')
            self.assertEqual(result['observed_usage']['api_calls'],0)


if __name__=='__main__': unittest.main()
