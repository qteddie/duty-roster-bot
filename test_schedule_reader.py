import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace
from pathlib import Path

from schedule_reader import Assignment, RosterProfile, ScheduleExtraction, Shift, format_report, result_dict, shift_needs_work, working_ranges
from schedule_reader import EMPLOYEES, read_schedule, resting_ranges, shift_slots


class ScheduleReaderTests(unittest.TestCase):
    def test_international_two_is_explicitly_a_rest_shift(self):
        self.assertTrue(shift_needs_work(Shift.INTERNATIONAL))
        self.assertFalse(shift_needs_work(Shift.INTERNATIONAL_2))

    def test_rest_ranges_merge_only_observed_rest_slots(self):
        items = [
            Assignment(time="09-10", shift=Shift.INTERNATIONAL_2, cell_text="丁", confidence=1),
            Assignment(time="10-11", shift=Shift.INTERNATIONAL_STANDBY, cell_text="丁", confidence=1),
            Assignment(time="12-13", shift=Shift.INTERNATIONAL, cell_text="丁", confidence=1),
        ]
        self.assertEqual(resting_ranges(items), ["09:00–11:00"])
        self.assertEqual(resting_ranges([]), [])

    def test_work_rest_ratio_does_not_override_complete_observed_schedule(self):
        for code, work_hours in [("庚", 8), ("丁", 5)]:
            with self.subTest(code=code):
                parsed = ScheduleExtraction(
                    employee_name=EMPLOYEES[code], employee_code=code,
                    roster_profile=RosterProfile.INTERNATIONAL,
                    assignments=[Assignment(
                        time=f"{hour:02d}-{hour + 1:02d}",
                        shift=Shift.INTERNATIONAL if hour < 6 + work_hours else Shift.INTERNATIONAL_2,
                        column_header_evidence="國際線" if hour < 6 + work_hours else "國際線（二）",
                        cell_text=code, confidence=1,
                    ) for hour in range(6, 14)],
                )
                client = Mock()
                client.responses.parse.return_value = SimpleNamespace(output_parsed=parsed)
                with patch("schedule_reader.image_data_url", return_value="data:image/png;base64,AA=="):
                    result = read_schedule(Path("example.png"), client=client, employee_code=code)
                self.assertEqual(client.responses.parse.call_count, 1)
                self.assertTrue(result_dict(result)["audit"]["passed"])
                self.assertEqual(result_dict(result)["audit"]["actual_working"], work_hours)

    def test_each_employee_is_used_in_first_pass_and_review(self):
        for code, name in EMPLOYEES.items():
            with self.subTest(code=code):
                parsed = ScheduleExtraction(
                    employee_name=name, employee_code=code,
                    roster_profile=RosterProfile.INTERNATIONAL, assignments=[],
                )
                client = Mock()
                client.responses.parse.return_value = SimpleNamespace(output_parsed=parsed)
                with patch("schedule_reader.image_data_url", return_value="data:image/png;base64,AA=="):
                    result = read_schedule(Path("example.png"), client=client, employee_code=code)
                self.assertEqual((result.employee_code, result.employee_name), (code, name))
                self.assertEqual(client.responses.parse.call_count, 2)
                for call in client.responses.parse.call_args_list:
                    self.assertIn(f"目標員工是「{name}」，代號是單一中文字「{code}」", call.kwargs["instructions"])

    def test_only_two_shift_types_count_as_work(self):
        assignments = [
            Assignment(time="05-06", shift=Shift.INTERNATIONAL_2, cell_text="己", confidence=0.95),
            Assignment(time="06-07", shift=Shift.INTERNATIONAL, cell_text="己", confidence=0.95),
            Assignment(time="07-08", shift=Shift.INTERNATIONAL, cell_text="己", confidence=0.95),
            Assignment(time="08-09", shift=Shift.INTERNATIONAL_STANDBY, cell_text="己", confidence=0.95),
            Assignment(time="09-10", shift=Shift.DOMESTIC_1, cell_text="己", confidence=0.95),
            Assignment(time="10-11", shift=Shift.DOMESTIC_2, cell_text="己", confidence=0.95),
        ]
        self.assertEqual(working_ranges(assignments), ["06:00–08:00", "09:00–10:00"])

    def test_shift_slots_show_an_eight_hour_window_without_filling_missing_hours(self):
        assignments = [
            Assignment(time="06-07", shift=Shift.INTERNATIONAL, cell_text="己", confidence=1),
            Assignment(time="08-09", shift=Shift.INTERNATIONAL_2, cell_text="己", confidence=1),
        ]

        self.assertEqual(
            shift_slots(assignments),
            ["06-07", "07-08", "08-09", "09-10", "10-11", "11-12", "12-13", "13-14"],
        )
        result = ScheduleExtraction(
            employee_name="蕭志明", employee_code="己",
            roster_profile=RosterProfile.INTERNATIONAL, assignments=assignments,
        )
        self.assertEqual(result_dict(result)["shift_slots"], shift_slots(assignments))

    def test_report_preserves_rest_assignments_and_flags_uncertainty(self):
        result = ScheduleExtraction(
            document_date="2026-09-07",
            employee_name="蕭志明",
            employee_code="己",
            roster_profile=RosterProfile.INTERNATIONAL,
            assignments=[
                Assignment(
                    time="12-13",
                    shift=Shift.DOMESTIC_2,
                    cell_text="己",
                    confidence=0.6,
                    note="字跡模糊",
                )
            ],
            warnings=[],
        )
        report = format_report(result)
        self.assertIn("國內線二線｜休息", report)
        self.assertIn("⚠ 需確認", report)
        self.assertIn("尚未辨識到工作時段", report)
        self.assertFalse(result_dict(result)["audit"]["passed"])

    def test_domestic_control_counts_work_and_rest(self):
        assignments = [
            Assignment(time=f"{hour:02d}-{hour + 1:02d}", shift=Shift.DOMESTIC_1, cell_text="己", confidence=1)
            for hour in range(6, 12)
        ] + [
            Assignment(time="12-13", shift=Shift.DOMESTIC_2, cell_text="己", confidence=1),
            Assignment(time="13-14", shift=Shift.DOMESTIC_STANDBY, cell_text="己", confidence=1),
        ]
        result = ScheduleExtraction(
            document_date="2026-09-09",
            employee_name="蕭志明",
            employee_code="己",
            roster_profile=RosterProfile.DOMESTIC_CONTROL,
            assignments=assignments,
        )
        audit = result_dict(result)["audit"]
        self.assertTrue(audit["passed"])
        self.assertEqual((audit["actual_total"], audit["actual_working"], audit["actual_resting"]), (8, 6, 2))

    def test_empty_assignments_are_reported_as_day_off(self):
        result = ScheduleExtraction(
            document_date="2026-09-10",
            employee_name="蕭志明",
            employee_code="己",
            roster_profile=RosterProfile.INTERNATIONAL,
            assignments=[],
        )

        payload = result_dict(result)

        self.assertTrue(payload["is_day_off"])
        self.assertIn("當日休假", format_report(result))

    def test_one_isolated_assignment_is_reported_as_day_off(self):
        result = ScheduleExtraction(
            document_date="2026-09-10",
            employee_name="蕭志明",
            employee_code="己",
            roster_profile=RosterProfile.INTERNATIONAL,
            assignments=[
                Assignment(time="15-16", shift=Shift.INTERNATIONAL_STANDBY,
                           cell_text="己", confidence=1),
            ],
        )

        self.assertTrue(result_dict(result)["is_day_off"])


if __name__ == "__main__":
    unittest.main()
