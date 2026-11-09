import csv
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from roster_resnet import (
    FIELDS, LABEL_FIELDS, LabelStore, _auto_label_group, _split_for, dataset_status,
    ingest_schedules,
)


class RosterResNetDatasetTests(unittest.TestCase):
    def test_group_split_is_deterministic(self):
        self.assertEqual(_split_for("same-photo"), _split_for("same-photo"))
        self.assertIn(_split_for("same-photo"), {"train", "val", "test"})

    def test_label_store_supports_multiple_labels_and_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (root / "labels.csv").open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=FIELDS); writer.writeheader()
                writer.writerow({"sample_id": "one", "image": "images/one.png",
                                 "source_group": "g", "row": 5, "column": 2, "split": "train",
                                 **{field: "" for field in LABEL_FIELDS.values()}})
            Image.new("RGB", (10, 10)).save(root / "one.png")
            store = LabelStore(root)
            store.update("one", {"丁": True, "庚": True})
            row = store.rows()[0]
            self.assertEqual([row[field] for field in LABEL_FIELDS.values()], ["1", "0", "0", "1"])
            status = dataset_status(root)
            self.assertEqual(status["labeled"], 1)
            self.assertEqual(status["positive_cells"]["丁"], 1)

    def test_label_store_updates_a_whole_sheet_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (root / "labels.csv").open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=FIELDS); writer.writeheader()
                for sample_id in ("one", "two"):
                    writer.writerow({"sample_id": sample_id, "image": f"images/{sample_id}.png",
                                     "source_group": "g", "row": 5, "column": 2,
                                     "split": "train",
                                     **{field: "" for field in LABEL_FIELDS.values()}})
            store = LabelStore(root)
            store.update_many({"one": {"丁": True}, "two": {"己": True, "庚": True}})
            rows = {row["sample_id"]: row for row in store.rows()}
            self.assertEqual(rows["one"]["ding"], "1")
            self.assertEqual(rows["two"]["ji"], "1")
            self.assertEqual(rows["two"]["geng"], "1")

    def test_auto_labels_are_suggestions_and_do_not_complete_human_labels(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "images").mkdir()
            checkpoint = root / "model.pt"
            checkpoint.touch()
            Image.new("RGB", (10, 10), "white").save(root / "images/one.png")
            with (root / "labels.csv").open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=FIELDS); writer.writeheader()
                writer.writerow({"sample_id": "one", "image": "images/one.png",
                                 "source_group": "group", "row": 5, "column": 2,
                                 "split": "train",
                                 **{field: "" for field in LABEL_FIELDS.values()}})
            prediction = SimpleNamespace(
                labels=("丁",), probabilities={"丁": .9, "戊": .1, "己": .2, "庚": .3},
            )
            with patch("roster_resnet.RosterResNetClassifier") as classifier:
                classifier.return_value.predict.return_value = [prediction]
                result = _auto_label_group(root, "group", checkpoint)

            self.assertEqual(result["samples"], 1)
            suggestions = __import__("json").loads((root / "auto-labels.json").read_text())
            self.assertTrue(suggestions["one"]["labels"]["丁"])
            self.assertEqual(dataset_status(root)["labeled"], 0)

    def test_single_photo_ingest_mode_preserves_other_manifest_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            schedules = root / "schedules"
            schedules.mkdir()
            with (root / "labels.csv").open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=FIELDS); writer.writeheader()
                writer.writerow({"sample_id": "existing", "image": "images/existing.png",
                                 "source_group": "existing-group", "row": 5, "column": 2,
                                 "split": "train",
                                 **{field: "0" for field in LABEL_FIELDS.values()}})

            ingest_schedules(
                schedules, root,
                Path(__file__).parent / "configs/phase1_roster_template.local.json",
                only_sources=[],
            )

            self.assertEqual(LabelStore(root).rows()[0]["sample_id"], "existing")


if __name__ == "__main__":
    unittest.main()
