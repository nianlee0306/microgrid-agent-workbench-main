"""Read-only run catalog, CSV normalization and bounded replay windows."""

import csv
import hashlib
import io
import json
import math
import re
import tempfile
from bisect import bisect_left, bisect_right
from copy import deepcopy
from pathlib import Path

from .agent_defs import AGENTS, RECOMMENDATIONS


class DataError(ValueError):
    def __init__(self, message, code="invalid_data"):
        super().__init__(message)
        self.code = code


# ID, display label and expected unit are independent of the source spelling.
SIGNALS = {
    "va_rms": ("Phase A voltage RMS", "V", "electrical_rms"),
    "vb_rms": ("Phase B voltage RMS", "V", "electrical_rms"),
    "vc_rms": ("Phase C voltage RMS", "V", "electrical_rms"),
    "ia_rms": ("Phase A current RMS", "A", "electrical_rms"),
    "ib_rms": ("Phase B current RMS", "A", "electrical_rms"),
    "ic_rms": ("Phase C current RMS", "A", "electrical_rms"),
    "i4_rms": ("Channel 4 current RMS", "A", "electrical_rms"),
    "v_positive_rms": ("Positive-sequence voltage RMS", "V", "electrical_rms"),
    "v_negative_rms": ("Negative-sequence voltage RMS", "V", "electrical_rms"),
    "i_positive_rms": ("Positive-sequence current RMS", "A", "electrical_rms"),
    "i_negative_rms": ("Negative-sequence current RMS", "A", "electrical_rms"),
    "frequency_actual_hz": ("Electrical frequency estimate", "Hz", "electrical_estimate"),
    "frequency_feedback_hz": ("Controller frequency feedback", "Hz", "controller_feedback"),
    "frequency_raw_hz": ("Unfiltered electrical frequency estimate", "Hz", "electrical_estimate"),
    "speed_estimated_rpm": ("Estimated speed from electrical frequency", "rpm", "electrical_estimate"),
    "speed_feedback_rpm": ("Speed indicated by controller feedback", "rpm", "controller_feedback"),
    "speed_mechanical_rpm": ("Recorded mechanical speed", "rpm", "mechanical_measurement"),
    "torque_nm": ("Recorded mechanical torque", "N·m", "mechanical_measurement"),
    "power_w": ("Recorded mechanical power", "W", "mechanical_measurement"),
    "energy_wh": ("Recorded mechanical energy", "Wh", "mechanical_measurement"),
    "controller_state": ("Recorded PI controller state", "state", "controller_record"),
}
COMMON_COLUMNS = {
    "va_rms": ("Va_RMS", "E1_RMS"), "vb_rms": ("Vb_RMS", "E2_RMS"),
    "vc_rms": ("Vc_RMS", "E3_RMS"), "ia_rms": ("Ia_RMS", "I1_RMS"),
    "ib_rms": ("Ib_RMS", "I2_RMS"), "ic_rms": ("Ic_RMS", "I3_RMS"),
    "i4_rms": ("I4_RMS",), "v_positive_rms": ("Vpositive_RMS",),
    "v_negative_rms": ("Vnegative_RMS",), "i_positive_rms": ("Ipositive_RMS",),
    "i_negative_rms": ("Inegative_RMS",),
    "controller_state": ("ControllerEnabled", "ControllerState", "Controller_State"),
}
FREQUENCY_COLUMNS = {
    **COMMON_COLUMNS,
    "frequency_actual_hz": ("FrequencyActual_Hz",),
    "frequency_feedback_hz": ("FrequencyFeedback_Hz",),
    "frequency_raw_hz": ("FrequencyRaw_Hz",),
    "speed_estimated_rpm": ("SpeedActual_rpm",),
    "speed_feedback_rpm": ("SpeedFeedback_rpm",),
}
MECHANICAL_COLUMNS = {
    "speed_mechanical_rpm": ("Speed",), "torque_nm": ("Torque",),
    "power_w": ("Power",), "energy_wh": ("Energy",),
}
FORMATS = {
    "matlab_frequency_summary": (FREQUENCY_COLUMNS, "Frequency PI", (
        "v_positive_rms", "frequency_actual_hz", "frequency_feedback_hz", "speed_estimated_rpm",
    )),
    "matlab_voltage_summary": (COMMON_COLUMNS, "Stator Voltage PI", (
        "va_rms", "vb_rms", "vc_rms", "ia_rms", "ib_rms", "ic_rms",
    )),
    "labvolt_mechanical": (MECHANICAL_COLUMNS, None, tuple(MECHANICAL_COLUMNS)),
}
RECORDING_KINDS = {
    "matlab_frequency_summary": "frequency",
    "matlab_voltage_summary": "voltage",
    "labvolt_mechanical": "mechanical",
}

RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}\Z")
MISSING = {"", "na", "n/a", "nan", "null", "none", "-", "--"}
STATE_VALUES = {
    "0": "Disabled", "0.0": "Disabled", "false": "Disabled", "off": "Disabled",
    "disabled": "Disabled", "1": "Enabled", "1.0": "Enabled", "true": "Enabled",
    "on": "Enabled", "enabled": "Enabled", "saturated": "Saturated",
    "standby": "Standby", "manual": "Manual", "automatic": "Automatic",
}
UNITS = {
    "rpm": "rpm", "r/min": "rpm", "rev/min": "rpm", "r.p.m.": "rpm",
    "nm": "N·m", "n-m": "N·m", "n.m": "N·m", "n·m": "N·m", "n•m": "N·m", "n*m": "N·m",
    "w": "W", "wh": "Wh", "w·h": "Wh", "w•h": "Wh", "w.h": "Wh", "w*h": "Wh",
    "v": "V", "a": "A", "hz": "Hz", "s": "s", "sec": "s", "seconds": "s",
    "state": "state", "bool": "state", "boolean": "state",
    "ms": "ms", "kw": "kW", "kwh": "kWh", "mv": "mV", "ma": "mA", "khz": "kHz",
}


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _normalized_unit(value):
    clean = value.strip().strip("[]()").strip()
    return UNITS.get(clean.casefold().replace(" ", ""), clean)


def _column(header, names):
    return next((header[name.casefold()] for name in names if name.casefold() in header), None)


def _format(header):
    names = set(header)
    if len(names & {"speed", "torque", "power", "energy"}) >= 2:
        return "labvolt_mechanical"
    frequency = {"frequencyactual_hz", "frequencyfeedback_hz", "speedactual_rpm", "frequencyraw_hz"}
    if "elapsedtime_s" in names and names & (frequency | {"vpositive_rms"}):
        return "matlab_frequency_summary"
    voltage = {"va_rms", "vb_rms", "vc_rms", "ia_rms", "ib_rms", "ic_rms", "vpositive_rms"}
    if "time_s" in names and names & voltage:
        return "matlab_voltage_summary"
    raise DataError("Unrecognized CSV header. Expected a MATLAB frequency/voltage summary or a LabVolt mechanical export.")


def _read_header(raw):
    try:
        decoded = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        decoded = raw.decode("cp1252")
    for sep in (",", ";", "\t"):
        reader = csv.reader(io.StringIO(decoded, newline=""), delimiter=sep, strict=True)
        for row in reader:
            if any(cell.strip() for cell in row):
                header = {cell.strip().casefold(): index for index, cell in enumerate(row) if cell.strip()}
                try:
                    source_format = _format(header)
                except DataError:
                    break
                if len(header) != sum(bool(cell.strip()) for cell in row):
                    raise DataError("Duplicate CSV headers are ambiguous.")
                return reader, row, header, source_format
    raise DataError("Unrecognized CSV header. Expected a MATLAB frequency/voltage summary or a LabVolt mechanical export.")


def _number(value):
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("Expected finite value")
    return parsed


def _cell(row, index):
    return row[index].strip() if index is not None and index < len(row) else ""


