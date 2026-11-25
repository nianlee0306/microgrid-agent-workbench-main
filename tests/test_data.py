import json
import tempfile
import unittest
from pathlib import Path

from app.cli import main as import_cli
from app.data import DataError, RunStore, import_recordings, load_csv_run, write_json
from tests.replay_fixtures import ReplayFixtureStore


class DataTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.source = Path(directory.name)

    def csv(self, name, text, run_id=None, **overrides):
        path = self.source / name
        path.write_text(text, encoding="utf-8")
        entry = {
            "run_id": run_id or path.stem,
            "file": name,
            "source": "Controlled synthetic test source",
            "category": "test",
            **overrides,
        }
        return path, entry

    def test_mechanical_blank_and_unit_rows_without_clock(self):
        path, entry = self.csv("mechanical.csv", "\nSpeed,Torque,Power,Energy,E\n,,,,\n[r/min],(N•m),W,W•h,\n\n1400,1.5,220,0,\n1410,,222,.02,\n")
        record, report = load_csv_run(path, entry, synthetic=True)
        metadata = record["metadata"]
        self.assertEqual(metadata["axis"], "sample_index")
        self.assertEqual(metadata["timezone"], "unknown")
        self.assertEqual(record["axis_values"], [0, 1])
        self.assertEqual(record["values"]["torque_nm"], [1.5, None])
        self.assertEqual(metadata["signals"]["torque_nm"]["unit"], "N·m")
        self.assertEqual(report["valid_rows"], 2)
        self.assertEqual(report["blank_rows"], 2)
        self.assertEqual(report["unit_rows"], 1)
        self.assertNotIn("frequency_actual_hz", record["values"])

    def test_frequency_fields_have_distinct_sources_and_meanings(self):
        path, entry = self.csv("labs.csv", "ElapsedTime_s,SampleIndex,Vpositive_RMS,FrequencyActual_Hz,FrequencyFeedback_Hz,SpeedActual_rpm,E1_RMS,I1_RMS\n.1,7,230,49.8,50.2,1494,231,1.4\n.2,9,233,49.9,50.1,1497,232,1.6\n")
        record, _ = load_csv_run(path, entry)
        self.assertEqual(record["sample_indices"], [7, 9])
        self.assertEqual(record["values"]["frequency_actual_hz"], [49.8, 49.9])
        self.assertEqual(record["values"]["frequency_feedback_hz"], [50.2, 50.1])
        self.assertEqual(record["metadata"]["signals"]["speed_estimated_rpm"]["provenance"], "electrical_estimate")
        self.assertEqual(record["values"]["va_rms"], [231.0, 232.0])
        self.assertEqual(record["values"]["ia_rms"], [1.4, 1.6])
        self.assertNotIn("speed_mechanical_rpm", record["values"])
        with self.assertRaisesRegex(DataError, "conflicts with the known telemetry column"):
            load_csv_run(path, {**entry, "fields": {"frequency_actual_hz": "FrequencyFeedback_Hz"}})

    def test_voltage_allows_missing_columns_and_preserves_invalid_fields(self):
        path, entry = self.csv("v.csv", "time_s,Va_RMS,Vb_RMS,Ia_RMS,controllerEnabled\n.1,231,,1.5,true\n.2,232,234,broken,false\nbad,232,234,1.5,false\n.4,233,234,1.1,true\n")
        record, report = load_csv_run(path, entry)
        self.assertNotIn("frequency_actual_hz", record["values"])
        self.assertNotIn("v_positive_rms", record["values"])
        self.assertEqual(record["values"]["controller_state"], ["Enabled", "Disabled", "Enabled"])
        self.assertEqual(record["values"]["vb_rms"], [None, 234.0, 234.0])
        self.assertEqual(record["sample_indices"], [0, 1, 3])
        self.assertEqual(report["missing_counts"]["ia_rms"], 1)
        self.assertIn("vc_rms", report["missing_key_fields"])
        self.assertEqual(report["invalid_rows"], 1)
        self.assertEqual(report["input_error_count"], 2)

    def test_manifest_exposes_units_without_conversion(self):
        path, entry = self.csv("v.csv", "time_s,Va_RMS,Vb_RMS\n.1,231,232\n", units={"va_rms": "mV"})
        record, report = load_csv_run(path, entry)
        self.assertEqual(record["metadata"]["signals"]["va_rms"]["unit"], "mV")
        self.assertEqual(report["data_quality"]["unit_mismatches"], ["va_rms"])
        self.assertEqual(record["values"]["va_rms"], [231.0])

    def test_validated_matlab_export_preserves_source_clock_metadata(self):
        path, entry = self.csv("voltage.csv", "SchemaVersion,SampleIndex,time_s,Va_RMS,Timestamp,TimestampTimeZone\ncontroller-log-v1,3,.1,231,2026-08-03T16:38:38.408+10:00,Australia/Sydney\n")
        record, _ = load_csv_run(path, entry)
        self.assertEqual(record["metadata"]["source_schema_version"], "controller-log-v1")
        self.assertEqual(record["metadata"]["timezone"], "Australia/Sydney")
        self.assertEqual(record["axis_values"], [0.1])
        self.assertEqual(record["sample_indices"], [3])
        self.assertEqual(record["timestamps"], ["2026-08-03T16:38:38.408+10:00"])
        with self.assertRaisesRegex(DataError, "timezone disagrees"):
            load_csv_run(path, {**entry, "timezone": "UTC"})

    def test_clock_metadata_and_alignment_are_explicit(self):
        _, va = self.csv("va.csv", "time_s,Va_RMS\n0,231\n2,232\n", source="DAQ A")
        _, labvolt = self.csv("labvolt.csv", "Speed,Torque,Time\n1400,1.1,0\n1402,1.2,2\n", source="DAQ B")
        manifest = {"schema_version": 1, "runs": [va, labvolt], "verified_alignments": [{
            "run_ids": ["va", "labvolt"], "verified": True, "sync_method": "Test trigger",
            "evidence_reference": "Synthetic timing reference",
            "acquisition_sources": {"va": "DAQ A", "labvolt": "DAQ B"},
            "offsets_s": {"va": 0, "labvolt": 0.1},
        }]}
        catalog, report = import_recordings(self.source, manifest)
        self.assertEqual(catalog["runs"][1]["metadata"]["axis"], "sample_index")
        self.assertTrue(report["failed_inputs"])
        labvolt.update(time_column="Time", time_basis="elapsed_seconds", time_unit="s", time_reference="Test acquisition clock")
        catalog, report = import_recordings(self.source, manifest)
        self.assertFalse(report["failed_inputs"])
        self.assertEqual(catalog["runs"][1]["metadata"]["verified_alignments"][0]["common_time_range_s"], [0.1, 2])

    def test_cli_report_includes_partial_success(self):
        _, first = self.csv("voltage.csv", "time_s,Va_RMS\n.1,231\n")
        manifest = self.source / "manifest.json"
        manifest.write_text(json.dumps({"schema_version": 1, "runs": [first, {**first, "run_id": "outside", "file": "../other.csv"}]}), encoding="utf-8")
        output = self.source / "normalized"
        status = import_cli(["import", "--source-dir", str(self.source), "--manifest", str(manifest), "--output-dir", str(output)])
        coverage = json.loads((output / "coverage-report.json").read_text(encoding="utf-8"))
        self.assertEqual(status, 1)
        self.assertEqual(coverage["requested_runs"], 2)
        self.assertEqual(coverage["imported_runs"], 1)
        self.assertEqual(coverage["failed_inputs"][0]["run_id"], "outside")
        self.assertTrue((output / "catalog.json").exists())

    def test_windows_share_evidence_id_and_validate_coordinate(self):
        store = ReplayFixtureStore()
        clean = store.get_window("demo-voltage-a", 2, 10)
        restricted = store.get_window("demo-voltage-a", 2.0, 10.0, ["va_rms"])
        self.assertEqual(clean["evidence_id"], restricted["evidence_id"])
        self.assertNotEqual(clean["evidence_id"], store.get_window("demo-voltage-a", 2, 9)["evidence_id"])
        self.assertEqual(clean["data_source"], "Synthetic Demo")
        self.assertEqual(clean["kind"], "voltage")
        self.assertIn("v_positive_rms", clean["values"])
        self.assertEqual(clean["sample_count"], 17)
        self.assertEqual(clean["sample_indices"], list(range(4, 21)))
        for start, end in [(-1, 1), (2.1, 2.2)]:
            with self.assertRaises(DataError) as unavailable:
                store.get_window("demo-voltage-a", start, end)
            self.assertEqual(unavailable.exception.code, "unavailable_window")
        with self.assertRaises(DataError) as invalid:
            store.get_window("demo-voltage-a", float("nan"), 0)
        self.assertEqual(invalid.exception.code, "invalid_window")
        self.assertEqual(len(store.evaluation_cases()), 6)

    def test_runtime_store_only_lists_real_catalog_entries(self):
        self.assertEqual(RunStore(self.source).list_runs(), [])
        path, entry = self.csv("voltage.csv", "time_s,Va_RMS\n.1,231\n")
        recorded, _ = load_csv_run(path, entry)
        synthetic, _ = load_csv_run(path, {**entry, "run_id": "sample-fixture"}, synthetic=True)
        relative_catalog = Path("archive") / "catalog.json"
        write_json(self.source / relative_catalog, {"schema_version": 1, "runs": [synthetic, recorded]})
        store = RunStore(self.source, catalog_path=relative_catalog)
        self.assertEqual([run["run_id"] for run in store.list_runs()], ["voltage"])
        self.assertFalse(store.get_window("voltage")["synthetic"])
        with self.assertRaises(DataError) as excluded:
            store.get_window("sample-fixture")
        self.assertEqual(excluded.exception.code, "run_not_found")

    def test_cli_rejects_a_synthetic_manifest(self):
        _, entry = self.csv("voltage.csv", "time_s,Va_RMS\n.1,231\n")
        manifest = self.source / "manifest.json"
        write_json(manifest, {"schema_version": 1, "synthetic": True, "runs": [entry]})
        output = self.source / "normalized"
        status = import_cli(["import", "--source-dir", str(self.source), "--manifest", str(manifest), "--output-dir", str(output)])
        self.assertEqual(status, 1)
        self.assertFalse((output / "catalog.json").exists())


if __name__ == "__main__":
    unittest.main()
