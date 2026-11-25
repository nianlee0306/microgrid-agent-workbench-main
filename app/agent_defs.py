"""Registry for the three Controller workflows."""

from dataclasses import dataclass

GET_WINDOW = "controller.get_window"
DIAGNOSTICS = "controller.compute_diagnostics"
QUALITY = "controller.get_evidence_quality"
OFFLINE_PI = "controller.evaluate_offline_pi"

VOLTAGE = ("va_rms", "vb_rms", "vc_rms")
FREQUENCY = ("frequency_actual_hz", "frequency_feedback_hz")
VOLTAGE_SIGNALS = (*VOLTAGE, "v_positive_rms", "v_negative_rms", "ia_rms", "ib_rms", "ic_rms",
                   "i4_rms", "i_positive_rms", "i_negative_rms", "controller_state")
FREQUENCY_SIGNALS = (*FREQUENCY, "frequency_raw_hz", "speed_estimated_rpm", "speed_feedback_rpm",
                     "speed_mechanical_rpm", *VOLTAGE_SIGNALS)
SECURITY_SIGNALS = tuple(dict.fromkeys((*VOLTAGE_SIGNALS, *FREQUENCY_SIGNALS, "torque_nm", "power_w", "energy_wh")))


@dataclass(frozen=True)
class Agent:
    id: str
    name: str
    description: str
    controller: str
    tasks: dict[str, str]
    tools: tuple[str, ...]
    signal_ids: tuple[str, ...]
    instructions: str

    def public(self, modes):
        return {
            "id": self.id, "name": self.name, "description": self.description, "controller": self.controller,
            "tasks": [{"id": key, "label": label} for key, label in self.tasks.items()],
            "tools": self.tools, "signal_ids": self.signal_ids, "modes": modes,
        }


AGENTS = {
    "voltage": Agent(
        "voltage", "Voltage Control Agent", "Review stator voltage RMS, phase balance, and recorded PI state.",
        "Stator Voltage PI", {"diagnose_voltage": "Review voltage and phase balance", "offline_pi": "Evaluate offline voltage PI"},
        (GET_WINDOW, DIAGNOSTICS, OFFLINE_PI), VOLTAGE_SIGNALS,
        "Assess three-phase voltage deviation, phase imbalance and any recorded PI state.",
    ),
    "frequency": Agent(
        "frequency", "Frequency and Speed Agent", "Review measured frequency, controller feedback, and speed provenance.",
        "Frequency PI", {"diagnose_frequency": "Review frequency and speed", "offline_pi": "Evaluate offline frequency PI"},
        (GET_WINDOW, DIAGNOSTICS, OFFLINE_PI), FREQUENCY_SIGNALS,
        "Differentiate electrical actual frequency, controller feedback, derived speed and independent mechanical speed. "
        "Describe observed variation without inferring oscillation causation or equivalence across collections.",
    ),
    "security": Agent(
        "security", "Security Review Agent", "Check evidence provenance, units, completeness, and replay timing.",
        "Evidence review", {"review_evidence": "Review evidence quality"}, (QUALITY,), SECURITY_SIGNALS,
        "Review completeness, provenance, units and replay timing. Flag gaps without attributing their cause.",
    ),
}

RECOMMENDATIONS = ("CONTINUE_MONITORING", "REQUEST_REVIEW", "INSUFFICIENT_EVIDENCE")
RECOMMENDATION_SCHEMA = {
    "type": "object",
    "properties": {
        "recommendation": {"type": "string", "enum": list(RECOMMENDATIONS)},
        "rationale": {"type": "string"},
        "evidence_ids": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["recommendation", "rationale", "evidence_ids"],
    "additionalProperties": False,
}


def suggestion(value, rationale, evidence_ids=()):
    return {"recommendation": value, "rationale": rationale, "evidence_ids": list(evidence_ids)}