def _signal_specs(entry, available, header):
    explicit = entry.get("fields", {})
    declared_units = entry.get("units", {})
    if not isinstance(explicit, dict) or not isinstance(declared_units, dict):
        raise DataError("fields and units must be objects.")
    if set(explicit).difference(available) or set(declared_units).difference(available):
        raise DataError("fields and units may only declare signals for the detected input format.")
    columns = {}
    for signal_id, aliases in available.items():
        if signal_id in explicit:
            name = explicit[signal_id]
            if not _text(name) or name.strip().casefold() not in header:
                raise DataError(f"Field mapping for {signal_id} does not match a CSV column.")
            known_columns = [key for key, names in available.items() if name.strip().casefold() in {column.casefold() for column in names}]
            if known_columns and signal_id not in known_columns:
                raise DataError(f"Field mapping for {signal_id} conflicts with the known telemetry column {name}.")
            aliases = (name.strip(),)
        index = _column(header, aliases)
        if index is not None:
            columns[signal_id] = index
    if len(columns) != len(set(columns.values())):
        raise DataError("Multiple signal IDs map to the same source column.")
    if any(not _text(unit) or len(unit) > 32 for unit in declared_units.values()):
        raise DataError("Declared units must be nonempty strings of up to 32 characters.")
    return columns, declared_units


def _run_entry(entry):
    if not isinstance(entry, dict):
        raise DataError("Each manifest run must be an object.")
    for field in ("run_id", "file", "source", "category"):
        if not _text(entry.get(field)):
            raise DataError(f"Manifest run is missing {field}.")
    if not RUN_ID.fullmatch(entry["run_id"]):
        raise DataError("run_id must be 1–80 ASCII letters, digits, underscores or hyphens.")
    for field in ("label", "timezone", "source_uri"):
        if field in entry and not _text(entry[field]):
            raise DataError(f"Manifest {field} must be a nonempty string.")
    if "historical_event" in entry and not isinstance(entry["historical_event"], bool):
        raise DataError("historical_event must be a boolean.")


def _axes(entry, header, source_format):
    primary_clock = ("ElapsedTime_s", "time_s")
    mechanical_clock = ("ElapsedTime_s", "time_s", "Time (s)", "Time [s]", "Time_s")
    axis_column = _column(header, mechanical_clock if source_format == "labvolt_mechanical" else primary_clock)
    explicit_clock = entry.get("time_column")
    if explicit_clock is not None:
        if not _text(explicit_clock) or explicit_clock.casefold() not in header:
            raise DataError("Manifest time_column does not match a CSV column.")
        explicit_index = header[explicit_clock.casefold()]
        if axis_column is None:
            if (entry.get("time_basis") != "elapsed_seconds" or entry.get("time_unit") != "s"
                    or not _text(entry.get("time_reference"))):
                raise DataError("A nonstandard clock requires elapsed_seconds, time_unit='s' and a verified time_reference.")
            axis_column = explicit_index
        elif explicit_index != axis_column:
            raise DataError("Manifest time_column conflicts with the elapsed seconds column.")
    axis = "elapsed_seconds" if axis_column is not None else "sample_index"
    if entry.get("time_basis", axis) != axis:
        raise DataError(f"Manifest time_basis disagrees with source coordinate {axis}.")
    index_column = _column(header, ("SampleIndex", "Sample_Index", "SampleNumber", "sample_index"))
    timestamp_column = _column(header, ("Timestamp", "Datetime"))
    return axis, axis_column, index_column, timestamp_column


