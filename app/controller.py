"""Controller boundary for an immutable, selected archive window."""

from .agent_defs import DIAGNOSTICS, GET_WINDOW, OFFLINE_PI, QUALITY
from .offline import PI_PRESETS, evaluate_offline_pi, review_window

DESCRIPTIONS = {
    GET_WINDOW: "Read a bounded summary of the selected historical telemetry. Raw rows are withheld.",
    DIAGNOSTICS: "Compute descriptive statistics and compare the selected signals to configured research limits.",
    QUALITY: "Read evidence completeness, units, provenance, replay freshness, and prior diagnostic status.",
    OFFLINE_PI: "Evaluate the registered normal PI arithmetic with isolated temporary state for the selected window.",
}

EXPECTED_UNITS = {
    "va_rms": "V", "vb_rms": "V", "vc_rms": "V", "v_positive_rms": "V", "v_negative_rms": "V",
    "ia_rms": "A", "ib_rms": "A", "ic_rms": "A", "i4_rms": "A", "i_positive_rms": "A",
    "i_negative_rms": "A", "frequency_actual_hz": "Hz", "frequency_raw_hz": "Hz",
    "frequency_feedback_hz": "Hz", "speed_estimated_rpm": "rpm", "speed_feedback_rpm": "rpm",
    "speed_mechanical_rpm": "rpm",
    "torque_nm": "N·m", "power_w": "W", "energy_wh": "Wh",
}


def workflow_for(agent_id, task):
    if agent_id == "security":
        return (QUALITY,)
    return (GET_WINDOW, DIAGNOSTICS, OFFLINE_PI) if task == "offline_pi" else (GET_WINDOW, DIAGNOSTICS)


def preset_for(agent_id):
    return next(
        (key for key, preset in PI_PRESETS.items()
         if (preset.get("role") if isinstance(preset, dict) else getattr(preset, "role", None)) == agent_id),
        None,
    )


