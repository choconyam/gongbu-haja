#!/usr/bin/env python3
"""validate_source_coverage.py 회귀 테스트."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts import validate_source_coverage as vsc


class ValidateSourceCoverageTests(unittest.TestCase):
    def write_json(self, root: Path, name: str, payload: object) -> Path:
        path = root / name
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def valid_payloads(self, mode: str = "faithful") -> tuple[dict, dict]:
        source_map = {
            "kind": "study_note_source_map",
            "schema_version": 1,
            "source_units": [
                {"source_unit_id": "slide-1"},
                {"source_unit_id": "slide-2"},
                {"source_unit_id": "slide-3"},
                {"source_unit_id": "slide-4"},
            ],
        }
        coverage = {
            "kind": "study_note_source_coverage",
            "schema_version": 1,
            "note_mode": mode,
            "reviewer_profile": "economy_max" if mode == "faithful" else "quality_xhigh",
            "items": [
                {"source_unit_id": "slide-1", "decision": "included", "note_refs": ["sec-1"]},
                {"source_unit_id": "slide-2", "decision": "merged", "note_refs": ["sec-1", "sec-2"]},
                {"source_unit_id": "slide-3", "decision": "excluded", "reason": "행정 슬라이드"},
                {"source_unit_id": "slide-4", "decision": "unresolved", "reason": "판독 필요", "note_refs": ["불확실성 목록"]},
            ],
        }
        return source_map, coverage

    def test_valid_faithful_and_deep_reports_pass_with_counts(self) -> None:
        for mode in ("faithful", "deep"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source, coverage = self.valid_payloads(mode)
                report = vsc.validate(self.write_json(root, "source.json", source), self.write_json(root, "coverage.json", coverage))
                self.assertFalse(report.errors)
                self.assertEqual(4, report.summary["source_unit_count"])
                self.assertEqual(4, report.summary["coverage_item_count"])
                self.assertEqual({"excluded": 1, "included": 1, "merged": 1, "unresolved": 1}, report.summary["decision_counts"])

    def test_public_validate_coverage_returns_ids_and_flat_integer_counts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, coverage = self.valid_payloads("deep")
            source_ids, counts = vsc.validate_coverage(
                self.write_json(root, "source.json", source),
                self.write_json(root, "coverage.json", coverage),
            )
            self.assertEqual(["slide-1", "slide-2", "slide-3", "slide-4"], source_ids)
            self.assertEqual(4, counts["source_unit_count"])
            self.assertEqual(4, counts["coverage_item_count"])
            self.assertEqual(1, counts["decision_included_count"])
            self.assertTrue(all(isinstance(value, int) for value in counts.values()))

    def test_public_validate_coverage_raises_with_detailed_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, coverage = self.valid_payloads()
            coverage["items"].pop()
            with self.assertRaises(vsc.CoverageValidationError) as caught:
                vsc.validate_coverage(
                    self.write_json(root, "source.json", source),
                    self.write_json(root, "coverage.json", coverage),
                )
            self.assertIn("missing-coverage", {issue.code for issue in caught.exception.report.errors})

    def test_empty_source_units_and_items_never_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, coverage = self.valid_payloads()
            source["source_units"] = []
            coverage["items"] = []
            report = vsc.validate(
                self.write_json(root, "source.json", source),
                self.write_json(root, "coverage.json", coverage),
            )
            codes = {issue.code for issue in report.errors}
            self.assertIn("empty-source-units", codes)
            self.assertIn("empty-coverage-items", codes)
            with self.assertRaises(vsc.CoverageValidationError):
                vsc.validate_coverage(root / "source.json", root / "coverage.json")

    def test_unhashable_mode_and_decision_are_reported_not_crashing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, coverage = self.valid_payloads()
            coverage["note_mode"] = []
            coverage["items"][0]["decision"] = {"decision": "included"}
            report = vsc.validate(
                self.write_json(root, "source.json", source),
                self.write_json(root, "coverage.json", coverage),
            )
            codes = {issue.code for issue in report.errors}
            self.assertIn("invalid-note-mode", codes)
            self.assertIn("invalid-decision", codes)

    def test_unresolved_requires_both_reason_and_note_refs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, coverage = self.valid_payloads()
            source["source_units"] = [{"source_unit_id": "slide-1"}]
            coverage["items"] = [
                {"source_unit_id": "slide-1", "decision": "unresolved", "note_refs": ["visible marker"]},
            ]
            report = vsc.validate(
                self.write_json(root, "source.json", source),
                self.write_json(root, "coverage.json", coverage),
            )
            self.assertIn("invalid-required-field", {issue.code for issue in report.errors})
            coverage["items"][0] = {
                "source_unit_id": "slide-1",
                "decision": "unresolved",
                "reason": "검토 필요",
            }
            report = vsc.validate(
                self.write_json(root, "source.json", source),
                self.write_json(root, "coverage.json", coverage),
            )
            self.assertIn("invalid-required-field", {issue.code for issue in report.errors})

    def test_id_contract_reports_duplicate_missing_and_additional(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, coverage = self.valid_payloads()
            source["source_units"].append({"source_unit_id": "slide-1"})
            coverage["items"] = [
                {"source_unit_id": "slide-1", "decision": "excluded", "reason": "x"},
                {"source_unit_id": "slide-1", "decision": "excluded", "reason": "y"},
                {"source_unit_id": "extra", "decision": "excluded", "reason": "z"},
            ]
            report = vsc.validate(self.write_json(root, "source.json", source), self.write_json(root, "coverage.json", coverage))
            codes = {issue.code for issue in report.errors}
            self.assertIn("duplicate-source-unit-id", codes)
            self.assertIn("duplicate-coverage-source-unit-id", codes)
            self.assertIn("missing-coverage", codes)
            self.assertIn("additional-coverage", codes)

    def test_decision_specific_required_fields_are_strict(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, coverage = self.valid_payloads()
            coverage["items"] = [
                {"source_unit_id": "slide-1", "decision": "included"},
                {"source_unit_id": "slide-2", "decision": "merged", "note_refs": []},
                {"source_unit_id": "slide-3", "decision": "excluded", "reason": ""},
                {"source_unit_id": "slide-4", "decision": "unresolved", "reason": "x", "note_refs": [""]},
            ]
            report = vsc.validate(self.write_json(root, "source.json", source), self.write_json(root, "coverage.json", coverage))
            self.assertGreaterEqual(sum(issue.code == "invalid-required-field" for issue in report.errors), 4)

    def test_header_mode_profile_and_decision_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, coverage = self.valid_payloads()
            source["schema_version"] = 2
            coverage["kind"] = "wrong"
            coverage["note_mode"] = "other"
            coverage["reviewer_profile"] = "quality_xhigh"
            coverage["items"][0]["decision"] = "maybe"
            report = vsc.validate(self.write_json(root, "source.json", source), self.write_json(root, "coverage.json", coverage))
            codes = {issue.code for issue in report.errors}
            self.assertIn("invalid-schema-version", codes)
            self.assertIn("invalid-kind", codes)
            self.assertIn("invalid-note-mode", codes)
            self.assertIn("invalid-decision", codes)

    def test_cli_json_and_nonzero_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, coverage = self.valid_payloads()
            source_path = self.write_json(root, "source.json", source)
            coverage_path = self.write_json(root, "coverage.json", coverage)
            result = subprocess.run(
                [sys.executable, str(Path(vsc.__file__)), str(source_path), str(coverage_path), "--json"],
                capture_output=True, text=True, encoding="utf-8", check=False,
            )
            self.assertEqual(0, result.returncode)
            self.assertEqual("pass", json.loads(result.stdout)["status"])
            coverage["items"].pop()
            coverage_path.write_text(json.dumps(coverage, ensure_ascii=False), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(Path(vsc.__file__)), str(source_path), str(coverage_path)],
                capture_output=True, text=True, encoding="utf-8", check=False,
            )
            self.assertNotEqual(0, result.returncode)
            self.assertIn("[ERROR] missing-coverage", result.stdout)

    def test_missing_and_invalid_json_are_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bad = self.write_json(root, "bad.json", [])
            report = vsc.validate(root / "missing.json", bad)
            self.assertIn("missing-file", {issue.code for issue in report.errors})
            invalid = root / "invalid.json"
            invalid.write_text("{", encoding="utf-8")
            report = vsc.validate(invalid, bad)
            self.assertIn("invalid-json", {issue.code for issue in report.errors})


if __name__ == "__main__":
    unittest.main()