def _csv_rows(reader, columns, axes, export_columns):
    axis_column, index_column, timestamp_column = axes
    schema_column, timezone_column = export_columns
    values = {key: [] for key in columns}
    coordinates = []
    sample_indices = []
    timestamps = []
    schema_versions = set()
    source_timezones = set()
    unit_row = {}
    errors = []
    input_errors = invalid_rows = source_rows = blank_rows = unit_rows = 0

    def record_error(message):
        nonlocal input_errors
        input_errors += 1
        if len(errors) < 40:
            errors.append(f"CSV line {reader.line_num}: {message}")

    for row in reader:
        if not any(cell.strip() for cell in row):
            blank_rows += 1
            continue
        observed = {key: _cell(row, index) for key, index in columns.items() if _cell(row, index)}
        unit_strings = list(observed.values())
        if unit_strings and all(cell.strip("[]()").strip().casefold().replace(" ", "") in UNITS for cell in unit_strings):
            unit_rows += 1
            clock_unit = _cell(row, axis_column)
            if clock_unit and _normalized_unit(clock_unit) != "s":
                raise DataError("CSV clock unit disagrees with the elapsed seconds coordinate.")
            for key, unit in observed.items():
                normalized = _normalized_unit(unit)
                if key in unit_row and unit_row[key] != normalized:
                    record_error(f"Inconsistent unit rows for {key}.")
                unit_row.setdefault(key, normalized)
            continue
        fallback_index = source_rows
        source_rows += 1
        try:
            index = _number(_cell(row, index_column)) if index_column is not None else fallback_index
            elapsed = _number(_cell(row, axis_column)) if axis_column is not None else index
            if int(index) != index or index < 0 or elapsed < 0:
                raise ValueError("Sample indices and elapsed time must be nonnegative.")
            if sample_indices and index <= sample_indices[-1]:
                raise ValueError("Sample index must increase.")
            if coordinates and elapsed < coordinates[-1]:
                raise ValueError("Elapsed time must be nondecreasing.")
        except ValueError:
            invalid_rows += 1
            record_error("Invalid or nonmonotonic sample coordinate.")
            continue
        for signal_id in columns:
            value = observed.get(signal_id, "").casefold()
            parsed = None
            if value not in MISSING:
                try:
                    parsed = STATE_VALUES[value] if signal_id == "controller_state" else _number(value)
                except (ValueError, KeyError):
                    record_error(f"Invalid value in {signal_id}; preserved as missing.")
            values[signal_id].append(parsed)
        coordinates.append(elapsed)
        sample_indices.append(int(index))
        timestamps.append(_cell(row, timestamp_column)[:128] or None)
        if schema_column is not None:
            schema_versions.add(_cell(row, schema_column))
        if timezone_column is not None:
            source_timezones.add(_cell(row, timezone_column) or "unknown")
    counts = {
        "source_rows": source_rows, "valid_rows": len(coordinates), "invalid_rows": invalid_rows,
        "blank_rows": blank_rows, "unit_rows": unit_rows, "input_error_count": input_errors,
        "errors": errors,
    }
    if schema_versions and schema_versions != {"controller-log-v1"}:
        raise DataError("CSV contains an unsupported or inconsistent controller export schema version.")
    if schema_versions and len(source_timezones) > 1:
        raise DataError("CSV contains inconsistent controller export time zones.")
    schema_version = next(iter(schema_versions), None)
    source_timezone = next(iter(source_timezones), "unknown") if schema_version else "unknown"
    return coordinates, sample_indices, timestamps, values, unit_row, counts, schema_version, source_timezone


def _quality(values, key_signals, signals, source):
    missing_counts = {key: sum(value is None for value in series) for key, series in values.items()}
    return {
        "missing_counts": missing_counts,
        "missing_key_fields": [key for key in key_signals if key not in values or missing_counts[key] == len(values[key])],
        "unit_mismatches": [key for key, meta in signals.items() if meta["unit_conflict"]],
        "source_conflicts": [key for key, meta in signals.items() if meta["source"] != source],
    }


