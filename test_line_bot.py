import os
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import line_bot
from PIL import Image
from line_bot import (
    _is_image_message_event,
    _save_crop,
    _target_id,
    format_line_error,
    format_line_report,
    format_line_total_price,
)


class LineBotTests(unittest.TestCase):
    def test_only_image_messages_trigger_in_direct_and_group_chats(self):
        for source in (
            {"type": "user", "userId": "U-sender"},
            {"type": "group", "groupId": "C-group", "userId": "U-sender"},
        ):
            self.assertTrue(_is_image_message_event({
                "type": "message", "source": source,
                "message": {"type": "image", "id": "image-id"},
            }))
            self.assertFalse(_is_image_message_event({
                "type": "message", "source": source,
                "message": {"type": "text", "text": "@bot"},
            }))

    def test_push_target_prefers_the_group_or_room_over_the_sender(self):
        self.assertEqual(
            _target_id({"source": {
                "type": "group", "groupId": "C-group", "userId": "U-sender",
            }}),
            "C-group",
        )
        self.assertEqual(
            _target_id({"source": {
                "type": "room", "roomId": "R-room", "userId": "U-sender",
            }}),
            "R-room",
        )
        self.assertEqual(
            _target_id({"source": {"type": "user", "userId": "U-sender"}}),
            "U-sender",
        )

    def test_report_keeps_uncertain_hours_blank_in_the_eight_hour_window(self):
        report = format_line_report({
            "document_date": "2026-09-10",
            "employee_name": "蕭志明",
            "employee_code": "己",
            "schedule_type": "international",
            "is_day_off": False,
            "audit": {"passed": False},
            "shift_slots": ["06-07", "07-08", "08-09", "09-10", "10-11", "11-12", "12-13", "13-14"],
            "assignments": [
                {"time": "06-07", "shift": "國際線", "needs_work": True, "confidence": 0.95, "note": ""},
                {"time": "07-08", "shift": "國際線（二）", "needs_work": False, "confidence": 0.7, "note": "字跡不清"},
                {"time": "08-09", "shift": "國際線", "needs_work": True, "confidence": 0.49, "note": "字跡不清"},
            ],
        })

        self.assertIn("蕭志明（己）9/10", report)
        self.assertIn("摘要｜上班1h｜休息1h", report)
        self.assertIn("• 06-07｜上班｜國際線", report)
        self.assertIn("• 07-08｜休息?｜國際二線", report)
        self.assertIn("• 08-09｜—\n", report)
        self.assertNotIn("08-09｜上班｜國際線", report)
        self.assertIn("• 13-14｜—", report)
        self.assertNotIn("日期待確認", report)
        self.assertNotIn("班別：", report)
        self.assertNotIn("已辨識", report)
        self.assertNotIn("逐時班表", report)
        self.assertNotIn("下圖圖例", report)

    def test_report_shows_conflict_without_counting_it_as_work(self):
        report = format_line_report({
            "employee_name": "莊雅婷", "employee_code": "庚", "is_day_off": False,
            "shift_slots": ["10-11", "11-12"],
            "assignments": [
                {"time": "10-11", "shift": "國際線", "needs_work": True,
                 "confidence": 0.95, "note": ""},
            ],
            "assignment_conflicts": [{
                "time": "11-12",
                "candidates": [
                    {"shift": "國際線", "confidence": 0.86},
                    {"shift": "國內工作管制門", "confidence": 0.82},
                ],
            }],
        })
        self.assertIn("摘要｜上班1h｜休息0h｜待確認1h", report)
        self.assertIn("• 11-12｜待確認", report)
        self.assertIn("　國際線86%／管制門82%", report)

    def test_crop_creates_separate_optimized_original_and_preview(self):
        source = BytesIO()
        Image.new("RGB", (1600, 1000), "white").save(source, "PNG")
        with tempfile.TemporaryDirectory() as directory, patch.object(
            line_bot, "CROP_DIRECTORY", Path(directory)
        ), patch.dict(os.environ, {
            "LINE_PUBLIC_BASE_URL": "https://example.test",
        }):
            urls = _save_crop({
                "filename": "丁-highlighted.jpg",
                "content": source.getvalue(),
            })
            files = sorted(Path(directory).glob("*.jpg"))

            self.assertIsNotNone(urls)
            self.assertNotEqual(urls["original"], urls["preview"])
            self.assertTrue(urls["original"].endswith("-original.jpg"))
            self.assertTrue(urls["preview"].endswith("-preview.jpg"))
            self.assertEqual(len(files), 2)
            preview = next(path for path in files if path.name.endswith("-preview.jpg"))
            with Image.open(preview) as image:
                self.assertLessEqual(max(image.size), 720)

    def test_error_explains_line_image_url_problem(self):
        message = format_line_error(RuntimeError(
            'LINE API 400: {"property":"messages[1].originalContentUrl"}'
        ))

        self.assertIn("已辨識完成", message)
        self.assertIn("不是照片不清楚", message)

    def test_total_price_is_rendered_as_the_final_line_message(self):
        message = format_line_total_price({"usage": {"estimated_usd": 0.0123456, "api_calls": 6}})
        self.assertEqual(message, "本次 API 總價（估算）：US$0.012346｜API 6 次")


class LineReplySequenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_reply_omits_api_price(self):
        pushed = []

        async def push(_target_id, messages):
            pushed.extend(messages)

        corrected = {"filename": "perspective-corrected.jpg", "content": b"corrected"}
        employee = {
            "employee_name": "測試員工",
            "employee_code": "丁",
            "is_day_off": True,
        }
        with patch("line_bot._fetch_image", return_value=(b"photo", "image/jpeg")), \
             patch("line_bot.create_corrected_schedule_preview", return_value=corrected), \
             patch("line_bot._save_crop", return_value={
                 "original": "https://example.test/corrected.jpg",
                 "preview": "https://example.test/corrected-preview.jpg",
             }), \
             patch("line_bot.analyze_schedule_image", return_value={
                 "employees": [employee],
                 "usage": {"estimated_usd": 1.25, "api_calls": 9},
             }), \
             patch("line_bot._push_messages", side_effect=push):
            await line_bot._process_image("user-id", "message-id")

        texts = [message["text"] for message in pushed if message["type"] == "text"]
        self.assertEqual(texts, ["測試員工（丁）\n當日休假"])
        self.assertFalse(any("API 總價" in text for text in texts))

    async def test_corrected_image_is_pushed_before_recognition_error(self):
        pushed = []

        async def push(_target_id, messages):
            pushed.extend(messages)

        corrected = {"filename": "perspective-corrected.jpg", "content": b"corrected"}
        with patch("line_bot._fetch_image", return_value=(b"photo", "image/jpeg")), \
             patch("line_bot.create_corrected_schedule_preview", return_value=corrected), \
             patch("line_bot._save_crop", return_value={
                 "original": "https://example.test/corrected.jpg",
                 "preview": "https://example.test/corrected-preview.jpg",
             }), \
             patch("line_bot.analyze_schedule_image", side_effect=RuntimeError("test failure")), \
             patch("line_bot._push_messages", side_effect=push):
            await line_bot._process_image("user-id", "message-id")

        self.assertEqual(pushed[0]["type"], "image")
        self.assertEqual(pushed[0]["originalContentUrl"], "https://example.test/corrected.jpg")
        self.assertEqual(pushed[0]["previewImageUrl"], "https://example.test/corrected-preview.jpg")
        self.assertEqual(pushed[1]["type"], "text")
        self.assertIn("暫時失敗", pushed[1]["text"])


if __name__ == "__main__":
    unittest.main()
