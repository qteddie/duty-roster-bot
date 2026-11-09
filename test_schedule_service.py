import os
import tempfile
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from backend.preprocessing import PreprocessResult
from backend.phase1_cell_pipeline import GridTemplate
from backend.schedule_service import _parse_roster_header_date, analyze_schedule_image


class ScheduleServiceTests(unittest.IsolatedAsyncioTestCase):
    def test_noisy_printed_header_date_is_parsed_without_using_filename(self):
        self.assertEqual(_parse_roster_header_date("202649 A258 MB"), "2026-09-25")
        self.assertEqual(_parse_roster_header_date("202649 A238 FMS"), "2026-09-23")
        self.assertIsNone(_parse_roster_header_date("沒有可確認日期"))

    async def test_diagnostics_survive_a_failure_after_a_billed_response(self):
        response = SimpleNamespace(
            id="diagnostic-test", model="gpt-5.6-luna", service_tier="default",
            usage=SimpleNamespace(model_dump=lambda: {"input_tokens": 1, "output_tokens": 1}),
            model_dump=lambda **kwargs: {"id": "diagnostic-test", "output": []},
        )
        def recognize(source, template, **kwargs):
            self.assertEqual(kwargs["artifacts_dir"].name, "grid")
            kwargs["usage_observer"](response, .1)
            raise RuntimeError("later batch failed")
        with tempfile.TemporaryDirectory() as tmp:
            artifacts = Path(tmp) / "record"
            with patch.dict(os.environ, {"OPENAI_API_KEY": "test"}), \
                 patch("backend.schedule_service.preprocess_roster", side_effect=lambda source,*args: PreprocessResult(source,"test",[])), \
                 patch("backend.schedule_service.recognize_source_multilabel", side_effect=recognize), \
                 patch("backend.schedule_service.save_usage") as saved:
                with self.assertRaisesRegex(RuntimeError, "later batch failed"):
                    await analyze_schedule_image(b"source-photo", filename="photo.jpg", content_type="image/jpeg",
                                                 use_final_markdown=True, artifacts_dir=artifacts)
            self.assertEqual((artifacts / "source.jpg").read_bytes(), b"source-photo")
            self.assertTrue((artifacts / "phase-usage.json").exists())
            self.assertTrue((artifacts / "api/cells/01.json").exists())
            saved.assert_called_once()

    async def test_image_preparation_flow_returns_lifetime_usage(self):
        def fake_preprocess(source, *_args):
            return PreprocessResult(
                image_path=source,
                method="full_photo_strips",
                zoom_image_paths=[],
            )

        def fake_read(_image, _zooms, **kwargs):
            return {
                "employees": [{"employee_code": "己"}],
                "usage": {"api_calls": 1},
                "usage_lifetime": {"api_calls": 4},
            }

        with patch.dict(os.environ, {"OPENAI_API_KEY": "test"}, clear=False), \
             patch("backend.schedule_service.preprocess_roster", side_effect=fake_preprocess), \
             patch("backend.schedule_service.read_roster_batch", side_effect=fake_read):
            result = await analyze_schedule_image(
                b"image", filename="roster.png", content_type="image/png"
            )

        self.assertEqual(result["usage_lifetime"], {"api_calls": 4})
        self.assertEqual(result["preprocessing"]["method"], "full_photo_strips")

    async def test_line_mode_uses_local_resnet_without_luna(self):
        def fake_preprocess(source, *_args):
            return PreprocessResult(image_path=source, method="full_photo_strips", zoom_image_paths=[])

        template = GridTemplate(
            output_width=100, output_height=100,
            grid_x=(0.0, 0.5), grid_y=(0.0, 0.5, 1.0),
            row_map={0: "06-07", 1: "07-08"}, column_map={0: "國際線"},
        )

        def fake_recognize(_source, _template, **kwargs):
            return {"markdown_grid": "| 時段 | 國際線 #1 |\n| --- | --- |\n| 06-07 | 己 |",
                    "cell_count": 1, "wu_geng_verifier_count": 0,
                    "correction_method": "source_original", "final": [
                        {"row": 0, "column": 0, "ding": False, "wu": False,
                         "ji": True, "geng": False, "confidence": 0.99},
                        {"row": 1, "column": 0, "ding": False, "wu": False,
                         "ji": True, "geng": False, "confidence": 0.99},
                    ]}

        def fake_evidence_card(_source, _template, **kwargs):
            destination = kwargs["destination"]
            destination.write_bytes(b"evidence")
            return destination

        with patch.dict(os.environ, {"OPENAI_API_KEY": "test"}, clear=False), \
             patch("backend.schedule_service.preprocess_roster", side_effect=fake_preprocess) as preprocess, \
             patch("backend.schedule_service.GridTemplate.from_json", return_value=template), \
             patch("backend.schedule_service.recognize_source_multilabel", side_effect=fake_recognize), \
             patch("backend.schedule_service.read_roster_batch_from_markdown") as markdown_reader, \
             patch("backend.schedule_service.create_employee_evidence_image", side_effect=fake_evidence_card), \
             patch("backend.schedule_service.read_roster_batch") as image_reader, \
             patch("backend.schedule_service.save_usage", return_value={"api_calls": 1}):
            result = await analyze_schedule_image(
                b"image", filename="roster.png", content_type="image/png", use_final_markdown=True,
                include_crop_images=True,
            )

        image_reader.assert_not_called()
        preprocess.assert_not_called()
        markdown_reader.assert_not_called()
        self.assertEqual(result["recognition_source"], "local_resnet18_epoch23")
        self.assertEqual(result["usage"]["api_calls"], 0)
        self.assertEqual(result["crop_image"]["filename"], "己-marked-roster.jpg")
        self.assertEqual(result["crop_image"]["content"], b"evidence")

    async def test_line_text_reconciles_every_cell_outlined_by_phase1(self):
        def fake_preprocess(source, *_args):
            return PreprocessResult(image_path=source, method="full_photo_strips", zoom_image_paths=[])

        template = GridTemplate(
            output_width=100, output_height=100,
            grid_x=(0.0, 0.5, 1.0), grid_y=(0.0, 0.5, 1.0),
            row_map={0: "12-13", 1: "13-14"},
            column_map={0: "國際線", 1: "國際線（二）"},
        )
        phase_response = SimpleNamespace(
            id="phase", model="gpt-5.6-luna", service_tier="default",
            usage=SimpleNamespace(model_dump=lambda: {
                "input_tokens": 100, "input_tokens_details": {}, "output_tokens": 10,
            }),
        )

        def fake_recognize(_source, _template, **kwargs):
            kwargs["usage_observer"](phase_response, 0.1)
            return {
                "markdown_grid": "| 時段 | 國際線 #1 |\n| --- | --- |\n| 12-13 | 己 |",
                "cell_count": 2, "wu_geng_verifier_count": 0,
                "correction_method": "perspective_grid_registration",
                "final": [
                    {"row": 0, "column": 0, "ding": False, "wu": False,
                     "ji": True, "geng": False, "confidence": 0.99},
                    {"row": 0, "column": 1, "ding": False, "wu": False,
                     "ji": False, "geng": False, "confidence": 0.99},
                    {"row": 1, "column": 0, "ding": False, "wu": False,
                     "ji": True, "geng": False, "confidence": 0.99},
                    {"row": 1, "column": 1, "ding": False, "wu": False,
                     "ji": False, "geng": False, "confidence": 0.99},
                ],
            }

        def fake_markdown_reader(_markdown, **_kwargs):
            # Simulate the second text model dropping the 12-13 recognition.
            return {"employees": [{"employee_code": "己", "employee_name": "蕭志明",
                                    "roster_profile": "international", "assignments": [
                                        {"time": "13-14", "shift": "國際線"},
                                    ]}],
                    "usage": {"api_calls": 1}, "usage_lifetime": {"api_calls": 2}, "calls": []}

        with patch.dict(os.environ, {"OPENAI_API_KEY": "test"}, clear=False), \
             patch("backend.schedule_service.preprocess_roster", side_effect=fake_preprocess), \
             patch("backend.schedule_service.GridTemplate.from_json", return_value=template), \
             patch("backend.schedule_service.recognize_source_multilabel", side_effect=fake_recognize), \
             patch("backend.schedule_service.read_roster_batch_from_markdown", side_effect=fake_markdown_reader), \
             patch("backend.schedule_service.save_usage", return_value={"api_calls": 1}):
            result = await analyze_schedule_image(
                b"image", filename="roster.png", content_type="image/png", use_final_markdown=True,
            )

        self.assertEqual(
            [(item["time"], item["shift"]) for item in result["assignments"]],
            [("12-13", "國際線"), ("13-14", "國際線")],
        )


if __name__ == "__main__":
    unittest.main()
