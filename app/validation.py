"""Deterministic checks and baseline decisions for archived replay."""

from .agent_defs import FREQUENCY, VOLTAGE, RECOMMENDATIONS, suggestion


def required_signals(agent_id, window):
    if agent_id == "voltage":
        return VOLTAGE
    if agent_id == "frequency":
        return FREQUENCY
    return {
        "voltage": VOLTAGE,
        "frequency": FREQUENCY,
        "mechanical": ("speed_mechanical_rpm",),
    }.get(window.get("kind"), tuple(window.get("key_signals", window["values"])))


def quality_for(window, required, thresholds, overrides=None):
    recorded = window["data_quality"]
    values = window["values"]
    quality = {
        "missing_counts": {key: sum(value is None for value in series) for key, series in values.items()},
        "missing_key_fields": [
            key for key in required if key not in values or any(value is None for value in values[key])
        ],
        "unit_mismatches": list(recorded.get("unit_mismatches") or []),
        "source_conflicts": list(recorded.get("source_conflicts") or []),
        "sample_count": window["sample_count"],
        "time_basis": window["axis"],
        "timezone": window.get("timezone") or "unknown",
        "replay_clock": None,
        "last_observed": None,
        "evidence_age_s": None,
        "max_evidence_age_s": thresholds.get("max_evidence_age_s"),
        "threshold_version": thresholds.get("version"),
    }
    quality.update(overrides or {})
    if window["axis"] == "elapsed_seconds":
        quality["replay_clock"] = quality["replay_clock"] if quality["replay_clock"] is not None else window["end"]
        present = [values[key] for key in required if key in values]
        last = next((
            axis for index, axis in reversed(list(enumerate(window["axis_values"])))
            if present and all(series[index] is not None for series in present)
        ), None)
        quality["last_observed"] = last
        if last is not None:
            quality["evidence_age_s"] = round(quality["replay_clock"] - last, 9)
    return quality


def evidence_issues(quality):
    conflicts = []
    gaps = []
    if quality["unit_mismatches"]:
        conflicts.append("INCONSISTENT_UNITS")
    if quality["source_conflicts"]:
        conflicts.append("INCONSISTENT_SOURCE")
    if quality["missing_key_fields"]:
        gaps.append("REQUIRED_FIELDS_MISSING")
    age = quality["evidence_age_s"]
    limit = quality["max_evidence_age_s"]
    if age is not None and limit is not None and (age < 0 or age > limit):
        gaps.append("REPLAY_EVIDENCE_STALE")
    if quality["time_basis"] == "sample_index":
        gaps.append("ELAPSED_TIME_UNAVAILABLE")
    elif limit is None:
        gaps.append("FRESHNESS_THRESHOLD_UNAVAILABLE")
    return conflicts, gaps


def baseline(agent_id, quality, diagnostics, evidence_ids):
    conflicts, gaps = evidence_issues(quality)
    if conflicts:
        return suggestion("REQUEST_REVIEW", "Resolve the unit or source conflicts in the recorded evidence.", evidence_ids)
    if gaps:
        return suggestion("INSUFFICIENT_EVIDENCE", "The selected evidence has required data, replay time or freshness gaps.", evidence_ids)
    if agent_id != "security" and diagnostics.get("threshold_status") != "available":
        return suggestion("INSUFFICIENT_EVIDENCE", "A versioned research threshold is required to classify this window.", evidence_ids)
    if diagnostics.get("exceedances"):
        return suggestion("REQUEST_REVIEW", "Review the recorded deviations that exceed the configured research limits.", evidence_ids)
    return suggestion("CONTINUE_MONITORING", "The available evidence passed the configured checks for this window.", evidence_ids)


def validate(raw, window, evidence, quality, diagnostics, agent_id, completed=True):
    refs = raw.get("evidence_ids") if isinstance(raw, dict) else None
    refs = refs if isinstance(refs, list) else []
    expected = (window["run_id"], window["start"], window["end"])
    valid_refs = [
        ident for ident in refs if isinstance(ident, str) and ident in evidence
        and tuple(evidence[ident][key] for key in ("run_id", "start", "end")) == expected
    ]
    valid_shape = (
        isinstance(raw, dict) and raw.keys() == {"recommendation", "rationale", "evidence_ids"}
        and raw["recommendation"] in RECOMMENDATIONS
        and isinstance(raw["rationale"], str) and 0 < len(raw["rationale"].strip()) <= 1600
        and isinstance(raw["evidence_ids"], list) and len(refs) <= 8
        and all(isinstance(ident, str) for ident in refs) and len(set(refs)) == len(refs)
    )
    reasons = []
    if not completed:
        reasons.append("REQUIRED_TOOL_WORKFLOW_INCOMPLETE")
    if not valid_shape:
        reasons.append("INVALID_RECOMMENDATION_SCHEMA")
    if len(valid_refs) != len(refs):
        reasons.append("INVALID_EVIDENCE_REFERENCE")
    if not valid_refs:
        reasons.append("NO_WINDOW_EVIDENCE_CITED")
    conflicts, gaps = evidence_issues(quality)
    reasons.extend(conflicts)
    reasons.extend(gaps)
    if agent_id != "security" and diagnostics.get("threshold_status") != "available":
        reasons.append("RESEARCH_THRESHOLDS_UNAVAILABLE")
    if valid_shape and raw["recommendation"] == "CONTINUE_MONITORING" and diagnostics.get("exceedances"):
        reasons.append("DIAGNOSTIC_LIMIT_EXCEEDED")
    verification = {
        "status": "passed" if not reasons else "flagged",
        "reasons": reasons,
        "valid_evidence_ids": valid_refs,
        "cited_count": len(refs),
        "valid_count": len(valid_refs),
        "threshold_version": quality["threshold_version"],
    }
    if not reasons:
        return raw, verification
    if conflicts or "DIAGNOSTIC_LIMIT_EXCEEDED" in reasons:
        return suggestion("REQUEST_REVIEW", "The verifier flagged a supported conflict or a research-limit exceedance.", valid_refs), verification
    return suggestion("INSUFFICIENT_EVIDENCE", "The verifier could not establish complete, valid evidence for this recommendation.", valid_refs), verification
