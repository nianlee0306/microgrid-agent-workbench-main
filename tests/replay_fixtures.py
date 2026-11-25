"""Synthetic recordings available only to automated tests."""

from pathlib import Path

from app.data import RunStore, import_recordings, read_json


FIXTURES = Path(__file__).with_name("fixtures") / "replay"


class ReplayFixtureStore(RunStore):
    def __init__(self):
        catalog, report = import_recordings(FIXTURES, read_json(FIXTURES / "manifest.json"), synthetic=True)
        if report["failed_inputs"]:
            raise AssertionError(f"Invalid synthetic fixtures: {report['failed_inputs']}")
        self.records = {run["metadata"]["run_id"]: run for run in catalog["runs"]}

    def evaluation_cases(self):
        return read_json(FIXTURES / "evaluation_cases.json")