class Controller:
    def __init__(self, agent, task, window, quality, thresholds, settings, audit, review_id, prior=()):
        self.agent = agent
        self.task = task
        self.window = window
        self.quality = quality
        self.thresholds = thresholds
        self.settings = settings
        self.audit = audit
        self.review_id = review_id
        self.prior = prior
        self.workflow = workflow_for(agent.id, task)
        self.calls = []
        self.diagnostics = {}
        self.evidence = {}
        self.preset = preset_for(agent.id)
        self.selected = list(window["values"])
        self.scope = {
            "run_scope": window["evidence_id"],
            "start": window["start"],
            "end": window["end"],
        }

    def args(self, tool):
        if tool == OFFLINE_PI:
            return {**self.scope, "preset_id": self.preset}
        return {**self.scope, "signal_ids": self.selected}

    def spec(self, name):
        params = {}
        for key, value in self.args(name).items():
            if key == "signal_ids":
                params[key] = {"type": "array", "items": {"type": "string", "enum": value},
                               "minItems": len(value), "maxItems": len(value)}
            elif key in ("start", "end"):
                params[key] = {"type": "number", "enum": [value]}
            elif value is None:
                params[key] = {"type": "null"}
            else:
                params[key] = {"type": "string", "enum": [value]}
        return {"type": "function", "name": name.replace(".", "__"), "description": DESCRIPTIONS[name],
                "strict": True, "parameters": {"type": "object", "properties": params,
                "additionalProperties": False, "required": list(params)}}

    def signal_summary(self):
        signals = {}
        for key, meta in self.window["signals"].items():
            if key not in self.selected:
                continue
            expected = EXPECTED_UNITS.get(key)
            signals[key] = {
                "unit": expected if expected and key not in self.quality["unit_mismatches"] else None,
                "provenance": {
                    "frequency_actual_hz": "electrical estimate",
                    "frequency_raw_hz": "unfiltered electrical estimate",
                    "frequency_feedback_hz": "controller feedback",
                    "speed_estimated_rpm": "derived from electrical frequency",
                    "speed_feedback_rpm": "derived from controller feedback",
                    "speed_mechanical_rpm": "independently recorded mechanical quantity",
                    "controller_state": "recorded controller state",
                }.get(key, "recorded channel"),
                "valid_count": self.window["sample_count"] - self.quality["missing_counts"][key],
            }
        return signals

    def diagnostics_result(self, backend=None):
        values = self.window["values"]
        thresholds = self.thresholds
        voltage = self.agent.id == "voltage"
        reference_key = "voltage_reference_v" if voltage else "frequency_reference_hz"
        limit_key = "voltage_max_abs_deviation_v" if voltage else "frequency_max_abs_deviation_hz"
        keys = ("va_rms", "vb_rms", "vc_rms") if voltage else ("frequency_actual_hz", "frequency_feedback_hz")
        reference, limit = thresholds.get(reference_key), thresholds.get(limit_key)
        numeric = {
            key: series for key, series in values.items() if key != "controller_state"
            and all(value is None or type(value) in (int, float) for value in series)
        }
        references = {key: reference for key in keys if key in numeric and reference is not None}
        result = review_window(numeric, references, backend=backend or self.settings.backend,
                               matlab_directory=self.settings.root / "matlab")
        exceedances = []
        for key in references:
            samples = [value for value in numeric[key] if value is not None]
            if limit is not None and samples and max(abs(value - reference) for value in samples) > limit:
                exceedances.append(key)
        phase_threshold = thresholds.get("phase_max_spread_v") if voltage else None
        phase_spread = None
        if voltage and all(key in numeric for key in keys):
            spreads = [max(row) - min(row) for row in zip(*(numeric[key] for key in keys)) if None not in row]
            if spreads:
                phase_spread = max(spreads)
                if phase_threshold is not None and phase_spread > phase_threshold:
                    exceedances.append("phase_spread")
        actual, feedback = (numeric.get(key, []) for key in ("frequency_actual_hz", "frequency_feedback_hz"))
        frequency_disagreement = max(
            (abs(a - b) for a, b in zip(actual, feedback) if a is not None and b is not None), default=None
        )
        result.update({
            "calculation_type": "Window diagnostics",
            "threshold_version": thresholds.get("version"),
            "threshold_status": "available" if reference is not None and limit is not None
                                and (not voltage or phase_threshold is not None) else "unavailable",
            "reference": reference,
            "max_abs_deviation_limit": limit,
            "max_phase_spread_v": phase_spread,
            "frequency_actual_feedback_max_gap_hz": frequency_disagreement,
            "controller_state_available": any(value is not None for value in values.get("controller_state", [])),
            "exceedances": exceedances,
        })
        return result

    def execute(self, name, arguments):
        if len(self.calls) >= len(self.workflow) or name != self.workflow[len(self.calls)] or name not in self.agent.tools:
            raise ValueError("Tool is outside the registered workflow.")
        expected = self.args(name)
        if not isinstance(arguments, dict) or arguments.keys() != expected.keys():
            raise ValueError("Unexpected tool arguments.")
        for key in ("start", "end"):
            if type(arguments[key]) not in (int, float):
                raise ValueError("Window bounds must be numeric.")
        if "signal_ids" in arguments:
            ids = arguments["signal_ids"]
            if not isinstance(ids, list) or not all(isinstance(ident, str) for ident in ids):
                raise ValueError("Signal IDs must be strings.")
            arguments = {**arguments, "signal_ids": sorted(ids)}
            expected = {**expected, "signal_ids": sorted(expected["signal_ids"])}
        if arguments != expected:
            raise ValueError("Tool scope does not match the selected window.")
        evidence_id = self.window["evidence_id"]
        backend = "Python Replay"
        status = "completed"
        if name == DIAGNOSTICS:
            result = self.diagnostics_result()
            self.diagnostics = result
        elif name == OFFLINE_PI:
            signal = PI_PRESETS[self.preset].signal_id if self.preset else None
            if signal in (*self.quality["unit_mismatches"], *self.quality["source_conflicts"]):
                result = {"status": "unavailable", "backend": None, "calculation_type": "Offline PI evaluation",
                          "reason": "The preset input has a recorded unit or provenance conflict."}
            else:
                result = evaluate_offline_pi(
                    role=self.agent.id, preset_id=self.preset or "",
                    elapsed_seconds=self.window["axis_values"] if self.window["axis"] == "elapsed_seconds" else None,
                    signals=self.window["values"], backend=self.settings.backend,
                    matlab_directory=self.settings.root / "matlab",
                )
        elif name == QUALITY:
            result = {"calculation_type": "Evidence review", "signals": self.signal_summary(),
                      "prior_diagnostics": list(self.prior), "data_quality": self.quality}
        else:
            result = {"calculation_type": "Historical replay", "signals": self.signal_summary(),
                      "data_quality": self.quality}
        backend = result.get("backend", backend)
        status = result.get("status") or status
        location = {key: self.window[key] for key in ("evidence_id", "run_id", "start", "end", "axis")}
        self.evidence[evidence_id] = location
        event = {
            **location, "tool": name, "status": status, "sample_count": self.window["sample_count"],
            "backend": backend, "data_source": self.window["data_source"], "data_quality": self.quality,
            "signal_ids": self.selected, "result": result,
        }
        self.audit.write({"event": "controller_tool", "review_id": self.review_id,
                          **{key: value for key, value in event.items() if key != "result"}})
        self.calls.append(event)
        return {
            "evidence_id": evidence_id, "start": location["start"], "end": location["end"], "axis": location["axis"],
            "sample_count": self.window["sample_count"], "backend": backend, "status": status, "result": result,
        }
