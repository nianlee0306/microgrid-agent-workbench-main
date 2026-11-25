"""Closed-loop PI responses of fixed, normalized synthetic plants."""

from math import exp, isfinite, sqrt

from .offline import MATLAB_BACKEND, _call_fixed_matlab, _engine_call, _engine_reason

PYTHON_SIMULATION = "Python Simulation"
_MATLAB_FUNCTION = "simulate_pi_sandbox"
_TIME_STEP = 0.05
_STEP_SAMPLE = 20
_SAMPLE_COUNT = 501
_GAIN_MIN, _GAIN_MAX = 0.0, 4.0
_COMMAND_MAX = 2.0
_BASELINE_GAINS = {"kp": 0.6, "ki": 0.5}
_SCENARIOS = {
    "voltage": ("Voltage response", 0.7, "A synthetic, faster first-order response to a reference step."),
    "frequency": ("Frequency response", 1.3, "A synthetic, slower first-order response to a reference step."),
}


def sandbox_scenarios():
    return [
        {
            "id": role,
            "label": label,
            "description": description,
            "baseline_gains": _BASELINE_GAINS.copy(),
            "gain_bounds": {key: [_GAIN_MIN, _GAIN_MAX] for key in _BASELINE_GAINS},
            "step_at_s": _STEP_SAMPLE * _TIME_STEP,
            "duration_s": (_SAMPLE_COUNT - 1) * _TIME_STEP,
        }
        for role, (label, _, description) in _SCENARIOS.items()
    ]


def validate_gains(kp, ki):
    gains = {"kp": kp, "ki": ki}
    for name, value in gains.items():
        if type(value) not in (int, float) or not _GAIN_MIN <= value <= _GAIN_MAX:
            raise ValueError(f"{name} must be a finite number from {_GAIN_MIN:g} to {_GAIN_MAX:g}.")
    return {name: float(value) for name, value in gains.items()}


def _simulate_python(scenario, gains):
    decay = exp(-_TIME_STEP / _SCENARIOS[scenario][1])
    kp, ki = gains["kp"], gains["ki"]
    output, integral = 0.0, 0.0
    trace = [[], [], [], [], []]
    for index in range(_SAMPLE_COUNT):
        reference = float(index >= _STEP_SAMPLE)
        error = reference - output
        raw = kp * error + integral
        command = min(_COMMAND_MAX, max(0.0, raw))
        limited = float(command != raw)
        for series, value in zip(trace, (index * _TIME_STEP, reference, output, command, limited)):
            series.append(value)
        if not ((raw > _COMMAND_MAX and error > 0) or (raw < 0 and error < 0)):
            integral += _TIME_STEP * ki * error
        output = decay * output + (1.0 - decay) * command
    return trace


def _metrics(rows):
    _, _, output, _, limited = rows
    post_step = output[_STEP_SAMPLE:]
    errors = [abs(1.0 - value) for value in post_step]
    count = len(errors)
    last_outside = max((index for index, error in enumerate(errors) if error > 0.02), default=-1)
    return {
        "iae": _TIME_STEP * sum(errors),
        "rmse": sqrt(sum(error ** 2 for error in errors) / count),
        "overshoot_pct": 100.0 * max(0.0, max(post_step) - 1.0),
        "settling_time_s": None if last_outside == count - 1 else _TIME_STEP * (last_outside + 1),
        "saturation_pct": 100.0 * sum(limited[_STEP_SAMPLE:]) / count,
        "final_error": errors[-1],
    }


def simulate_pi_sandbox(scenario, kp, ki, *, backend="python", matlab_directory=None):
    """Execute a fresh normalized simulation. Its only variable inputs are scenario and PI gains."""
    if scenario not in _SCENARIOS:
        raise ValueError("Select a fixed voltage or frequency sandbox scenario.")
    if backend not in {"python", "matlab_offline"}:
        raise ValueError("Select the MATLAB or Python sandbox backend.")
    gains = validate_gains(kp, ki)
    executed_backend = PYTHON_SIMULATION
    fallback_reason = None
    rows = None
    if backend == "matlab_offline":
        try:
            def run_with_engine(_matlab, engine):
                matrix = _engine_call(engine.simulate_pi_sandbox, scenario, gains["kp"], gains["ki"])
                return [list(row) for row in matrix]

            rows = _call_fixed_matlab(matlab_directory, _MATLAB_FUNCTION, run_with_engine)
            if len(rows) != 5 or any(len(row) != _SAMPLE_COUNT for row in rows):
                raise RuntimeError("Unexpected sandbox result shape.")
            if any(not isfinite(value) for row in rows for value in row):
                raise RuntimeError("Non-finite sandbox result.")
            executed_backend = MATLAB_BACKEND
        except Exception as error:
            rows = None
            fallback_reason = _engine_reason(error)
    if rows is None:
        rows = _simulate_python(scenario, gains)
    result = {
        "scenario": scenario,
        "gains": gains,
        "backend": executed_backend,
        "metrics": _metrics(rows),
        "trace": dict(zip(("time_s", "reference", "output", "command"), rows[:4])),
    }
    if fallback_reason:
        result["fallback_reason"] = fallback_reason
    return result


def tuning_gate(baseline, candidate):
    """Qualify a candidate for the synthetic session under fixed comparison criteria."""
    current = baseline["metrics"]
    proposed = candidate["metrics"]
    reasons = []
    if baseline["scenario"] != candidate["scenario"] or baseline["backend"] != candidate["backend"]:
        reasons.append("Compare the same scenario on the same simulation backend.")
    if proposed["iae"] >= current["iae"] * 0.995:
        reasons.append("Tracking error must improve by more than 0.5%.")
    if proposed["overshoot_pct"] > min(15.0, current["overshoot_pct"] + 5.0):
        reasons.append("Overshoot exceeds 15% or increases by more than five percentage points.")
    if proposed["saturation_pct"] > 30.0:
        reasons.append("The simulated command is clipped for more than 30% of the step response.")
    if proposed["settling_time_s"] is None:
        reasons.append("The response must settle within 2% of the reference during the experiment.")
    return {"eligible": not reasons, "reasons": reasons}