def load_csv_run(path, entry, *, synthetic=False):
    """Normalize one source recording, retaining blanks and coordinate provenance."""
    _run_entry(entry)
    raw = path.read_bytes()
    reader, raw_header, header, source_format = _read_header(raw)
    available, controller, key_signals = FORMATS[source_format]
    columns, declared_units = _signal_specs(entry, available, header)
    axis, axis_column, index_column, timestamp_column = _axes(entry, header, source_format)
    if axis_column in columns.values() or index_column in columns.values():
        raise DataError("A source telemetry column also maps to a sample coordinate.")
    export_columns = (_column(header, ("SchemaVersion",)), _column(header, ("TimestampTimeZone",)))
    coordinates, sample_indices, timestamps, values, unit_row, counts, version, source_timezone = _csv_rows(
        reader, columns, (axis_column, index_column, timestamp_column), export_columns,
    )
    if not coordinates or not any(value is not None for series in values.values() for value in series):
        raise DataError("No usable telemetry rows were found.", "empty_recording")
    declared_zone = entry.get("timezone", "unknown")
    if source_timezone != "unknown" and declared_zone not in {"unknown", source_timezone}:
        raise DataError("Manifest timezone disagrees with the controller export timezone.")
    signals = {}
    for key, index in columns.items():
        label, expected_unit, provenance = SIGNALS[key]
        declared = _normalized_unit(declared_units[key]) if key in declared_units else None
        observed = unit_row.get(key)
        unit = observed or declared or expected_unit
        signals[key] = {
            "label": label, "unit": unit, "expected_unit": expected_unit,
            "unit_basis": "source_unit_row" if observed else ("manifest" if declared else "source_schema"),
            "unit_conflict": any(value != expected_unit for value in (unit, declared or unit)),
            "provenance": provenance, "source": entry["source"], "column": raw_header[index].strip(),
        }
    quality = _quality(values, key_signals, signals, entry["source"])
    source_display = "Recorded LabVolt CSV" if source_format == "labvolt_mechanical" else "Recorded MATLAB Export"
    revision = hashlib.sha256(raw + json.dumps(entry, sort_keys=True).encode("utf-8")).hexdigest()
    metadata = {
        "run_id": entry["run_id"], "label": entry.get("label", entry["run_id"]), "filename": path.name,
        "source": entry["source"], "source_uri": entry.get("source_uri"),
        "category": entry["category"], "source_format": source_format, "source_schema_version": version,
        "kind": RECORDING_KINDS[source_format],
        "controller": controller, "data_source": "Synthetic Demo" if synthetic else source_display,
        "synthetic": synthetic, "axis": axis, "axis_unit": "s" if axis == "elapsed_seconds" else "sample",
        "axis_min": coordinates[0], "axis_max": coordinates[-1], "sample_count": len(coordinates),
        "time_column": raw_header[axis_column].strip() if axis_column is not None else None,
        "time_reference": entry.get("time_reference"),
        "sample_index_column": raw_header[index_column].strip() if index_column is not None else None,
        "timezone": source_timezone if source_timezone != "unknown" else declared_zone,
        "historical_event": entry.get("historical_event", False),
        "signals": signals, "key_signals": key_signals, "data_quality": quality, "verified_alignments": [],
        "revision": revision,
    }
    coverage = {**metadata, **counts, "missing_key_fields": quality["missing_key_fields"],
                "missing_counts": quality["missing_counts"]}
    return {
        "metadata": metadata, "axis_values": coordinates, "sample_indices": sample_indices,
        "timestamps": timestamps, "values": values,
    }, coverage


def _safe_source(source_dir, filename):
    root = source_dir.resolve()
    relative = Path(filename)
    path = (root / relative).resolve()
    if relative.is_absolute() or not path.is_relative_to(root) or path.suffix.casefold() != ".csv":
        raise DataError("Manifest file must be a CSV path within the source directory.")
    return path


def _verified_alignments(alignments, records):
    if not isinstance(alignments, list):
        raise DataError("verified_alignments must be a list.")
    verified = []
    for alignment in alignments:
        if not isinstance(alignment, dict):
            raise DataError("Each verified alignment must be an object.")
        ids = alignment.get("run_ids", [])
        if (not isinstance(ids, list) or len(ids) < 2 or any(not _text(run_id) for run_id in ids)
                or len(set(ids)) != len(ids) or any(run_id not in records for run_id in ids)):
            raise DataError("Verified alignment requires at least two distinct imported run IDs.")
        if alignment.get("verified") is not True or any(not _text(alignment.get(key)) for key in (
            "sync_method", "evidence_reference",
        )):
            raise DataError("Alignment requires verified=true, sync_method and evidence_reference.")
        sources = alignment.get("acquisition_sources")
        offsets = alignment.get("offsets_s")
        if not isinstance(sources, dict) or not isinstance(offsets, dict) or set(sources) != set(ids) or set(offsets) != set(ids):
            raise DataError("Verified alignment requires one acquisition source and elapsed-seconds offset per run.")
        for run_id in ids:
            meta = records[run_id]["metadata"]
            if (meta["axis"] != "elapsed_seconds" or sources[run_id] != meta["source"]
                    or type(offsets[run_id]) not in (int, float) or not math.isfinite(offsets[run_id])):
                raise DataError("Verified alignment requires compatible time axes and matching acquisition provenance.")
        start = max(records[run_id]["metadata"]["axis_min"] + offsets[run_id] for run_id in ids)
        end = min(records[run_id]["metadata"]["axis_max"] + offsets[run_id] for run_id in ids)
        if start > end:
            raise DataError("Verified alignment does not produce an overlapping time range.")
        clean = {key: alignment[key] for key in (
            "run_ids", "verified", "sync_method", "evidence_reference", "acquisition_sources", "offsets_s",
        )}
        clean["common_time_range_s"] = [start, end]
        verified.append(clean)
    for alignment in verified:
        for run_id in alignment["run_ids"]:
            records[run_id]["metadata"]["verified_alignments"].append(alignment)


