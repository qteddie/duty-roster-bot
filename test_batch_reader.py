import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from backend.api_usage import save_usage, usage_record
from backend.batch_reader import read_roster_batch, read_roster_batch_from_markdown
from schedule_reader import EMPLOYEES, ScheduleExtraction, result_dict


def reference_employees():
    reference = json.loads((Path(__file__).parent / "tests/fixtures/1952955_expected.json").read_text())
    return [ScheduleExtraction(
        document_date=reference["document_date"], employee_code=code, employee_name=EMPLOYEES[code],
        roster_profile="domestic_control" if code == "戊" else "international",
        assignments=[{"time": slot, "shift": shift, "cell_text": code,
                      "column_header_evidence": shift, "confidence": 1}
                     for slot, shift in slots.items()],
    ) for code, slots in reference["employees"].items()]


def response(employees, response_id="one"):
    raw = {"input_tokens": 10000, "input_tokens_details": {"cached_tokens": 2000},
           "output_tokens": 1000, "output_tokens_details": {"reasoning_tokens": 500}}
    return SimpleNamespace(id=response_id, model="gpt-5.6-luna", service_tier="default",
                           usage=SimpleNamespace(model_dump=lambda: raw),
                           output_text=json.dumps({"employees": [e.model_dump(mode="json") for e in employees]}))


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        env = patch.dict(os.environ, {"API_USAGE_DB": str(Path(self.directory.name) / "usage.db")})
        env.start()
        self.addCleanup(env.stop)

    def test_four_employees_cost_one_call_and_domestic_three_rest_hours_are_valid(self):
        client = Mock()
        client.responses.create.return_value = response(reference_employees())
        with patch("backend.batch_reader.image_data_url", return_value="data:image/png;base64,AA=="):
            result = read_roster_batch(Path("photo.png"), [], model="gpt-5.6-luna", codes=list(EMPLOYEES), client=client)
        self.assertEqual(client.responses.create.call_count, 1)
        domestic = result["employees"][1]
        self.assertEqual(domestic["working_ranges"], ["06:00–07:00", "09:00–12:00", "13:00–14:00"])
        self.assertEqual(domestic["resting_ranges"], ["07:00–09:00", "12:00–13:00"])
        self.assertTrue(domestic["audit"]["passed"])
        self.assertEqual(result["usage"]["api_calls"], 1)
        self.assertAlmostEqual(result["usage"]["estimated_usd"], 0.00284)

    def test_review_requests_only_incomplete_employee_and_counts_both_calls(self):
        employees = reference_employees()
        employees[3].assignments.pop(2)
        client = Mock()
        client.responses.create.side_effect = [response(employees), response([reference_employees()[3]], "two")]
        with patch("backend.batch_reader.image_data_url", return_value="data:image/png;base64,AA=="):
            result = read_roster_batch(Path("photo.png"), [], model="gpt-5.6-luna", codes=list(EMPLOYEES), client=client)
        prompt = client.responses.create.call_args.kwargs["input"][0]["content"][-1]["text"]
        self.assertIn("庚／莊雅婷", prompt)
        self.assertNotIn("丁／簡志明", prompt)
        self.assertIn("第一輪資料僅供找錯", prompt)
        self.assertIn('"employee_code": "庚"', prompt)
        self.assertEqual(result["usage"]["api_calls"], 2)
        self.assertAlmostEqual(result["usage"]["estimated_usd"], 0.00568)
        self.assertEqual(len(result["employees"][3]["assignments"]), 8)

    def test_each_uncertain_employee_is_reviewed_in_a_separate_request(self):
        employees = reference_employees()
        employees[0].assignments.pop()
        employees[3].assignments.pop()
        client = Mock()
        client.responses.create.side_effect = [
            response(employees),
            response([reference_employees()[0]], "two"),
            response([reference_employees()[3]], "three"),
        ]
        with patch("backend.batch_reader.image_data_url", return_value="data:image/png;base64,AA=="):
            result = read_roster_batch(
                Path("photo.png"), [], model="gpt-5.6-luna",
                codes=list(EMPLOYEES), client=client,
            )

        review_prompts = [
            call.kwargs["input"][0]["content"][-1]["text"]
            for call in client.responses.create.call_args_list[1:]
        ]
        self.assertEqual(client.responses.create.call_count, 3)
        self.assertIn("丁／簡志明", review_prompts[0])
        self.assertNotIn("庚／莊雅婷", review_prompts[0])
        self.assertIn("庚／莊雅婷", review_prompts[1])
        self.assertNotIn("丁／簡志明", review_prompts[1])
        self.assertTrue(all(employee["audit"]["passed"] for employee in result["employees"]))

    def test_focused_review_ignores_unrequested_employees(self):
        employees = reference_employees()
        employees[0].assignments.pop()
        client = Mock()
        client.responses.create.side_effect = [
            response(employees),
            response([reference_employees()[0], reference_employees()[1]], "two"),
        ]
        with patch("backend.batch_reader.image_data_url", return_value="data:image/png;base64,AA=="):
            result = read_roster_batch(
                Path("photo.png"), [], model="gpt-5.6-luna",
                codes=list(EMPLOYEES), client=client,
            )

        self.assertEqual(client.responses.create.call_count, 2)
        self.assertEqual(len(result["employees"][0]["assignments"]), 8)

    def test_usage_deduplicates_response_ids_and_does_not_price_unknown_models(self):
        r = response([])
        record = usage_record(r, "gpt-5.6-luna", 2)
        save_usage(record)
        self.assertEqual(save_usage(record)["api_calls"], 1)
        r.model = "unknown-model"
        self.assertIsNone(usage_record(r, r.model, 2)["estimated_usd"])

    def test_parse_failure_still_records_billed_usage(self):
        r = response([])
        r.output_text = "invalid JSON"
        client = Mock()
        client.responses.create.return_value = r
        with patch("backend.batch_reader.image_data_url", return_value="data:image/png;base64,AA=="):
            with self.assertRaises(ValueError):
                read_roster_batch(Path("photo.png"), [], model="gpt-5.6-luna", codes=list(EMPLOYEES), client=client)
        self.assertEqual(save_usage(usage_record(r, r.model, 2))["api_calls"], 1)

    def test_markdown_reader_sends_only_text_to_luna(self):
        client = Mock()
        client.responses.create.return_value = response(reference_employees())
        markdown = "| 時段 | 國際線 #1 |\n| --- | --- |\n| 06-07 | 丁+己 |"
        result = read_roster_batch_from_markdown(
            markdown, model="gpt-5.6-luna", codes=list(EMPLOYEES), client=client,
        )
        content = client.responses.create.call_args.kwargs["input"][0]["content"]
        self.assertEqual([item["type"] for item in content], ["input_text", "input_text"])
        self.assertIn(markdown, content[0]["text"])
        self.assertNotIn("input_image", {item["type"] for item in content})
        self.assertEqual(result["usage"]["api_calls"], 1)


if __name__ == "__main__":
    unittest.main()
