"""Operator CLI for importing authorized, local experiment recordings."""

import argparse
import json
import sys
from pathlib import Path

from app.data import DataError, import_recordings, read_json, write_json


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    importer = commands.add_parser("import", help="Normalize a manifest of local CSVs and write a coverage report")
    importer.add_argument("--source-dir", type=Path, required=True, help="Root directory of the authorized CSV files")
    importer.add_argument("--manifest", type=Path, required=True, help="Manifest JSON with stable run IDs and provenance")
    default_output = Path(__file__).resolve().parents[1] / "data" / "imported"
    importer.add_argument("--output-dir", type=Path, default=default_output, help="Destination for catalog and coverage report")
    args = parser.parse_args(argv)

    try:
        if not args.source_dir.is_dir():
            raise DataError("Source directory does not exist.")
        manifest = read_json(args.manifest)
        if isinstance(manifest, dict) and manifest.get("synthetic") is True:
            raise DataError("The recording import requires real CSVs. Synthetic fixtures are reserved for tests.")
        catalog, report = import_recordings(args.source_dir, manifest)
        report_path = args.output_dir / "coverage-report.json"
        write_json(report_path, report)
        if catalog["runs"]:
            write_json(args.output_dir / "catalog.json", catalog)
    except (OSError, ValueError) as exc:
        print(f"Import failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps({
        "imported_runs": report["imported_runs"], "requested_runs": report["requested_runs"],
        "valid_rows": report["valid_rows"], "input_error_count": report["input_error_count"],
        "coverage_report": str(report_path.resolve()),
    }))
    return int(not catalog["runs"] or bool(report["input_error_count"]))


if __name__ == "__main__":
    sys.exit(main())