def import_recordings(source_dir, manifest, *, synthetic=None):
    """Return a normalized catalog and a traceable report for a manifest."""
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1 or not isinstance(manifest.get("runs"), list):
        raise DataError("Manifest requires schema_version 1 and a runs list.")
    if "synthetic" in manifest and not isinstance(manifest["synthetic"], bool):
        raise DataError("Manifest synthetic flag must be a boolean.")
    if synthetic is None:
        synthetic = manifest.get("synthetic", False)
    records = {}
    reports = []
    failures = []
    seen_ids = set()
    for entry in manifest["runs"]:
        run_id = entry.get("run_id") if isinstance(entry, dict) else None
        filename = entry.get("file") if isinstance(entry, dict) else None
        try:
            _run_entry(entry)
            if run_id in seen_ids:
                raise DataError(f"Duplicate run ID: {run_id}.")
            seen_ids.add(run_id)
            source = _safe_source(source_dir, filename)
            record, detail = load_csv_run(source, entry, synthetic=synthetic)
            records[run_id] = record
            reports.append(detail)
        except (OSError, csv.Error, ValueError) as exc:
            failures.append({"run_id": run_id, "file": filename, "valid_rows": 0, "error": str(exc)})
    try:
        _verified_alignments(manifest.get("verified_alignments", []), records)
    except DataError as exc:
        failures.append({"run_id": None, "file": None, "error": str(exc)})
    return {"schema_version": 1, "runs": list(records.values())}, {
        "schema_version": 1, "source_directory": str(source_dir.resolve()), "runs": reports,
        "imported_runs": len(records), "failed_inputs": failures,
        "requested_runs": len(manifest["runs"]),
        "valid_rows": sum(report["valid_rows"] for report in reports),
        "input_error_count": len(failures) + sum(report["input_error_count"] for report in reports),
    }


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as temporary:
            name = Path(temporary.name)
            json.dump(value, temporary, ensure_ascii=False, allow_nan=False, indent=2)
            temporary.write("\n")
        name.replace(path)
    finally:
        if name is not None:
            name.unlink(missing_ok=True)


