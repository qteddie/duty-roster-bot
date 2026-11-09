"""Saved rosters made by an older grid are read again from their saved photo (``DemoApp.ensure_grid``)."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from all_staff.dataset import GRID_VERSION
from all_staff.demo import DemoApp, DemoError
from all_staff.lines import DUTY_VERSION
from all_staff.schema import CellAnnotation, save_annotations

OLD, MIDDLE, NEWEST = "20261001-090000-aaaaaa", "20261003-090000-bbbbbb", "20261004-090000-cccccc"
COMMON = {"generated_at": "now", "model": {}, "time_rows": {"5": "06-07"}, "roster": {}, "allowed": ["B", "80"],
          "column_names": {"35": "內部管理", "34": "內機"}}


def fake_prepare(upload, cells, template, *, fx, fy, only_sources):
    """What prepare_context leaves behind: crops, a debug picture, annotations."""
    (cells / "images").mkdir(parents=True); (cells / "debug").mkdir()
    (cells / "images" / "new.png").write_bytes(b"new"); (cells / "debug" / "g.jpg").write_bytes(b"debug")
    save_annotations(cells / "annotations.jsonl", [CellAnnotation(
        sample_id="new", image="images/new.png", source_group="g" * 64, row=5, column=35, width=10, height=10,
        reviewed=False)])
    return {}


def fake_sheet(dataset, items, engine, context, url, *, name, overview):
    cell = {"id": "new", "r": 5, "c": 35, "img": url(dataset / "images" / "new.png"), "w": 10, "h": 10,
            "core": [0, 0, 10, 10], "lines": [], "pred": [{"code": "B", "conf": .99, "box": [0, 0, 1, 1], "line": 0}]}
    return {"cells": [cell], "overview": overview, "name": name}


class RegridTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        root = Path(self.folder.name)
        self.app = DemoApp.__new__(DemoApp)
        self.app.runs, self.app.lock = root / "runs", threading.Lock()
        self.app.engine, self.app.context = object(), {"fx": .22, "fy": .12}
        self.app.template, self.app.recognizer, self.app.roster, self.app.root = root / "t.json", root / "r.pt", None, root
        self.app.runs.mkdir()
        self.patches = [mock.patch("all_staff.context.prepare_context", side_effect=fake_prepare),
                        mock.patch("all_staff.demo.sheet_payload", side_effect=fake_sheet),
                        mock.patch("all_staff.demo.common_payload", return_value=dict(COMMON))]
        self.prepare = self.patches[0].start(); self.sheet = self.patches[1].start(); self.patches[2].start()

    def tearDown(self):
        for patch in self.patches:
            patch.stop()
        self.folder.cleanup()

    def make(self, run_id, grid=None, photo=True):
        run = self.app.runs / run_id
        (run / "cells/images").mkdir(parents=True)
        (run / "cells/images/old.png").write_bytes(b"old")
        for derived in ("people", "slots"):
            (run / derived).mkdir(); (run / derived / "x.jpg").write_bytes(b"x")
        if photo:
            (run / "upload").mkdir(); (run / "upload" / "photo.jpg").write_bytes(b"photo")
        data = {"run": {"id": run_id, "name": "10_5.jpg", "codes": 3, "roster_date": "2026-10-05", "seconds": 20},
                "names": {"35": ["沈姻姻"]}, "names_version": 5, "leave_table": {"available": True, "rest": ["35"]},
                "leave_table_version": 1, "allowed": ["B"], "roster": {}, "time_rows": {"5": "06-07"},
                "column_names": {}, "people": [{"code": "A", "slots": [{"row": 19}]}],
                "sheets": [{"cells": [{"id": "old", "r": 19, "c": 7, "pred": [{"code": "A", "conf": .89}]}]}],
                "duty_version": DUTY_VERSION}
        if grid:
            data["grid_version"] = grid
        (run / "result.json").write_text(json.dumps(data), encoding="utf-8")
        return run

    def result(self, run_id):
        return json.loads((self.app.runs / run_id / "result.json").read_text(encoding="utf-8"))

    def test_old_run_is_read_again_and_keeps_names_leave_table_and_date(self):
        run = self.make(OLD)
        after = self.app.ensure_grid(OLD)
        self.assertEqual(after, self.result(OLD))
        self.assertEqual(after["grid_version"], GRID_VERSION)
        self.assertEqual([p["code"] for p in after["people"]], ["B"])   # the call-out's "A" is gone
        self.assertEqual(after["people"][0]["slots"][0]["duty"], "內部管理")   # the last four columns have names
        self.assertEqual(after["run"]["codes"], 1)
        self.assertEqual(after["run"]["roster_date"], "2026-10-05")
        self.assertEqual((after["names"], after["leave_table"]), ({"35": ["沈姻姻"]}, {"available": True, "rest": ["35"]}))
        self.assertEqual(after["sheets"][0]["cells"][0]["img"], f"/runs/{OLD}/cells/images/new.png")
        self.assertEqual(after["sheets"][0]["overview"], f"/runs/{OLD}/cells/debug/g.jpg")
        self.assertTrue((run / "cells/images/new.png").is_file())
        self.assertFalse((run / "cells/images/old.png").exists())
        for leftover in ("cells.next", "cells.old", "people", "slots"):      # old pictures are drawn again on demand
            self.assertFalse((run / leftover).exists(), leftover)
        self.assertEqual(list(run.glob("*.tmp")), [])

    def test_a_run_that_is_already_current_is_left_alone_unless_forced(self):
        self.make(MIDDLE, grid=GRID_VERSION)
        self.app.ensure_grid(MIDDLE)
        self.prepare.assert_not_called()
        self.app.ensure_grid(MIDDLE, force=True)
        self.prepare.assert_called_once()

    def test_failure_changes_nothing(self):
        run = self.make(OLD)
        before = (run / "result.json").read_text(encoding="utf-8")
        with mock.patch("all_staff.demo.sheet_payload", side_effect=RuntimeError("模型壞了")):
            with self.assertRaises(RuntimeError):
                self.app.ensure_grid(OLD)
        self.assertEqual((run / "result.json").read_text(encoding="utf-8"), before)
        self.assertTrue((run / "cells/images/old.png").is_file())
        self.assertTrue((run / "people/x.jpg").is_file())
        for leftover in ("cells.next", "cells.old"):
            self.assertFalse((run / leftover).exists(), leftover)
        self.assertEqual(list(run.glob("*.tmp")), [])

    def test_a_photo_that_no_longer_shows_a_table_changes_nothing(self):
        run = self.make(OLD)
        with mock.patch("all_staff.schema.load_annotations", return_value=[]):
            with self.assertRaises(DemoError):
                self.app.ensure_grid(OLD)
        self.assertTrue((run / "cells/images/old.png").is_file())
        self.assertNotIn("grid_version", self.result(OLD))

    def test_missing_photo_is_reported(self):
        self.make(OLD, photo=False)
        with self.assertRaises(DemoError):
            self.app.ensure_grid(OLD)
        self.assertNotIn("grid_version", self.result(OLD))

    def test_upgrade_runs_goes_newest_first_skips_current_and_remembers_failures(self):
        for run_id, grid in ((OLD, None), (MIDDLE, GRID_VERSION), (NEWEST, None)):
            self.make(run_id, grid=grid)
        order = []

        def prepare(upload, cells, template, **kwargs):
            order.append(upload.parent.name)
            if upload.parent.name == NEWEST:
                raise RuntimeError("格線找不到")
            return fake_prepare(upload, cells, template, **kwargs)
        self.prepare.side_effect = prepare
        report = self.app.upgrade_runs(progress=lambda *_: None)
        self.assertEqual(order, [NEWEST, OLD])
        self.assertEqual(report["upgraded"], [OLD])
        self.assertEqual(report["current"], [MIDDLE])
        self.assertIn("格線找不到", report["failed"][NEWEST])
        self.assertEqual(self.result(OLD)["grid_version"], GRID_VERSION)
        self.assertNotIn("grid_version", self.result(NEWEST))
        # a failure is not tried again on every start-up ...
        order.clear()
        again = self.app.upgrade_runs(progress=lambda *_: None)
        self.assertEqual(order, [])
        self.assertIn(NEWEST, again["failed"])
        # ... until it is asked for
        self.prepare.side_effect = fake_prepare
        forced = self.app.upgrade_runs(force=True, only=[NEWEST], progress=lambda *_: None)
        self.assertEqual(forced["upgraded"], [NEWEST])
        self.assertEqual(self.result(NEWEST)["grid_version"], GRID_VERSION)
        self.assertFalse(list((self.app.runs / NEWEST).glob("regrid-*.failed")))

    def test_limit_counts_runs_read_again(self):
        for run_id in (OLD, MIDDLE, NEWEST):
            self.make(run_id)
        report = self.app.upgrade_runs(limit=2, progress=lambda *_: None)
        self.assertEqual(report["upgraded"], [NEWEST, MIDDLE])
        self.assertNotIn("grid_version", self.result(OLD))


class PictureNameTests(unittest.TestCase):
    """LINE caches pictures by address: runs read again must not reuse the names of the pictures drawn before."""

    def app(self, folder, grid):
        app = DemoApp.__new__(DemoApp)
        app.runs, app.lock = Path(folder) / "runs", threading.Lock()
        run = app.runs / "run-1"
        run.mkdir(parents=True)
        data = {"run": {"id": "run-1"}, "duty_version": DUTY_VERSION,
                "people": [{"code": "B", "slots": [{"row": 5, "column": 35}]}]}
        if grid:
            data["grid_version"] = grid
        (run / "result.json").write_text(json.dumps(data), encoding="utf-8")
        return app, run

    def test_person_pictures_get_a_grid_suffix_only_when_read_with_the_current_grid(self):
        with tempfile.TemporaryDirectory() as folder:
            app, run = self.app(folder, GRID_VERSION)
            (run / "people").mkdir()
            current = f"B-digits-v{DUTY_VERSION}-g{GRID_VERSION}"
            for name in (current, current + "-preview"):
                (run / "people" / f"{name}.jpg").write_bytes(b"x")
            self.assertEqual(app.person_image("run-1", "B"),
                             (f"run-1/people/{current}.jpg", f"run-1/people/{current}-preview.jpg"))
        with tempfile.TemporaryDirectory() as folder:
            app, run = self.app(folder, None)
            (run / "people").mkdir()
            old = f"B-digits-v{DUTY_VERSION}"
            for name in (old, old + "-preview"):
                (run / "people" / f"{name}.jpg").write_bytes(b"x")
            self.assertEqual(app.person_image("run-1", "B"),
                             (f"run-1/people/{old}.jpg", f"run-1/people/{old}-preview.jpg"))


if __name__ == "__main__":
    unittest.main()
