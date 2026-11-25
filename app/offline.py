"""Fixed, local computations over an already validated Controller window."""

from dataclasses import dataclass
from math import isfinite, nan
from pathlib import Path
from statistics import mean
from threading import Lock
from time import monotonic
from typing import Mapping, Optional, Sequence


PYTHON_BACKEND = "Python Replay"
MATLAB_BACKEND = "Offline MATLAB"
_MATLAB_DIRECTORY = Path(__file__).resolve().parents[1] / "matlab"
_ENGINE_LOCK = Lock()
_ENGINE_START_TIMEOUT = 20.0
_ENGINE_CALL_TIMEOUT = 10.0
_ENGINE_RETRY_DELAY = 30.0
_engine_session = None
_engine_start_failure = None
_MAX_SAMPLES = 250_000
_REVIEW_SIGNALS = frozenset({
    "v_positive_rms", "v_negative_rms", "va_rms", "vb_rms", "vc_rms",
    "ia_rms", "ib_rms", "ic_rms", "i4_rms", "i_positive_rms", "i_negative_rms",
    "frequency_actual_hz", "frequency_feedback_hz", "frequency_raw_hz",
    "speed_estimated_rpm", "speed_feedback_rpm", "speed_mechanical_rpm",
    "controller_state", "torque_nm", "power_w", "energy_wh",
})


@dataclass(frozen=True)
class PIPreset:
    role: str
    signal_id: str
    unit: str
    reference: float
    error_scale: float
    bias: float
    kp: float
    ki: float
    kaw: float
    low: float
    high: float
    rate: float
    period: float

    def matlab_parameters(self):
        return [self.reference, self.error_scale, self.bias, self.kp, self.ki,
                self.kaw, self.low, self.high, self.rate, self.period]


class _EngineUnavailable(RuntimeError):
    pass


# Synthetic examples for offline replay, with normalized actuator outputs.
PI_PRESETS = {
    "voltage-example-v1": PIPreset(
        "voltage", "v_positive_rms", "ratio", 230.0, 230.0, 0.5,
        0.10, 0.25, 1.0, 0.0, 1.0, 0.20, 0.05,
    ),
    "frequency-example-v1": PIPreset(
        "frequency", "frequency_feedback_hz", "ratio", 50.0, 1.0, 0.5,
        0.08, 0.20, 1.0, 0.0, 1.0, 0.20, 0.05,
    ),
}


def _number(value):
    if isinstance(value, (int, float)) and isfinite(value):
        return float(value)
    return None


def _validated_series(values, *, allow_missing, allow_bool=False):
    if not isinstance(values, (list, tuple)) or len(values) > _MAX_SAMPLES:
        raise ValueError("Expected a bounded array of window values.")
    clean = []
    for value in values:
        if value is None and allow_missing:
            clean.append(nan)
            continue
        number = _number(value)
        if number is None or (isinstance(value, bool) and not allow_bool):
            raise ValueError("Expected finite numeric values or explicit missing samples.")
        clean.append(number)
    return clean


def _summarize(series, reference):
    values = [value for value in series if isfinite(value)]
    if not values:
        return [len(series), 0, nan, nan, nan, nan, nan]
    low, high, average = min(values), max(values), mean(values)
    deviation = nan if reference is None else average - reference
    maximum_deviation = nan if reference is None else max(abs(low - reference), abs(high - reference))
    return [len(series), len(values), low, high, average, deviation, maximum_deviation]


def _metrics(result, reference):
    count, valid_count, low, high, average, deviation, maximum_deviation = result
    return {
        "sample_count": int(count),
        "valid_count": int(valid_count),
        "missing_count": int(count - valid_count),
        "min": _number(low),
        "max": _number(high),
        "mean": _number(average),
        "range": _number(high - low),
        "reference": reference,
        "mean_deviation": _number(deviation),
        "max_abs_deviation": _number(maximum_deviation),
    }