class RunStore:
    def __init__(self, root, *, catalog_path=None):
        self.catalog_path = Path(root) / (Path(catalog_path).expanduser() if catalog_path else "data/imported/catalog.json")
        self.records = {}
        if not self.catalog_path.exists():
            return
        catalog = read_json(self.catalog_path)
        if not isinstance(catalog, dict) or catalog.get("schema_version") != 1 or not isinstance(catalog.get("runs"), list):
            raise DataError("Imported catalog has an unsupported schema. Re-run the import command.")
        for run in catalog["runs"]:
            metadata = run.get("metadata") if isinstance(run, dict) else None
            if not isinstance(metadata, dict) or not isinstance(metadata.get("synthetic"), bool):
                raise DataError("Imported catalog has an invalid recording entry. Re-run the import command.")
            if metadata["synthetic"]:
                continue
            run_id = metadata.get("run_id")
            if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
                raise DataError("Imported catalog has an invalid run ID. Re-run the import command.")
            if run_id in self.records:
                raise DataError(f"Imported run ID conflicts with an existing run: {run_id}")
            self.records[run_id] = run

    def list_runs(self):
        return [deepcopy(run["metadata"]) for run in self.records.values()]

    def _record(self, run_id):
        if run_id not in self.records:
            raise DataError("Run was not found.", "run_not_found")
        return self.records[run_id]

    def get_run(self, run_id):
        return deepcopy(self._record(run_id)["metadata"])

    def get_window(self, run_id, start=None, end=None, signal_ids=None):
        record = self._record(run_id)
        meta = record["metadata"]
        lower, upper = meta["axis_min"], meta["axis_max"]
        start = lower if start is None else start
        end = upper if end is None else end
        if any(type(value) not in (int, float) or not math.isfinite(value) for value in (start, end)) or start > end:
            raise DataError("A window requires finite start and end coordinates in ascending order.", "invalid_window")
        if start < lower or end > upper:
            raise DataError(f"Requested window is outside the available range [{lower:g}, {upper:g}].", "unavailable_window")
        if signal_ids is None:
            signal_ids = list(meta["signals"])
        if any(key not in meta["signals"] for key in signal_ids) or len(signal_ids) != len(set(signal_ids)):
            raise DataError("Requested signals must be distinct and present in this run.", "invalid_signal")
        axis = record["axis_values"]
        first, last = bisect_left(axis, start), bisect_right(axis, end)
        if first == last:
            raise DataError("No recorded samples fall within the selected window.", "unavailable_window")
        coordinates = axis[first:last]
        values = {key: record["values"][key][first:last] for key in signal_ids}
        signals = {key: dict(meta["signals"][key]) for key in signal_ids}
        key_signals = [key for key in meta["key_signals"] if key in signals or key not in meta["signals"]]
        quality = _quality(values, key_signals, signals, meta["source"])
        quality.update({"time_basis": meta["axis"], "timezone": meta["timezone"], "replay_clock": end})
        payload = json.dumps([meta["revision"], run_id, float(start) or 0.0, float(end) or 0.0], separators=(",", ":"))
        evidence_id = "ev_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
        return {
            **deepcopy(meta), "start": start, "end": end, "axis_values": coordinates,
            "sample_indices": record["sample_indices"][first:last], "timestamps": record["timestamps"][first:last],
            "values": values, "sample_count": len(coordinates), "signals": signals,
            "evidence_id": evidence_id, "data_quality": quality,
        }

    def evaluation_cases(self):
        path = self.catalog_path.with_name("evaluation_cases.json")
        if not path.exists():
            return []
        try:
            cases = read_json(path)
        except (OSError, ValueError) as exc:
            raise DataError("Could not read evaluation_cases.json.", "invalid_evaluation") from exc
        if not isinstance(cases, list):
            raise DataError("evaluation_cases.json must contain a JSON array of labeled cases.", "invalid_evaluation")
        case_ids = set()
        for index, case in enumerate(cases, 1):
            try:
                self._validate_evaluation_case(case, case_ids)
            except DataError as exc:
                raise DataError(f"Evaluation case {index}: {exc}", "invalid_evaluation") from exc
            case_ids.add(case["case_id"])
        return cases

    def _validate_evaluation_case(self, case, case_ids):
        required_text = ("case_id", "condition", "agent_id", "task", "run_id", "ground_truth")
        fields = {*required_text, "start", "end", "signal_ids"}
        if not isinstance(case, dict) or case.keys() != fields or not all(_text(case[key]) for key in required_text):
            raise DataError("Provide case_id, condition, agent_id, task, run_id, start, end, signal_ids and ground_truth.")
        if case["case_id"] in case_ids:
            raise DataError("Case IDs must be distinct.")
        agent = AGENTS.get(case["agent_id"])
        if agent is None or case["task"] not in agent.tasks:
            raise DataError("Select a registered Agent and task.")
        if case["ground_truth"] not in RECOMMENDATIONS:
            raise DataError("Use a supported labeled recommendation.")
        if any(type(case[key]) not in (int, float) for key in ("start", "end")):
            raise DataError("Start and end must be numeric recording coordinates.")
        signals = case["signal_ids"]
        if not isinstance(signals, list) or not signals or not all(
            isinstance(signal, str) and signal in agent.signal_ids for signal in signals
        ):
            raise DataError("Signal IDs must be a nonempty list of signals allowed for the selected Agent.")
        self.get_window(case["run_id"], case["start"], case["end"], signals)
