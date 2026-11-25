"""Request numerical proposals for a synthetic, isolated PI experiment."""

import json
from math import isfinite

from .model import ModelFailure

_GAIN_KEYS = ("kp", "ki")
_METRIC_KEYS = (
    "iae", "rmse", "overshoot_pct", "settling_time_s", "saturation_pct", "final_error",
)


def _finite_number(value):
    return type(value) is int or (type(value) is float and isfinite(value))


def _check_candidate(candidate, bounds):
    if not isinstance(candidate, dict) or candidate.keys() != {"kp", "ki", "rationale"}:
        raise ModelFailure("invalid_gain_proposal")
    for key in _GAIN_KEYS:
        low, high = bounds[key]
        gain = candidate[key]
        if not _finite_number(gain) or not low <= gain <= high:
            raise ModelFailure("invalid_gain_proposal")
    rationale = candidate["rationale"]
    if not isinstance(rationale, str) or not 1 <= len(rationale.strip()) <= 400:
        raise ModelFailure("invalid_gain_proposal")
    return {key: float(candidate[key]) for key in _GAIN_KEYS} | {"rationale": rationale.strip()}


def _gain_trials(current, bounds):
    low, high = bounds
    values = (min(high, max(low, round(current + change, 8)))
              for change in (0.3, 0.5, 0.7, -0.3, -0.5, -0.7))
    return list(dict.fromkeys(value for value in values if abs(value - current) > 1e-8))


def _trial_schema(kp, ki):
    return {
        "type": "object",
        "properties": {
            "kp": {"type": "number", "enum": kp},
            "ki": {"type": "number", "enum": ki},
            "rationale": {"type": "string", "minLength": 1, "maxLength": 400},
        },
        "required": ["kp", "ki", "rationale"],
        "additionalProperties": False,
    }


def propose_gains(ollama, *, mode, model, scenario, gains, metrics, bounds, trace):
    """Return a single bounded candidate. Simulation and application are server actions."""
    scenario_id = scenario.get("id") if isinstance(scenario, dict) else scenario
    if scenario_id not in ("voltage", "frequency"):
        raise ValueError("Unknown synthetic PI scenario.")

    if mode == "demo":
        candidate = {
            "kp": min(bounds["kp"][1], gains["kp"] + 0.6),
            "ki": min(bounds["ki"][1], gains["ki"] + 0.7),
            "rationale": "Increase the proportional and integral gains by fixed sandbox increments, then test the step response.",
        }
        return _check_candidate(candidate, bounds)
    if mode != "local":
        raise ModelFailure("local_model_required")

    kp_trials, ki_trials = (_gain_trials(gains[key], bounds[key]) for key in _GAIN_KEYS)
    kp_options, ki_options = [*kp_trials, gains["kp"]], [*ki_trials, gains["ki"]]
    # Disjoint branches allow changing either gain or both while excluding the active pair.
    schema = {"anyOf": [
        _trial_schema(kp_options, ki_trials),
        _trial_schema(kp_trials, [gains["ki"]]),
    ]}
    context = {
        "scenario": scenario_id,
        "source": "Normalized synthetic step response",
        "active_gains": {key: gains[key] for key in _GAIN_KEYS},
        "baseline_metrics": {key: metrics[key] for key in _METRIC_KEYS if key in metrics},
        "gain_bounds": bounds,
        "gain_trials": {"kp": kp_options, "ki": ki_options},
    }
    response = ollama.propose_pi_gains(
        model,
        "You propose parameters for an isolated synthetic step-response experiment. "
        "Answer with exactly one JSON object matching the supplied schema.",
        "Choose one pair from the listed gain trials. The active gains describe the measured baseline "
        "and at least one gain must change. For low overshoot and ample saturation margin, consider "
        "increasing kp by around 0.3 and ki by around 0.5. Use the baseline metrics to decide the direction "
        "and size of the change. Prefer a modest adjustment that reduces "
        "integral absolute error (IAE) by more than 0.5% while targeting at most 15% overshoot, "
        "30% actuator saturation, and absolute final error of 0.02. State the intended effect in one "
        "sentence. The simulator will measure the outcome, so reserve numerical outcome claims for its results.\n\n"
        + json.dumps(context, allow_nan=False, separators=(",", ":")),
        schema,
        trace,
    )
    candidate = _check_candidate(response, bounds)
    if candidate["kp"] not in kp_options or candidate["ki"] not in ki_options or (
        candidate["kp"] == gains["kp"] and candidate["ki"] == gains["ki"]
    ):
        raise ModelFailure("invalid_gain_proposal")
    return candidate