def _load_engine():
    global _engine_session, _engine_start_failure

    if _engine_session is not None:
        return _engine_session
    if _engine_start_failure is not None:
        failed_at, reason = _engine_start_failure
        if monotonic() - failed_at < _ENGINE_RETRY_DELAY:
            raise _EngineUnavailable(reason)
    import matlab
    import matlab.engine

    try:
        future = matlab.engine.start_matlab("-nodesktop -nojvm", background=True)
        engine = _resolve_future(future, _ENGINE_START_TIMEOUT, matlab.engine.TimeoutError)
    except Exception as error:
        _engine_start_failure = (monotonic(), _engine_reason(error))
        raise
    _engine_session = (matlab, engine)
    _engine_start_failure = None
    return _engine_session


def _resolve_future(future, timeout, engine_timeout):
    try:
        return future.result(timeout)
    except engine_timeout as error:
        future.cancel()
        raise TimeoutError from error


def _engine_call(function, *args, nargout=1):
    from matlab.engine import TimeoutError as MatlabTimeoutError

    future = function(*args, nargout=nargout, background=True)
    return _resolve_future(future, _ENGINE_CALL_TIMEOUT, MatlabTimeoutError)


def _call_fixed_matlab(directory, name, operation):
    source = Path(directory or _MATLAB_DIRECTORY).resolve()
    if not (source / f"{name}.m").is_file():
        raise RuntimeError("The fixed MATLAB adapter is unavailable.")
    with _ENGINE_LOCK:
        matlab, engine = _load_engine()
        _engine_call(engine.addpath, str(source), "-begin", nargout=0)
        expected = (source / f"{name}.m").resolve()
        resolved = _engine_call(engine.which, name)
        if not resolved or Path(resolved).resolve() != expected:
            raise RuntimeError("The fixed MATLAB adapter could not be resolved.")
        return operation(matlab, engine)


def _engine_reason(error):
    if isinstance(error, _EngineUnavailable):
        return str(error)
    if isinstance(error, ImportError):
        return "The matching MATLAB Engine for Python is unavailable."
    if isinstance(error, TimeoutError):
        return "The MATLAB Engine exceeded the offline calculation timeout."
    if isinstance(error, RuntimeError) and str(error).startswith("The fixed MATLAB adapter"):
        return str(error)
    return f"The fixed MATLAB computation failed ({type(error).__name__})."


def review_window(
    signals: Mapping[str, Sequence[Optional[float]]],
    references: Optional[Mapping[str, float]] = None,
    *,
    backend: str = "python",
    matlab_directory: Optional[Path] = None,
) -> dict:
    """Summarize approved signals, preserving missingness and reference provenance."""
    inputs = {}
    reference_values = {}
    for name, values in signals.items():
        if name not in _REVIEW_SIGNALS:
            raise ValueError(f"Signal is unavailable for offline diagnostics: {name}")
        inputs[name] = _validated_series(values, allow_missing=True, allow_bool=name == "controller_state")
        reference = (references or {}).get(name)
        if reference is not None and (isinstance(reference, bool) or _number(reference) is None):
            raise ValueError(f"Reference for {name} must be finite.")
        reference_values[name] = reference

    if not inputs or not any(inputs.values()):
        return {"status": "unavailable", "backend": None, "metrics": {}, "reason": "No approved window samples are available."}

    fallback_reason = None
    if backend == "matlab_offline":
        try:
            def summarize_with_engine(matlab, engine):
                return {
                    name: list(_engine_call(engine.review_controller_window,
                        matlab.double(values), name,
                        float(reference_values[name]) if reference_values[name] is not None else nan,
                    )[0])
                    for name, values in inputs.items()
                }

            results = _call_fixed_matlab(matlab_directory, "review_controller_window", summarize_with_engine)
            return {"status": "completed", "backend": MATLAB_BACKEND,
                    "metrics": {name: _metrics(result, reference_values[name]) for name, result in results.items()}}
        except Exception as error:
            fallback_reason = _engine_reason(error)

    result = {
        "status": "completed",
        "backend": PYTHON_BACKEND,
        "metrics": {name: _metrics(_summarize(values, reference_values[name]), reference_values[name])
                    for name, values in inputs.items()},
    }
    if fallback_reason:
        result["fallback_reason"] = fallback_reason
    return result


def _simulate_pi(times, measurements, preset):
    # This window begins with fresh bias output and bumpless integral state.
    output = preset.bias
    last_update = times[0]
    first_error = (preset.reference - measurements[0]) / preset.error_scale
    integral = -preset.kp * first_error
    low, high = output, output
    updates = 0
    for time, feedback in zip(times[1:], measurements[1:]):
        period = time - last_update
        if period + 1e-12 < preset.period:
            continue
        last_update = time
        error = (preset.reference - feedback) / preset.error_scale
        raw = preset.bias + preset.kp * error + integral
        saturated = min(max(raw, preset.low), preset.high)
        change = preset.rate * period
        output = min(max(saturated, output - change), output + change)
        integral += period * (preset.ki * error + preset.kaw * (output - raw))
        low, high = min(low, output), max(high, output)
        updates += 1
    return [len(times), updates, low, high, output]


def evaluate_offline_pi(
    *,
    role: str,
    preset_id: str,
    elapsed_seconds: Optional[Sequence[Optional[float]]],
    signals: Mapping[str, Sequence[Optional[float]]],
    backend: str = "python",
    matlab_directory: Optional[Path] = None,
) -> dict:
    """Run a preset PI calculation against recorded inputs in temporary state."""
    result = {"type": "offline_pi", "preset_id": preset_id}

    def unavailable(reason):
        return dict(result, status="unavailable", backend=None, reason=reason)

    preset = PI_PRESETS.get(preset_id)
    if preset is None or role != preset.role:
        return unavailable("No matching offline PI reference preset is configured for this Agent.")
    if elapsed_seconds is None:
        return unavailable("Offline PI requires a verified elapsed-seconds clock.")
    if preset.signal_id not in signals:
        return unavailable(f"Offline PI requires recorded {preset.signal_id} samples.")
    try:
        times = _validated_series(elapsed_seconds, allow_missing=False)
        measurements = _validated_series(signals[preset.signal_id], allow_missing=False)
    except ValueError as error:
        return unavailable(str(error))
    if len(times) < 2 or len(times) != len(measurements):
        return unavailable("Offline PI requires at least two time-aligned feedback samples.")
    if times[0] < 0 or any(right <= left for left, right in zip(times, times[1:])):
        return unavailable("Offline PI requires nonnegative, strictly increasing elapsed seconds.")
    if times[-1] - times[0] + 1e-12 < preset.period:
        return unavailable("The window is shorter than the reference preset control period.")

    if backend == "matlab_offline":
        try:
            def evaluate_with_engine(matlab, engine):
                return list(_engine_call(engine.evaluate_offline_pi,
                    matlab.double(times), matlab.double(measurements),
                    matlab.double(preset.matlab_parameters()),
                )[0])

            outcome = _call_fixed_matlab(matlab_directory, "evaluate_offline_pi", evaluate_with_engine)
            executed_backend = MATLAB_BACKEND
        except Exception as error:
            return unavailable(_engine_reason(error))
    else:
        outcome = _simulate_pi(times, measurements, preset)
        executed_backend = PYTHON_BACKEND

    count, updates, low, high, last = outcome
    if not all(isfinite(value) for value in outcome):
        return unavailable("Offline PI returned non-finite metrics for this window.")
    return dict(result, status="completed", backend=executed_backend, signal_id=preset.signal_id,
                reference=preset.reference, sample_count=int(count), update_count=int(updates),
                model_output={"unit": preset.unit, "min": low, "max": high, "last": last},
                interpretation="Isolated PI arithmetic with recorded feedback and fresh state. No plant response is modeled.")
